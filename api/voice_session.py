"""Server-side voice gateway for the live voice dialog (``hermes.voice.v1``).

Transport: **SSE downstream + POST upstream**.  This is a deliberate deviation
from the original WebSocket design: the production server is a
``ThreadingHTTPServer`` built on ``BaseHTTPRequestHandler`` (server.py) with no
``aiohttp``/``websockets`` in the venv, so a hand-rolled RFC 6455 server inside
a request handler would put the live service at risk for the same result.  SSE
delivers the two properties the native client actually needs: the agent's text
as it is generated, and an immediate ``speech.stop`` on barge-in.

Routes (all device-Bearer authenticated, see api/auth.py):

    POST /api/voice/session    create/bind one voice channel for a session_id
    GET  /api/voice/stream     SSE downstream (``?voice_id=``, Last-Event-ID)
    POST /api/voice/interrupt  barge-in -> ``speech.stop``
    POST /api/voice/close      tear the channel down

The gateway owns **no chat state**.  A turn is started by the client through the
existing ``POST /api/chat/start``; the reply is streamed by the existing chat
runtime, which already appends every SSE frame to the durable run journal
(``api/run_journal.py``).  This module only *projects* that journal onto the
fixed voice vocabulary, so the voice client talks to the very same session, the
same memory and the same approvals — it is a transport adapter, not a second
agent.  Nothing here calls the agent, mutates a session, or generates a turn.

Wire format (one frame per event, flushed immediately)::

    id: <voice_id>:<seq>
    event: <name>
    data: <json>

``seq`` is a per-voice monotonic integer.  ``id`` is the resume cursor: a
reconnecting client sends it back as ``Last-Event-ID`` (or ``?last_event_id=``
for clients that cannot set headers) and the gateway replays the frames it
missed from durable storage — never from an in-memory queue.

Server events (fixed names, do not rename)::

    session.ready        once per connection: protocol, session_id, vocabulary
    transcript.partial   user speech-to-text, interim
    transcript.final     user speech-to-text, final
    agent.started        a turn/run began (per run)
    agent.text.delta     assistant text chunk — the answer is the concatenation
    tool.started         a tool call started
    tool.finished        a tool call finished
    agent.finished       terminal for the turn: reason=done|cancelled|error|stream_end
    speech.stop          stop speaking now (barge-in); idempotent

Client -> server is plain POST (no frames): the client speaks through
``/api/transcribe`` + ``/api/chat/start`` exactly as before, and barge-in through
``/api/voice/interrupt``.  ``speech.stop`` never cancels the agent's turn: it
only tells the client's TTS to stop, so the running turn keeps writing and the
client may follow up with ``/api/chat/steer``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import select
import socket
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

VOICE_PROTOCOL = "hermes.voice.v1"

#: Fixed wire vocabulary.  Legacy chat SSE names are projected onto these; the
#: names themselves are part of the Android contract and must not be renamed.
VOICE_WIRE_EVENTS = (
    "session.ready",
    "transcript.partial",
    "transcript.final",
    "agent.started",
    "agent.text.delta",
    "tool.started",
    "tool.finished",
    "agent.finished",
    "speech.stop",
)

#: Events whose payload is re-derived from the run journal on replay.
_JOURNAL_PAYLOAD_EVENTS = frozenset(
    {"agent.text.delta", "tool.started", "tool.finished", "agent.finished"}
)

_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VOICE_ID_PREFIX = "v1_"

_STATE_DIR_ENV = "HERMES_WEBUI_VOICE_STATE_DIR"

_DEFAULT_TTL_SECONDS = 1800.0
_DEFAULT_HEARTBEAT_SECONDS = 5.0
_DEFAULT_POLL_SECONDS = 0.15
_DEFAULT_MAX_STREAMS = 8
_DEFAULT_STREAM_MAX_SECONDS = 3600.0
_DEFAULT_SWEEP_SECONDS = 30.0
_DEFAULT_PERSIST_SECONDS = 2.0
_DEFAULT_MAX_PENDING = 4096
_DEFAULT_REPLAY_MAX_BYTES = 8 * 1024 * 1024
_INTERRUPT_DEBOUNCE_SECONDS = 2.0


# ── tunables (env-overridable so tests and operators can shorten them) ──────


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "")
    if not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value


def _ttl_seconds() -> float:
    return _env_float("HERMES_WEBUI_VOICE_TTL_SECONDS", _DEFAULT_TTL_SECONDS, minimum=1.0)


def _heartbeat_seconds() -> float:
    return _env_float(
        "HERMES_WEBUI_VOICE_HEARTBEAT_SECONDS", _DEFAULT_HEARTBEAT_SECONDS, minimum=0.2
    )


def _poll_seconds() -> float:
    return _env_float("HERMES_WEBUI_VOICE_POLL_SECONDS", _DEFAULT_POLL_SECONDS, minimum=0.01)


def _max_streams() -> int:
    return int(_env_float("HERMES_WEBUI_VOICE_MAX_STREAMS", _DEFAULT_MAX_STREAMS, minimum=1))


def _stream_max_seconds() -> float:
    return _env_float(
        "HERMES_WEBUI_VOICE_STREAM_MAX_SECONDS", _DEFAULT_STREAM_MAX_SECONDS, minimum=5.0
    )


def _sweep_seconds() -> float:
    return _env_float("HERMES_WEBUI_VOICE_SWEEP_SECONDS", _DEFAULT_SWEEP_SECONDS, minimum=0.5)


def _persist_seconds() -> float:
    return _env_float(
        "HERMES_WEBUI_VOICE_PERSIST_SECONDS", _DEFAULT_PERSIST_SECONDS, minimum=0.0
    )


def _max_pending() -> int:
    return int(_env_float("HERMES_WEBUI_VOICE_MAX_PENDING", _DEFAULT_MAX_PENDING, minimum=16))


def _replay_max_bytes() -> int:
    return int(
        _env_float("HERMES_WEBUI_VOICE_REPLAY_MAX_BYTES", _DEFAULT_REPLAY_MAX_BYTES, minimum=1024)
    )


# ── storage ────────────────────────────────────────────────────────────────


def _voice_state_dir() -> Path:
    """Directory holding voice-gateway state (registry + replay pointers)."""
    override = os.getenv(_STATE_DIR_ENV, "").strip()
    if override:
        return Path(override)
    try:
        import api.config as config

        return Path(config.STATE_DIR) / "voice"
    except Exception:  # pragma: no cover - config import is always available in-process
        return Path("/tmp") / "hermes-webui-voice"


def _registry_path() -> Path:
    return _voice_state_dir() / "sessions.json"


def _pointer_path(voice_id: str) -> Path:
    digest = hashlib.sha256(str(voice_id).encode("utf-8")).hexdigest()[:32]
    return _voice_state_dir() / "journal" / f"{digest}.jsonl"


def _mask_voice_id(voice_id: str) -> str:
    """Short, non-reversible-enough handle for logs (never the full id)."""
    raw = str(voice_id or "")
    if len(raw) <= 8:
        return "v***"
    return f"{raw[:5]}…{raw[-2:]}"


# ── errors ─────────────────────────────────────────────────────────────────


class VoiceError(Exception):
    """Transport-level failure carrying the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


# ── session model ──────────────────────────────────────────────────────────


class VoiceSession:
    """One live voice channel bound to one chat ``session_id``."""

    def __init__(self, voice_id: str, session_id: str, profile: str, ttl: float):
        now = time.time()
        self.voice_id = str(voice_id)
        self.session_id = str(session_id)
        self.profile = str(profile or "default")
        self.ttl = float(ttl)
        self.created_at = now
        self.last_seen_at = now
        self.expires_at = now + float(ttl)
        self.seq = 0
        self.cursor = ""
        self.interrupted_at = 0.0
        self.closed = False
        self.close_reason = ""
        self.subscribers = 0
        self.cond = threading.Condition(threading.Lock())
        #: delivery queue for connected streams only; durable replay lives in
        #: the run journal + pointer file, never here.
        self.pending: list[dict] = []

    # -- lifecycle ---------------------------------------------------------
    def touch(self) -> None:
        now = time.time()
        self.last_seen_at = now
        self.expires_at = now + self.ttl

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def expires_in(self) -> int:
        return max(0, int(round(self.expires_at - time.time())))

    def mark_closed(self, reason: str) -> None:
        with self.cond:
            self.closed = True
            self.close_reason = str(reason or "")
            self.pending.clear()
            self.cond.notify_all()

    # -- persistence -------------------------------------------------------
    def to_record(self) -> dict:
        return {
            "voice_id": self.voice_id,
            "session_id": self.session_id,
            "profile": self.profile,
            "ttl": self.ttl,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "expires_at": self.expires_at,
            "seq": self.seq,
            "cursor": self.cursor,
            "interrupted_at": self.interrupted_at,
            "closed": self.closed,
            "close_reason": self.close_reason,
        }

    @classmethod
    def from_record(cls, record: dict) -> "VoiceSession":
        voice = cls(
            str(record["voice_id"]),
            str(record["session_id"]),
            str(record.get("profile") or "default"),
            float(record.get("ttl") or _ttl_seconds()),
        )
        voice.created_at = float(record.get("created_at") or voice.created_at)
        voice.last_seen_at = float(record.get("last_seen_at") or voice.created_at)
        voice.expires_at = float(record.get("expires_at") or voice.expires_at)
        voice.seq = int(record.get("seq") or 0)
        voice.cursor = str(record.get("cursor") or "")
        voice.interrupted_at = float(record.get("interrupted_at") or 0.0)
        voice.closed = bool(record.get("closed"))
        voice.close_reason = str(record.get("close_reason") or "")
        return voice


class _VoiceRegistry:
    """In-process index over the durable voice-session registry file."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_id: dict[str, VoiceSession] = {}
        self.by_key: dict[str, VoiceSession] = {}
        self.loaded = False
        self.dirty = False
        self.last_persist = 0.0

    @staticmethod
    def key(profile: str | None, session_id: str) -> str:
        return f"{str(profile or 'default')}\x00{str(session_id)}"

    # -- load/persist ------------------------------------------------------
    def ensure_loaded_locked(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        path = _registry_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("voice: unreadable voice session registry, starting empty", exc_info=True)
            return
        try:
            data = json.loads(raw)
        except Exception:
            logger.warning("voice: corrupt voice session registry, starting empty", exc_info=True)
            return
        records = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(records, dict):
            return
        now = time.time()
        restored = 0
        for record in records.values():
            if not isinstance(record, dict):
                continue
            try:
                voice = VoiceSession.from_record(record)
            except Exception:
                continue
            if voice.closed or voice.expired(now):
                continue
            self.by_id[voice.voice_id] = voice
            self.by_key[self.key(voice.profile, voice.session_id)] = voice
            restored += 1
        if restored:
            logger.info("voice: restored %d voice session(s) from disk", restored)

    def persist_locked(self, *, force: bool = False) -> None:
        now = time.time()
        if not force:
            if not self.dirty or (now - self.last_persist) < _persist_seconds():
                return
        records = {voice.voice_id: voice.to_record() for voice in self.by_id.values()}
        path = _registry_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
            tmp.write_text(json.dumps({"version": 1, "sessions": records}), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        except Exception:
            logger.warning("voice: failed to persist voice session registry", exc_info=True)
            return
        self.dirty = False
        self.last_persist = now

    def persist_if_due(self) -> None:
        if not self.dirty:
            return
        with self.lock:
            self.persist_locked()


_REGISTRY = _VoiceRegistry()
_STREAM_SLOTS = threading.BoundedSemaphore(_max_streams())
_SWEEPER_STARTED = False
_SWEEPER_LOCK = threading.Lock()


def _new_voice_id() -> str:
    return _VOICE_ID_PREFIX + secrets.token_urlsafe(18)


def _validate_session_id(session_id) -> str:
    sid = str(session_id or "").strip()
    if not _SAFE_SESSION_ID_RE.match(sid):
        raise VoiceError(400, "session_id is required")
    return sid


def _active_profile() -> str:
    try:
        from api.profiles import get_active_profile_name

        return str(get_active_profile_name() or "default")
    except Exception:
        return "default"


def _profiles_match(left, right) -> bool:
    try:
        from api.profiles import _profiles_match as impl

        return bool(impl(left, right))
    except Exception:
        return str(left or "default") == str(right or "default")


def _load_session_metadata(session_id: str):
    """Load session metadata for ownership checks (indirection for tests)."""
    from api.models import get_session

    return get_session(session_id, metadata_only=True)


# ── pointer journal (durable "which journal row produced which voice seq") ──


def _append_pointer(voice: VoiceSession, *, seq: int, event: str, cursor: str | None, data) -> None:
    row = {"seq": int(seq), "event": str(event)}
    if cursor:
        row["cursor"] = str(cursor)
    if data is not None:
        row["data"] = data
    path = _pointer_path(voice.voice_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
    except Exception:
        logger.warning("voice: failed to append replay pointer", exc_info=True)


def _read_pointers(voice: VoiceSession, after_seq: int) -> list[dict]:
    path = _pointer_path(voice.voice_id)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning("voice: replay pointer stat failed", exc_info=True)
        return []
    if size > _replay_max_bytes():
        logger.info("voice: replay pointer file too large (%d bytes), skipping replay", size)
        return []
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                try:
                    seq = int(row.get("seq") or 0)
                except (TypeError, ValueError):
                    continue
                if seq > after_seq:
                    rows.append(row)
    except Exception:
        logger.warning("voice: replay pointer read failed", exc_info=True)
        return []
    rows.sort(key=lambda item: int(item.get("seq") or 0))
    return rows


def _last_pointer_seq(voice: VoiceSession) -> int:
    path = _pointer_path(voice.voice_id)
    try:
        size = path.stat().st_size
        if size <= 0:
            return 0
        with open(path, "rb") as handle:
            handle.seek(max(0, size - 4096))
            tail = handle.read()
    except Exception:
        return 0
    for raw in reversed(tail.splitlines()):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
            return int(row.get("seq") or 0)
        except Exception:
            continue
    return 0


def _delete_pointer(voice: VoiceSession) -> None:
    try:
        _pointer_path(voice.voice_id).unlink(missing_ok=True)
    except Exception:
        logger.debug("voice: could not delete replay pointer", exc_info=True)


# ── registry operations ────────────────────────────────────────────────────


def _ensure_sweeper() -> None:
    global _SWEEPER_STARTED
    with _SWEEPER_LOCK:
        if _SWEEPER_STARTED:
            return
        _SWEEPER_STARTED = True

    def _loop() -> None:
        while True:
            try:
                time.sleep(_sweep_seconds())
                sweep_expired()
            except Exception:  # pragma: no cover - defensive
                logger.debug("voice: sweeper iteration failed", exc_info=True)

    threading.Thread(target=_loop, name="voice-session-sweeper", daemon=True).start()


def _drop_locked(voice: VoiceSession, reason: str) -> None:
    _REGISTRY.by_id.pop(voice.voice_id, None)
    _REGISTRY.by_key.pop(_VoiceRegistry.key(voice.profile, voice.session_id), None)
    _REGISTRY.dirty = True
    voice.mark_closed(reason)
    _delete_pointer(voice)


def get_or_create_voice_session(
    session_id, *, profile: str | None = None, ttl: float | None = None
) -> tuple[VoiceSession, bool]:
    """Return the single voice channel for ``session_id`` (create if absent)."""
    sid = _validate_session_id(session_id)
    profile_name = str(profile or _active_profile())
    ttl_value = float(ttl if ttl is not None else _ttl_seconds())
    _ensure_sweeper()
    with _REGISTRY.lock:
        _REGISTRY.ensure_loaded_locked()
        key = _VoiceRegistry.key(profile_name, sid)
        voice = _REGISTRY.by_key.get(key)
        now = time.time()
        if voice is not None and (voice.closed or voice.expired(now)):
            _drop_locked(voice, "expired" if voice.expired(now) else "closed")
            voice = None
        created = voice is None
        if created:
            voice = VoiceSession(_new_voice_id(), sid, profile_name, ttl_value)
            _REGISTRY.by_id[voice.voice_id] = voice
            _REGISTRY.by_key[key] = voice
        else:
            voice.ttl = ttl_value
            voice.touch()
        # The pointer file is the durable high-water mark; the registry file is
        # persisted lazily, so reconcile seq with the pointer on (re)attach.
        voice.seq = max(int(voice.seq), _last_pointer_seq(voice))
        _REGISTRY.dirty = True
        _REGISTRY.persist_locked(force=created)
        return voice, created


def resolve_voice_session(voice_id, *, profile: str | None = None) -> VoiceSession:
    """Resolve an opaque ``voice_id`` for the caller's profile or raise."""
    vid = str(voice_id or "").strip()
    if not vid:
        raise VoiceError(400, "voice_id is required")
    profile_name = str(profile or _active_profile())
    with _REGISTRY.lock:
        _REGISTRY.ensure_loaded_locked()
        voice = _REGISTRY.by_id.get(vid)
        if voice is None:
            raise VoiceError(404, "voice session not found")
        if voice.closed:
            raise VoiceError(410, "voice session closed")
        if voice.expired():
            _drop_locked(voice, "expired")
            raise VoiceError(410, "voice session expired")
        if not _profiles_match(voice.profile, profile_name):
            # Foreign owner: never answer 200 with an empty channel.
            logger.info(
                "voice: rejected foreign voice session %s (profile mismatch)",
                _mask_voice_id(vid),
            )
            raise VoiceError(403, "voice session belongs to another profile")
        return voice


def close_voice_session(voice: VoiceSession, reason: str = "client_close") -> None:
    with _REGISTRY.lock:
        _drop_locked(voice, reason)
        _REGISTRY.persist_locked(force=True)


def sweep_expired(now: float | None = None) -> int:
    """Drop closed/expired voice sessions and their replay pointers."""
    moment = now if now is not None else time.time()
    removed = 0
    with _REGISTRY.lock:
        _REGISTRY.ensure_loaded_locked()
        for voice in list(_REGISTRY.by_id.values()):
            if voice.closed or voice.expired(moment):
                _drop_locked(voice, "expired" if voice.expired(moment) else "closed")
                removed += 1
        if removed:
            _REGISTRY.persist_locked(force=True)
    return removed


def active_voice_session_count() -> int:
    with _REGISTRY.lock:
        _REGISTRY.ensure_loaded_locked()
        return len(_REGISTRY.by_id)


# ── event recording / publishing ───────────────────────────────────────────


def record_event(
    voice: VoiceSession, event: str, data, *, cursor: str | None = None
) -> int | None:
    """Durably log one voice event and queue it for the connected stream.

    Returns the assigned ``seq`` (or ``None`` when the channel is closed).  The
    pointer row is written *before* the frame is queued so a client that
    reconnects can always replay what it missed.
    """
    if event not in VOICE_WIRE_EVENTS:
        raise ValueError(f"unknown voice event: {event}")
    payload = data if isinstance(data, dict) else {"value": data}
    with voice.cond:
        if voice.closed:
            return None
        voice.seq += 1
        seq = voice.seq
        if cursor:
            voice.cursor = str(cursor)
        voice.pending.append({"seq": seq, "event": event, "data": payload})
        overflow = len(voice.pending) - _max_pending()
        if overflow > 0:
            del voice.pending[:overflow]
            logger.debug("voice: dropped %d undelivered events (slow subscriber)", overflow)
        voice.cond.notify_all()
        _REGISTRY.dirty = True
    _append_pointer(
        voice,
        seq=seq,
        event=event,
        cursor=cursor,
        data=None if (cursor and event in _JOURNAL_PAYLOAD_EVENTS) else payload,
    )
    _REGISTRY.persist_if_due()
    return seq


def _advance_cursor(voice: VoiceSession, cursor: str | None) -> None:
    if not cursor:
        return
    with voice.cond:
        voice.cursor = str(cursor)


def publish_transcript(
    session_id, text, *, final: bool = True, profile: str | None = None
) -> bool:
    """Publish a user transcript onto the session's voice channel, if any.

    Called by the existing ``POST /api/transcribe`` handler when the client
    names its ``session_id``: the voice client then sees its own utterance come
    back on the same channel as ``transcript.partial``/``transcript.final``,
    which keeps the spoken turn and the agent's answer in one ordered stream.
    Returns ``True`` when a channel received the event.
    """
    body = str(text or "").strip()
    if not body:
        return False
    try:
        sid = _validate_session_id(session_id)
    except VoiceError:
        return False
    profile_name = str(profile or _active_profile())
    with _REGISTRY.lock:
        _REGISTRY.ensure_loaded_locked()
        voice = _REGISTRY.by_key.get(_VoiceRegistry.key(profile_name, sid))
        if voice is None or voice.closed or voice.expired():
            return False
    record_event(
        voice,
        "transcript.final" if final else "transcript.partial",
        {"text": body, "session_id": sid, "final": bool(final)},
    )
    return True


# ── projection: run-journal row -> voice events ─────────────────────────────


def _short(value, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _tool_payload(payload: dict, *, started: bool) -> dict:
    out = {"name": _short(payload.get("name"), 120)}
    preview = _short(payload.get("preview"), 400)
    if preview:
        out["preview"] = preview
    tid = _short(payload.get("tid") or payload.get("tool_call_id"), 120)
    if tid:
        out["tool_call_id"] = tid
    if not started:
        out["is_error"] = bool(payload.get("is_error"))
        duration = payload.get("duration")
        if isinstance(duration, (int, float)):
            out["duration"] = float(duration)
    return out


def _project(row: dict) -> list[tuple[str, dict]]:
    """Map one chat-runtime SSE row onto zero or more voice events."""
    name = str(row.get("event") or "")
    raw_payload = row.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    session_id = str(row.get("session_id") or payload.get("session_id") or "")
    if name == "token":
        text = str(payload.get("text") or "")
        return [("agent.text.delta", {"text": text})] if text else []
    if name == "tool":
        return [("tool.started", _tool_payload(payload, started=True))]
    if name == "tool_complete":
        return [("tool.finished", _tool_payload(payload, started=False))]
    if name == "done":
        out: dict = {"reason": "done", "session_id": session_id}
        usage = payload.get("usage")
        if isinstance(usage, dict):
            out["usage"] = usage
        terminal_state = payload.get("terminal_state")
        if terminal_state:
            out["terminal_state"] = _short(terminal_state, 64)
        return [("agent.finished", out)]
    if name == "cancel":
        return [
            (
                "agent.finished",
                {
                    "reason": "cancelled",
                    "session_id": session_id,
                    "message": _short(payload.get("message") or payload.get("reason"), 300),
                },
            )
        ]
    if name in ("apperror", "error"):
        return [
            (
                "agent.finished",
                {
                    "reason": "error",
                    "session_id": session_id,
                    "message": _short(
                        payload.get("error") or payload.get("message") or payload.get("detail"),
                        300,
                    ),
                },
            )
        ]
    if name == "stream_end":
        return [("agent.finished", {"reason": "stream_end", "session_id": session_id})]
    return []


# ── run journal discovery ──────────────────────────────────────────────────


def _journal_dir(session_id: str) -> Path:
    from api.models import SESSION_DIR
    from api.run_journal import RUN_JOURNAL_DIR_NAME

    return Path(SESSION_DIR) / RUN_JOURNAL_DIR_NAME / str(session_id)


def _latest_run_path(session_id: str) -> Path | None:
    """Newest run journal file for a session (mtime order)."""
    directory = _journal_dir(session_id)
    try:
        candidates = list(directory.glob("*.jsonl"))
    except Exception:
        return None
    if not candidates:
        return None
    best: tuple[float, Path] | None = None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except Exception:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, path)
    return best[1] if best else None


def _split_cursor(cursor: str | None) -> tuple[str, int]:
    raw = str(cursor or "")
    if not raw:
        return "", 0
    run_id, _, seq_raw = raw.rpartition(":")
    try:
        seq = int(seq_raw)
    except (TypeError, ValueError):
        return "", 0
    return run_id, max(0, seq)


class _RunTail:
    """Incremental reader for one run journal file (offset + seq filtered)."""

    def __init__(self, session_id: str):
        self.session_id = str(session_id)
        self.run_id = ""
        self.offset = 0
        self.buffer = b""
        self.last_seq = 0
        self.started_sent = False
        self.terminal_sent = False

    def _attach(self, run_id: str, cursor_run: str, cursor_seq: int) -> None:
        self.run_id = run_id
        self.offset = 0
        self.buffer = b""
        self.last_seq = cursor_seq if cursor_run == run_id else 0
        self.started_sent = False
        self.terminal_sent = False

    def poll(self, voice: VoiceSession) -> bool:
        path = _latest_run_path(self.session_id)
        if path is None:
            return False
        run_id = path.stem
        if run_id != self.run_id:
            cursor_run, cursor_seq = _split_cursor(voice.cursor)
            self._attach(run_id, cursor_run, cursor_seq)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        except Exception:
            return False
        if size < self.offset:
            # Journal rewritten/truncated under us: restart this run.
            self.offset = 0
            self.buffer = b""
        if size == self.offset:
            return False
        try:
            with open(path, "rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read(size - self.offset)
        except FileNotFoundError:
            return False
        except Exception:
            logger.debug("voice: run journal read failed", exc_info=True)
            return False
        self.offset += len(chunk)
        data = self.buffer + chunk
        lines = data.split(b"\n")
        self.buffer = lines.pop() if lines else b""
        emitted = False
        for raw in lines:
            if self._consume(voice, raw):
                emitted = True
        return emitted

    def _consume(self, voice: VoiceSession, raw: bytes) -> bool:
        if not raw.strip():
            return False
        try:
            row = json.loads(raw.decode("utf-8"))
        except Exception:
            return False
        if not isinstance(row, dict):
            return False
        try:
            seq = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            return False
        if seq <= self.last_seq:
            return False
        self.last_seq = seq
        event_id = str(row.get("event_id") or "")
        if self.terminal_sent:
            # The run already ended on this channel. Rows appended after the
            # terminal frame belong to a bookkeeping tail the client must not
            # see: a second agent.finished (or orphan text with no
            # agent.started) would desync the dialog. A *new* run arrives as a
            # new journal file and re-attaches the tail.
            _advance_cursor(voice, event_id or None)
            return False
        emitted = False
        if not self.started_sent:
            self.started_sent = True
            record_event(
                voice,
                "agent.started",
                {
                    "session_id": self.session_id,
                    "run_id": self.run_id,
                    "started_at": row.get("created_at"),
                },
                cursor=event_id or None,
            )
            emitted = True
        for wire_event, payload in _project(row):
            if wire_event == "agent.finished":
                if self.terminal_sent:
                    continue
                self.terminal_sent = True
            record_event(voice, wire_event, payload, cursor=event_id or None)
            emitted = True
        if not emitted:
            _advance_cursor(voice, event_id or None)
        return emitted


# ── SSE plumbing ───────────────────────────────────────────────────────────


class _FrameWriter:
    """Writes SSE frames and tracks the last successful flush."""

    def __init__(self, handler, voice: VoiceSession):
        self._handler = handler
        self._voice = voice
        self.last_write = time.monotonic()

    def _write(self, payload: bytes) -> None:
        wfile = self._handler.wfile
        wfile.write(payload)
        wfile.flush()
        self.last_write = time.monotonic()

    def comment(self, text: str = "keepalive") -> None:
        self._write(f": {text}\n\n".encode("utf-8"))

    def raw(self, text: str) -> None:
        self._write(text.encode("utf-8"))

    def emit(self, event: str, data, seq: int) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        frame = (
            f"id: {self._voice.voice_id}:{int(seq)}\n"
            f"event: {event}\n"
            f"data: {body}\n\n"
        )
        self._write(frame.encode("utf-8"))


def _peer_gone(handler) -> bool:
    """True when the client has closed its half of the connection.

    SSE is a one-way GET with no request body, so any readable state that peeks
    as EOF means the peer is gone. Without this the subscriber slot stays taken
    until the next write fails — up to a full heartbeat — and a client that
    reconnects right after a network blip would get 409 instead of resuming.
    """
    sock = getattr(handler, "connection", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    try:
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


def _drain_pending(voice: VoiceSession, writer: _FrameWriter) -> bool:
    with voice.cond:
        items = voice.pending
        if not items:
            return False
        voice.pending = []
    for item in items:
        writer.emit(item["event"], item["data"], item["seq"])
    return True


def _replay(voice: VoiceSession, after_seq: int, writer: _FrameWriter) -> int:
    """Replay durable frames the client missed, using their original seqs.

    Returns the highest replayed ``seq`` (0 when nothing was replayed) so the
    caller can drop the same frames from the in-memory delivery queue.
    """
    rows = _read_pointers(voice, after_seq)
    if not rows:
        return 0
    journal_cache: dict[str, dict[int, dict]] = {}
    sent = 0
    for row in rows:
        event = str(row.get("event") or "")
        if event not in VOICE_WIRE_EVENTS:
            continue
        try:
            seq = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        data = None
        if event == "agent.started":
            run_id, _seq = _split_cursor(row.get("cursor"))
            data = {"session_id": voice.session_id, "run_id": run_id}
        elif isinstance(row.get("data"), dict):
            data = row["data"]
        else:
            run_id, journal_seq = _split_cursor(row.get("cursor"))
            if not run_id or journal_seq <= 0:
                continue
            data = _journal_payload(voice, run_id, journal_seq, event, journal_cache)
            if data is None:
                continue
        writer.emit(event, data, seq)
        sent = max(sent, seq)
    return sent


def _journal_payload(
    voice: VoiceSession,
    run_id: str,
    journal_seq: int,
    wire_event: str,
    cache: dict[str, dict[int, dict]],
) -> dict | None:
    """Re-derive one frame's payload from the durable run journal."""
    bucket = cache.get(run_id)
    if bucket is None:
        bucket = {}
        try:
            from api.run_journal import read_run_events

            result = read_run_events(voice.session_id, run_id)
            for row in result.get("events") or []:
                try:
                    bucket[int(row.get("seq") or 0)] = row
                except (TypeError, ValueError):
                    continue
        except Exception:
            logger.info("voice: run journal unavailable for replay (%s)", _mask_voice_id(voice.voice_id))
            bucket = {}
        cache[run_id] = bucket
    row = bucket.get(int(journal_seq))
    if not isinstance(row, dict):
        return None
    for candidate, payload in _project(row):
        if candidate == wire_event:
            return payload
    return None


class _ResumePlan:
    __slots__ = ("requested", "status", "after_seq")

    def __init__(self, requested: bool, status: str, after_seq: int | None = None):
        self.requested = bool(requested)
        self.status = str(status)
        self.after_seq = after_seq


def _plan_resume(voice: VoiceSession, last_event_id: str) -> _ResumePlan:
    raw = str(last_event_id or "").strip()
    if not raw:
        return _ResumePlan(False, "fresh")
    voice_id, _, seq_raw = raw.rpartition(":")
    if not voice_id or not seq_raw.isdigit():
        return _ResumePlan(True, "cursor_invalid")
    if voice_id != voice.voice_id:
        return _ResumePlan(True, "cursor_ignored")
    client_seq = int(seq_raw)
    with voice.cond:
        current = voice.seq
        if client_seq > current:
            # Server restarted with a stale registry: trust the client cursor so
            # new frames never reuse an id the client already applied.
            voice.seq = client_seq
    if client_seq > current:
        return _ResumePlan(True, "cursor_ahead", client_seq)
    return _ResumePlan(True, "ok", client_seq)


def _serve_stream(handler, voice: VoiceSession, query: dict) -> bool:
    from api.helpers import _CLIENT_DISCONNECT_ERRORS

    last_event_id = str(handler.headers.get("Last-Event-ID") or "").strip()
    if not last_event_id:
        last_event_id = str((query.get("last_event_id") or [""])[0] or "").strip()

    plan = _plan_resume(voice, last_event_id)

    from api.sse_chunked import end_sse_headers
    from api.streaming import _sse_set_write_deadline

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    end_sse_headers(handler)
    _sse_set_write_deadline(handler)

    writer = _FrameWriter(handler, voice)
    try:
        writer.raw("retry: 2000\n\n")
        if plan.after_seq is None:
            # Fresh channel: start at the durable high-water mark and drop the
            # undelivered queue. Those events predate this connection, and
            # replaying them after session.ready would put ids on the wire that
            # are lower than (or equal to) the ready frame's — an SSE client
            # dedupes and orders by id, so it would silently lose frames.
            high = max(int(voice.seq), _last_pointer_seq(voice))
            with voice.cond:
                voice.seq = high
                voice.pending = []
            ready_seq = high
        else:
            ready_seq = plan.after_seq
        writer.emit(
            "session.ready",
            {
                "protocol": VOICE_PROTOCOL,
                "voice_id": voice.voice_id,
                "session_id": voice.session_id,
                "server_events": list(VOICE_WIRE_EVENTS),
                "keepalive_seconds": int(round(_heartbeat_seconds())),
                "resume_requested": plan.requested,
                "resume_status": plan.status,
            },
            ready_seq,
        )
        if plan.after_seq is not None:
            replayed = _replay(voice, plan.after_seq, writer)
            if replayed:
                # Those frames are now on the wire; drop the same seqs from the
                # delivery queue or the loop below would emit them a second time.
                with voice.cond:
                    voice.pending = [
                        item for item in voice.pending if int(item.get("seq") or 0) > replayed
                    ]
                logger.info(
                    "voice: replayed frames up to seq %d after Last-Event-ID on %s",
                    replayed,
                    _mask_voice_id(voice.voice_id),
                )
        tail = _RunTail(voice.session_id)
        deadline = time.monotonic() + _stream_max_seconds()
        while True:
            if voice.closed:
                break
            if voice.expired():
                close_voice_session(voice, "expired")
                break
            if _peer_gone(handler):
                break
            if time.monotonic() >= deadline:
                logger.info("voice: stream lifetime cap reached, closing")
                break
            wrote = _drain_pending(voice, writer)
            try:
                if tail.poll(voice):
                    wrote = True
            except Exception:
                logger.warning("voice: journal tail failed", exc_info=True)
            if wrote:
                continue
            if (time.monotonic() - writer.last_write) >= _heartbeat_seconds():
                writer.comment()
            with voice.cond:
                if voice.pending or voice.closed:
                    continue
                voice.cond.wait(timeout=_poll_seconds())
    except _CLIENT_DISCONNECT_ERRORS:
        pass
    finally:
        _REGISTRY.persist_if_due()
    return True


# ── HTTP handlers ──────────────────────────────────────────────────────────


def _read_json(handler) -> dict:
    from api.helpers import read_body

    try:
        return read_body(handler)
    except ValueError as exc:
        raise VoiceError(400, str(exc)) from None


def _require_same_session(voice: VoiceSession, payload: dict) -> None:
    """A supplied ``session_id`` must match the voice channel's owner."""
    supplied = str(payload.get("session_id") or "").strip()
    if supplied and supplied != voice.session_id:
        logger.info(
            "voice: rejected cross-session access on %s", _mask_voice_id(voice.voice_id)
        )
        raise VoiceError(403, "voice session does not belong to this session")


def _session_owner_profile(session_id: str) -> str | None:
    """Profile that owns ``session_id``, or ``None`` when it cannot be loaded."""
    try:
        session = _load_session_metadata(session_id)
    except Exception:
        return None
    return str(getattr(session, "profile", None) or "default")


def handle_voice_session_create(handler) -> bool:
    """``POST /api/voice/session`` -> ``{voice_id, session_id, expires_in}``."""
    from api.helpers import bad, j

    try:
        payload = _read_json(handler)
        sid = _validate_session_id(payload.get("session_id"))
        profile = _active_profile()
        owner = _session_owner_profile(sid)
        if owner is None:
            bad(handler, "unknown session", 404)
            return True
        if not _profiles_match(owner, profile):
            logger.info("voice: refused voice session for a foreign session")
            bad(handler, "Session not found", 404)
            return True
        voice, _created = get_or_create_voice_session(sid, profile=profile)
        return j(
            handler,
            {
                "voice_id": voice.voice_id,
                "session_id": voice.session_id,
                "expires_in": voice.expires_in(),
            },
        )
    except VoiceError as exc:
        bad(handler, exc.message, exc.status)
        return True


def handle_voice_stream(handler, parsed) -> bool:
    """``GET /api/voice/stream?voice_id=…`` — SSE downstream channel."""
    from api.helpers import bad

    query = parse_qs(parsed.query or "")
    voice_id = str((query.get("voice_id") or [""])[0] or "").strip()
    try:
        voice = resolve_voice_session(voice_id, profile=_active_profile())
    except VoiceError as exc:
        bad(handler, exc.message, exc.status)
        return True
    with voice.cond:
        busy = voice.subscribers > 0
        if not busy:
            voice.subscribers += 1
    if busy:
        bad(handler, "voice channel already has an active stream", 409)
        return True
    if not _STREAM_SLOTS.acquire(blocking=False):
        with voice.cond:
            voice.subscribers = max(0, voice.subscribers - 1)
        bad(handler, "too many concurrent voice streams", 503)
        return True
    try:
        voice.touch()
        return _serve_stream(handler, voice, query)
    finally:
        with voice.cond:
            voice.subscribers = max(0, voice.subscribers - 1)
            voice.cond.notify_all()
        _STREAM_SLOTS.release()


def handle_voice_interrupt(handler) -> bool:
    """``POST /api/voice/interrupt`` — barge-in: emit ``speech.stop``.

    Idempotent by design: a repeat call never breaks the channel and simply
    refreshes the stop.  The agent's turn is *not* cancelled here — a barge-in
    is a steer, not a cancel; the client decides whether to follow up with
    ``/api/chat/steer``.
    """
    from api.helpers import bad, j

    try:
        payload = _read_json(handler)
        voice = resolve_voice_session(payload.get("voice_id"), profile=_active_profile())
        _require_same_session(voice, payload)
        now = time.time()
        with voice.cond:
            already = bool(voice.interrupted_at and (now - voice.interrupted_at) < _INTERRUPT_DEBOUNCE_SECONDS)
            voice.interrupted_at = now
        run_id, _seq = _split_cursor(voice.cursor)
        stop_payload: dict = {
            "reason": "interrupt",
            "at": round(now, 3),
            "run_id": run_id,
            "session_id": voice.session_id,
        }
        record_event(voice, "speech.stop", stop_payload)
        voice.touch()
        _REGISTRY.persist_if_due()
        return j(
            handler,
            {
                "ok": True,
                "voice_id": voice.voice_id,
                "interrupted": True,
                "already_interrupted": already,
            },
        )
    except VoiceError as exc:
        bad(handler, exc.message, exc.status)
        return True


def handle_voice_close(handler) -> bool:
    """``POST /api/voice/close`` — tear the channel down and free its state."""
    from api.helpers import bad, j

    try:
        payload = _read_json(handler)
        voice = resolve_voice_session(payload.get("voice_id"), profile=_active_profile())
        _require_same_session(voice, payload)
        voice_id = voice.voice_id
        close_voice_session(voice, "client_close")
        return j(handler, {"ok": True, "voice_id": voice_id, "closed": True})
    except VoiceError as exc:
        bad(handler, exc.message, exc.status)
        return True
