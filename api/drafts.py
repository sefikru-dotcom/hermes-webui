"""Atomic sidecar storage for WebUI composer drafts.

Background: ``POST /api/session/draft`` used to call ``get_session()`` (a full
JSON parse of the whole conversation), take the per-session agent lock and call
``Session.save()`` (full re-serialisation + fsync + index churn). On a session
with a few thousand messages that is 4-5 seconds of blocking I/O for a 2-second
debounced autosave, serialised against the running agent.

A draft is tiny, independent of the transcript, and has no reason to share the
session file's consistency domain, so it now lives in its own sidecar::

    <SESSION_DIR>/drafts/<session_id>.json

* writes are atomic (temp file + fsync + ``os.replace``) under a per-session
  in-process lock and an advisory ``flock``, so concurrent autosaves from two
  tabs never tear the file and never drop each other's fields;
* reads are one small JSON file, never the session sidecar;
* the session JSON (messages, mtime, index) is not touched at all.

Compatibility contract
----------------------
* ``composer_draft`` inside the session JSON stays the legacy fallback: it is
  still written by ``Session.save()`` and is used whenever no sidecar exists.
* A *present* sidecar is authoritative, including an empty one. Writing an
  empty draft is an explicit clear and must not resurrect the stale inline
  copy on the next read.
* ``resolve_draft()`` is overlaid onto every session loaded through
  ``Session.load()`` / ``get_session()``, so GET session, export and reload all
  see the newest draft.
* ``delete_draft()`` is called when a session is deleted so a later session
  with the same id can never inherit an old draft.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:  # POSIX only; the WebUI server runs on Linux, tests may run elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - Windows dev boxes
    fcntl = None


DRAFTS_DIRNAME = "drafts"
SIDECAR_VERSION = 1

# Mirrors the API-level clamps in api/routes.py so direct callers (tests,
# future endpoints) cannot persist an unbounded draft either.
MAX_DRAFT_TEXT = 50_000
MAX_DRAFT_FILES = 50

# Same path-safety contract as api.models._SAFE_SID_CHARS: alphanumerics,
# underscore and hyphen only. Dots/slashes can never name a parent directory
# or hide an unexpected extension.
_SAFE_SID_CHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
)

# Bounded legacy read: the inline composer_draft object is small, but it can
# sit anywhere in a legacy session JSON (before or after the message array),
# so scan a bounded head and parse only the value window.
_INLINE_SCAN_BYTES = 4 * 1024 * 1024
_INLINE_VALUE_BYTES = 512 * 1024

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def is_safe_session_id(sid) -> bool:
    """Return True iff ``sid`` is a non-empty path-safe session id."""
    if not sid or not isinstance(sid, str):
        return False
    return all(c in _SAFE_SID_CHARS for c in sid)


def _session_dir() -> Path:
    # Imported lazily: api.models imports this module from inside functions,
    # and tests monkeypatch api.models.SESSION_DIR per test.
    from api.models import SESSION_DIR

    return Path(SESSION_DIR)


def drafts_dir(*, create: bool = False) -> Path:
    """Return the draft sidecar directory (optionally creating it)."""
    path = _session_dir() / DRAFTS_DIRNAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def sidecar_path(sid: str) -> Path:
    """Return the draft sidecar path for ``sid`` (no safety check)."""
    return drafts_dir() / f"{sid}.json"


def _lock_for(sid: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(sid)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[sid] = lock
        return lock


@contextlib.contextmanager
def _cross_process_lock(sid: str):
    """Best-effort advisory lock so two server processes cannot interleave."""
    if fcntl is None:
        yield
        return
    lock_path = drafts_dir(create=True) / f".{sid}.lock"
    handle = None
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        # Locking is an extra safety net; atomic os.replace still guarantees
        # readers never observe a torn file without it.
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass


def _normalize_draft(value) -> dict:
    """Return a JSON-safe draft dict with ``text``/``files`` normalized.

    Unknown keys from older/newer clients are preserved verbatim, matching the
    pre-sidecar route which merged into ``dict(current_draft)``.
    """
    if not isinstance(value, dict):
        return {"text": "", "files": []}
    draft = dict(value)
    text = draft.get("text")
    draft["text"] = text[:MAX_DRAFT_TEXT] if isinstance(text, str) else ""
    files = draft.get("files")
    if isinstance(files, list):
        draft["files"] = files[:MAX_DRAFT_FILES]
    else:
        draft["files"] = []
    return draft


def _read_sidecar(sid: str) -> dict | None:
    """Return the sidecar draft, or None when no sidecar exists.

    A corrupt sidecar is treated as absent (legacy inline draft wins) so a
    half-written file from an older version can never brick draft restore.
    """
    path = sidecar_path(sid)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.debug("draft sidecar unreadable for %s", sid, exc_info=True)
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("corrupt draft sidecar for %s; falling back to inline draft", sid)
        return None
    if not isinstance(payload, dict):
        return None
    draft = payload.get("draft")
    return _normalize_draft(draft if isinstance(draft, dict) else {})


def _scan_inline_draft(path: Path) -> dict:
    """Bounded scan for a legacy top-level ``composer_draft`` object."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(_INLINE_SCAN_BYTES)
    except OSError:
        return {}
    if not head:
        return {}
    markers = ('\n  "composer_draft":', '"composer_draft"')
    for marker in markers:
        idx = head.find(marker)
        if idx == -1:
            continue
        colon = head.find(":", idx + len(marker))
        if colon == -1:
            continue
        window = head[colon + 1: colon + 1 + _INLINE_VALUE_BYTES]
        try:
            value, _end = json.JSONDecoder().raw_decode(window.lstrip())
        except ValueError:
            continue
        if isinstance(value, dict):
            return _normalize_draft(value)
    return {}


def read_inline_draft(sid: str) -> dict:
    """Read the legacy inline ``composer_draft`` without parsing messages.

    Uses the same cheap metadata-prefix reader as ``Session.load_metadata_only``
    and falls back to a bounded scan for legacy layouts whose prefix stops
    before the draft key.
    """
    if not is_safe_session_id(sid):
        return {}
    path = _session_dir() / f"{sid}.json"
    if not path.exists():
        return {}
    try:
        from api.models import _read_metadata_json_prefix

        prefix = _read_metadata_json_prefix(path)
        if prefix:
            data = json.loads(prefix)
            if isinstance(data, dict) and isinstance(data.get("composer_draft"), dict):
                return _normalize_draft(data["composer_draft"])
    except Exception:
        logger.debug("inline draft prefix read failed for %s", sid, exc_info=True)
    return _scan_inline_draft(path)


def resolve_draft(sid: str, inline=None) -> dict:
    """Return the effective draft: sidecar when present, else the inline copy.

    ``inline`` lets a caller that already holds a loaded session skip a disk
    read of the session file. A sidecar that exists always wins — an empty
    sidecar means "the user cleared the draft", not "fall back to inline".
    """
    if not is_safe_session_id(sid):
        return {}
    sidecar = _read_sidecar(sid)
    if sidecar is not None:
        return sidecar
    if isinstance(inline, dict):
        return _normalize_draft(inline)
    return read_inline_draft(sid)


# Backwards/readability alias for callers that only read.
read_effective_draft = resolve_draft


def _write_sidecar(sid: str, draft: dict) -> None:
    directory = drafts_dir(create=True)
    target = directory / f"{sid}.json"
    payload = json.dumps(
        {
            "version": SIDECAR_VERSION,
            "updated_at": time.time(),
            "draft": draft,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    fd, tmp_name = tempfile.mkstemp(prefix=f".{sid}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_draft(sid: str, *, text=None, files=None, inline=None) -> tuple[dict, bool]:
    """Merge ``text``/``files`` into the draft sidecar atomically.

    Returns ``(draft, unchanged)``. When no sidecar exists yet the legacy inline
    draft is used as the merge base, so a partial update (``text`` only, or
    ``files`` only) never drops the other half of an old draft.
    """
    if not is_safe_session_id(sid):
        raise ValueError(f"Unsafe session_id {sid!r}")
    with _lock_for(sid):
        with _cross_process_lock(sid):
            current = _read_sidecar(sid)
            if current is None:
                base = inline if isinstance(inline, dict) else read_inline_draft(sid)
                current = _normalize_draft(base)
            next_draft = dict(current)
            if text is not None:
                next_draft["text"] = text[:MAX_DRAFT_TEXT] if isinstance(text, str) else ""
            if files is not None:
                if isinstance(files, list):
                    next_draft["files"] = files[:MAX_DRAFT_FILES]
                else:
                    next_draft["files"] = []
            next_draft = _normalize_draft(next_draft)
            if next_draft == current:
                return current, True
            _write_sidecar(sid, next_draft)
            return next_draft, False


def clear_draft(sid: str) -> dict:
    """Explicitly clear the draft (writes an authoritative empty sidecar)."""
    draft, _unchanged = write_draft(sid, text="", files=[])
    return draft


def delete_draft(sid: str) -> bool:
    """Remove the draft sidecar (and lock file) for a deleted session."""
    if not is_safe_session_id(sid):
        return False
    removed = False
    for path in (sidecar_path(sid), drafts_dir() / f".{sid}.lock"):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("failed to unlink draft sidecar %s", path, exc_info=True)
    return removed
