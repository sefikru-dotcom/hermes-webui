"""LLM Wiki discovery, bounded allowlists, cache, and private-safe status."""

from __future__ import annotations

import os
from pathlib import Path
import stat as _stat
import threading
import time

_LLM_WIKI_DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/research/research-llm-wiki"
_LLM_WIKI_PAGE_DIRS = ("entities", "concepts", "comparisons", "queries")


def _llm_wiki_active_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home
        return Path(get_active_hermes_home()).expanduser()
    except Exception:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()


def _llm_wiki_env_file_path(hermes_home: Path) -> str | None:
    env_path = hermes_home / ".env"
    if not env_path.exists() or not env_path.is_file():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() != "WIKI_PATH":
                continue
            value = value.strip().strip('"').strip("'")
            return value or None
    except Exception:
        return None
    return None


def _llm_wiki_get_config_path_value(config: dict, dotted_key: str) -> str | None:
    if not isinstance(config, dict):
        return None
    if dotted_key in config and config.get(dotted_key):
        return str(config.get(dotted_key))
    cur = config
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return str(cur) if cur else None


def _llm_wiki_config_path(*, _llm_wiki_get_config_path_value) -> str | None:
    try:
        from api.config import get_config as _get_cfg
        cfg = _get_cfg()
    except Exception:
        return None
    return (
        _llm_wiki_get_config_path_value(cfg, "skills.config.wiki.path")
        or _llm_wiki_get_config_path_value(cfg, "wiki.path")
    )


# Cap WIKI walks to prevent self-DoS if WIKI_PATH points at /, /etc, /home, etc.
# Real LLM wikis have under a few thousand files; 10k is generous and catches misconfig.
_LLM_WIKI_MAX_FILES = 10000
# Cap a single served wiki page at 2 MiB so a huge/binary file can't be slurped
# wholesale into memory + a JSON response (DoS / memory-blowup guard).
_LLM_WIKI_MAX_PAGE_BYTES = 2 * 1024 * 1024
# Refuse to walk these system roots even if explicitly configured.
_LLM_WIKI_FORBIDDEN_ROOTS = frozenset(
    str(Path(p).expanduser().resolve()) for p in ("/", "/etc", "/usr", "/var", "/opt", "/sys", "/proc")
)
_WIKI_ALLOWLIST_TTL = 5.0  # seconds
_wiki_allowlist_cache: dict[str, dict[str, object]] = {}
_wiki_allowlist_cache_lock = threading.Lock()


def _llm_wiki_resolve_path(
    *,
    _llm_wiki_active_hermes_home,
    _llm_wiki_config_path,
    _llm_wiki_env_file_path,
) -> tuple[Path, str, bool]:
    hermes_home = _llm_wiki_active_hermes_home()
    raw = os.getenv("WIKI_PATH") or _llm_wiki_env_file_path(hermes_home)
    source = "WIKI_PATH" if raw else "default"
    configured = bool(raw)
    if not raw:
        raw = _llm_wiki_config_path()
        if raw:
            source = "skills.config.wiki.path"
            configured = True
    if not raw:
        raw = "~/wiki"
    return Path(os.path.expandvars(raw)).expanduser(), source, configured


def _llm_wiki_safe_iso(ts: float | None) -> str | None:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def _llm_wiki_count_files(root: Path, *, _LLM_WIKI_FORBIDDEN_ROOTS, _LLM_WIKI_MAX_FILES) -> int:
    if not root.exists() or not root.is_dir():
        return 0
    # Defense in depth: refuse to walk forbidden system roots even if WIKI_PATH
    # was set to one. The endpoint is auth-gated but a misconfigured server
    # shouldn't self-DoS by rglob'ing all of /etc on every Insights load.
    try:
        if str(root.resolve()) in _LLM_WIKI_FORBIDDEN_ROOTS:
            return 0
    except Exception:
        return 0
    count = 0
    iterated = 0
    for item in root.rglob("*"):
        iterated += 1
        if iterated > _LLM_WIKI_MAX_FILES:
            break  # bounded — prevents hangs on symlink loops or huge trees
        try:
            if item.is_file() and not any(part.startswith(".") for part in item.relative_to(root).parts):
                count += 1
        except Exception:
            continue
    return count


def _llm_wiki_page_files_cache_signature(wiki_path: Path, *, _LLM_WIKI_PAGE_DIRS) -> tuple:
    """Return a change-detection signature over the configured wiki sections.

    Reads only the section-level directories (not every page), using
    st_mtime_ns, st_dev, and st_ino so that quick section replacements and
    mtime-ns-resolution changes are both caught.  Missing or inaccessible
    section dirs are represented as ``(section, None)`` so their appearance
    or disappearance also invalidates the cache.
    """
    sig = []
    for section in _LLM_WIKI_PAGE_DIRS:
        section_dir = wiki_path / section
        try:
            st = section_dir.lstat()
        except OSError:
            sig.append((section, None))
            continue
        sig.append((section, st.st_dev, st.st_ino, st.st_mtime_ns))
    return tuple(sig)


def _llm_wiki_page_files_uncached(
    wiki_path: Path,
    *,
    _LLM_WIKI_FORBIDDEN_ROOTS,
    _LLM_WIKI_MAX_FILES,
    _LLM_WIKI_PAGE_DIRS,
) -> list[Path]:
    pages: list[Path] = []
    # Defense in depth: refuse forbidden system roots, and resolve the wiki root
    # ONCE as the single trust base for all containment checks below.
    try:
        wiki_real = wiki_path.resolve()
        if str(wiki_real) in _LLM_WIKI_FORBIDDEN_ROOTS:
            return pages
    except Exception:
        return pages

    def _is_clean_relpath(rel: Path) -> bool:
        # No dot-prefixed segment (dotfile/dotdir) anywhere in the path.
        return not any(part.startswith(".") for part in rel.parts)

    iterated = 0
    for dirname in _LLM_WIKI_PAGE_DIRS:
        section = wiki_path / dirname
        if not section.exists() or not section.is_dir():
            continue
        # The section itself must resolve UNDER the real wiki root — guards a
        # symlinked section (e.g. concepts -> /tmp/outside) from exposing files
        # outside the wiki tree entirely.
        try:
            section_real = section.resolve()
            section_real.relative_to(wiki_real)
        except (OSError, ValueError):
            continue
        for item in section.rglob("*.md"):
            iterated += 1
            if iterated > _LLM_WIKI_MAX_FILES:
                return pages  # bounded
            try:
                rel = item.relative_to(section)
                if not item.is_file() or not _is_clean_relpath(rel):
                    continue
                # Reject multi-link (hardlinked) page files. A hardlink at a
                # clean *.md name can point at an arbitrary inode (incl. one
                # outside the wiki); O_NOFOLLOW + inode-identity at read time
                # cannot tell such a hardlink apart from the real page, so
                # exclude any file with st_nlink > 1 from the allowlist. (#4375)
                try:
                    if item.lstat().st_nlink > 1:
                        continue
                except OSError:
                    continue
                # Resolve the real target and require it to live under BOTH the
                # real wiki root and the real section, with no dot-prefixed
                # segment on the resolved-relative path. This closes symlink
                # escapes whose link name looks like a clean *.md page but whose
                # target is an arbitrary / hidden / out-of-tree file (the read
                # endpoint would otherwise serve it).
                item_real = item.resolve()
                item_real.relative_to(section_real)
                rel_real = item_real.relative_to(wiki_real)
                if not _is_clean_relpath(rel_real):
                    continue
                pages.append(item)
            except (OSError, ValueError):
                continue
    return pages


def _llm_wiki_page_files(
    wiki_path: Path,
    *,
    _WIKI_ALLOWLIST_TTL,
    _llm_wiki_page_files_cache_signature,
    _llm_wiki_page_files_uncached,
    _wiki_allowlist_cache,
    _wiki_allowlist_cache_lock,
) -> list[Path]:
    """Return all allowlisted wiki page paths under *wiki_path*.

    Trust boundary: the allowlist uses ``(st_dev, st_ino)`` inode identity.
    A hardlink created at a listed page name *before* this snapshot is taken
    would carry that page's identity through to the caller's fstat check.
    This is only exploitable with write access to the wiki directory, which
    is outside the realistic threat model (the wiki directory is
    operator-controlled).  Defense-in-depth via an ``openat``-chain with
    no-follow directory fds would close the gap but is deferred — the inode
    match already defeats path-swap and symlink races at open time.
    """
    try:
        wiki_root = wiki_path.resolve()
    except OSError:
        wiki_root = wiki_path

    key = str(wiki_root)
    sig = _llm_wiki_page_files_cache_signature(wiki_root)
    now = time.monotonic()

    with _wiki_allowlist_cache_lock:
        cached = _wiki_allowlist_cache.get(key)
        if (
            cached is not None
            and cached.get("signature") == sig
            and now < cached.get("expires_at", 0)
        ):
            return list(cached["files"])

    pages = _llm_wiki_page_files_uncached(wiki_root)

    with _wiki_allowlist_cache_lock:
        _wiki_allowlist_cache[key] = {
            "signature": sig,
            "expires_at": now + _WIKI_ALLOWLIST_TTL,
            "files": tuple(pages),
        }
    return list(pages)


def _llm_wiki_clear_page_files_cache(*, _wiki_allowlist_cache, _wiki_allowlist_cache_lock) -> None:
    """Clear the allowlist cache; intended for tests only."""
    with _wiki_allowlist_cache_lock:
        _wiki_allowlist_cache.clear()


def _llm_wiki_allowlisted_entries(
    wiki_path: Path,
    *,
    _llm_wiki_page_files,
) -> dict[str, tuple[Path, tuple[int, int]]]:
    """Return listed relpaths mapped to resolved targets plus stable identity."""
    try:
        wiki_real = wiki_path.resolve()
    except OSError:
        return {}

    def _wiki_read_relpath_is_clean(rel: Path) -> bool:
        rel_text = rel.as_posix()
        return bool(rel.parts) and "\\" not in rel_text and not any(part.startswith(".") for part in rel.parts)

    entries: dict[str, tuple[Path, tuple[int, int]]] = {}
    try:
        for listed_path in _llm_wiki_page_files(wiki_real):
            try:
                rel_listed = listed_path.relative_to(wiki_real)
                if not _wiki_read_relpath_is_clean(rel_listed):
                    continue
                section_real = (wiki_real / rel_listed.parts[0]).resolve()
                section_real.relative_to(wiki_real)
                resolved_target = listed_path.resolve()
                resolved_target.relative_to(section_real)
                if not resolved_target.is_file():
                    continue
                # Reject hardlinked targets (st_nlink > 1): a multi-link file at
                # a clean page name can carry an arbitrary inode through the
                # O_NOFOLLOW + identity read check. Defense in depth alongside
                # the same rejection in the allowlist walk. (#4375)
                if resolved_target.stat().st_nlink > 1:
                    continue
                rel_resolved = resolved_target.relative_to(wiki_real)
                if not _wiki_read_relpath_is_clean(rel_resolved):
                    continue
                st0 = resolved_target.stat()
                if listed_path.resolve() != resolved_target:
                    continue
                entries[rel_listed.as_posix()] = (resolved_target, (st0.st_dev, st0.st_ino))
            except (OSError, ValueError):
                continue
    except Exception:
        return {}
    return entries


def _llm_wiki_status_file_entry_stat(wiki_path: Path, path: Path) -> os.stat_result | None:
    """Return the current top-level status-file entry metadata when it is safe to trust."""
    try:
        wiki_root = wiki_path.resolve()
    except OSError:
        wiki_root = wiki_path
    try:
        path.resolve().relative_to(wiki_root)
        st_entry = path.lstat()
    except (OSError, ValueError):
        return None
    if not _stat.S_ISREG(st_entry.st_mode):
        return None
    return st_entry


def _llm_wiki_verified_status_file_stat(
    wiki_path: Path,
    path: Path,
    *,
    _llm_wiki_status_file_entry_stat,
) -> os.stat_result | None:
    """Return identity-checked metadata for a top-level wiki status file."""
    st_entry = _llm_wiki_status_file_entry_stat(wiki_path, path)
    if st_entry is None:
        return None
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        st_open = os.fstat(fd)
        if (st_open.st_dev, st_open.st_ino) != (st_entry.st_dev, st_entry.st_ino):
            return None
        return st_open
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _llm_wiki_last_writer(
    wiki_path: Path,
    page_files: list[Path] | list[tuple[Path, tuple[int, int]]], *, _llm_wiki_status_file_entry_stat) -> str:
    """Best-effort last-writer detection for the LLM Wiki status card.

    Closes the gap left by the original panel (commit 2684d6fa, Issue #1257):
    the field was reserved as ``"last_writer": None`` with no reader wired up,
    so the UI always rendered "Not available". This helper makes the field
    useful without breaking the private-safe contract (reads only one line
    of frontmatter and one line of log.md headings, never page bodies).

    Priority:
      1. Most-recently-modified page frontmatter ``updated_by`` / ``writer`` /
         ``author`` (case-insensitive).
      2. Most recent ``log.md`` heading of the form
         ``## [YYYY-MM-DD] <action> | subject`` — returns
         ``"ai-agent (<action>)"`` so the user can see ingest vs update.
      3. Static fallback ``"ai-agent"`` so the UI never shows "Not available"
         for a configured wiki.
    """
    # #3455 review (Codex): resolve the wiki root once and require every file we
    # read to stay under it, so a symlinked .md page can't leak frontmatter from
    # outside the wiki. Also read bounded line-by-line (frontmatter only / log
    # headings only), never full page bodies, per the private-safe status contract.
    try:
        wiki_root = wiki_path.resolve()
    except Exception:
        wiki_root = wiki_path

    def _within_wiki(p: Path) -> bool:
        try:
            return p.resolve().is_relative_to(wiki_root)
        except Exception:
            return False

    # Priority 1: most recent page frontmatter (resolved-path must stay in-wiki)
    latest_page: Path | None = None
    latest_identity: tuple[int, int] | None = None
    latest_mtime = -1.0
    for item in page_files:
        candidate, identity = item if isinstance(item, tuple) else (item, None)
        if not _within_wiki(candidate):
            continue  # skip symlinks resolving outside the wiki
        try:
            st_candidate = candidate.stat()
        except Exception:
            continue
        if identity is not None and (st_candidate.st_dev, st_candidate.st_ino) != identity:
            continue
        mtime = st_candidate.st_mtime
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest_page = candidate
            latest_identity = identity
    if latest_page is not None:
        try:
            fd = os.open(str(latest_page), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if latest_identity is not None:
                    st_open = os.fstat(fd)
                    if (st_open.st_dev, st_open.st_ino) != latest_identity:
                        raise FileNotFoundError("wiki page changed after allowlist snapshot")
                with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
                    fd = None
                    first = fh.readline()
                    if first.strip() == "---":
                        # Read only the frontmatter block, bounded to a small line cap.
                        for _ in range(200):
                            line = fh.readline()
                            if line == "" or line.strip() == "---":
                                break  # EOF or end of frontmatter — never touch the body
                            stripped = line.strip()
                            lower = stripped.lower()
                            for key in ("updated_by", "writer", "author"):
                                if lower.startswith(f"{key}:"):
                                    value = stripped.split(":", 1)[1].strip()
                                    if value:
                                        return value
            finally:
                if fd is not None:
                    os.close(fd)
        except Exception:
            pass

    # Priority 2: log.md last entry action verb (heading lines only, bounded)
    log_path = wiki_path / "log.md"
    log_entry = _llm_wiki_status_file_entry_stat(wiki_path, log_path)
    if log_entry is not None:
        try:
            fd = os.open(str(log_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                st_open = os.fstat(fd)
                if (st_open.st_dev, st_open.st_ino) != (log_entry.st_dev, log_entry.st_ino):
                    raise FileNotFoundError("wiki log changed before open")
                with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
                    fd = None
                    for _ in range(5000):  # cap the heading scan
                        line = fh.readline()
                        if line == "":
                            break
                        stripped = line.strip()
                        if not stripped.startswith("## [") or "|" not in stripped:
                            continue
                        tail = stripped.split("]", 1)[1].strip() if "]" in stripped else ""
                        action = tail.split()[0] if tail else "update"
                        return f"ai-agent ({action})"
            finally:
                if fd is not None:
                    os.close(fd)
        except Exception:
            pass

    # Priority 3: never return None / "Not available" for a configured wiki
    return "ai-agent"


def _build_llm_wiki_status(
    *,
    _LLM_WIKI_DOCS_URL,
    _llm_wiki_allowlisted_entries,
    _llm_wiki_count_files,
    _llm_wiki_last_writer,
    _llm_wiki_resolve_path,
    _llm_wiki_safe_iso,
    _llm_wiki_verified_status_file_stat,
) -> dict:
    """Return private-safe LLM Wiki status metadata without reading page bodies."""
    try:
        wiki_path, path_source, path_configured = _llm_wiki_resolve_path()
        base = {
            "available": False,
            "enabled": False,
            "status": "missing",
            "entry_count": 0,
            "page_count": 0,
            "raw_source_count": 0,
            "last_updated": None,
            "last_writer": "ai-agent",
            "path_configured": path_configured,
            "path_source": path_source,
            "toggle_available": False,
            "toggle_reason": "Hermes Agent exposes WIKI_PATH/wiki.path for location, but no stable on/off config flag is currently available.",
            "docs_url": _LLM_WIKI_DOCS_URL,
        }
        if not wiki_path.exists():
            return base
        if not wiki_path.is_dir():
            base["status"] = "not_directory"
            return base

        allowlisted_entries = _llm_wiki_allowlisted_entries(wiki_path)
        verified_page_entries: list[tuple[Path, tuple[int, int], os.stat_result]] = []
        for target, identity in allowlisted_entries.values():
            try:
                st = target.stat()
            except Exception:
                continue
            if (st.st_dev, st.st_ino) != identity:
                continue
            verified_page_entries.append((target, identity, st))
        page_entries = [(target, identity) for target, identity, _ in verified_page_entries]
        page_files = [target for target, _, _ in verified_page_entries]
        status_files: list[tuple[Path, float]] = []
        for path in (wiki_path / "SCHEMA.md", wiki_path / "index.md", wiki_path / "log.md"):
            st_status = _llm_wiki_verified_status_file_stat(wiki_path, path)
            if st_status is None:
                continue
            status_files.append((path, st_status.st_mtime))
        status_files.extend((target, st.st_mtime) for target, _, st in verified_page_entries)
        latest = None
        for _, mtime in status_files:
            latest = mtime if latest is None else max(latest, mtime)

        base.update({
            "available": True,
            "enabled": True,
            "status": "ready" if page_files else "empty",
            "entry_count": len(page_files),
            "page_count": len(page_entries),
            "raw_source_count": _llm_wiki_count_files(wiki_path / "raw"),
            "last_updated": _llm_wiki_safe_iso(latest),
            "last_writer": _llm_wiki_last_writer(wiki_path, page_entries),
        })
        return base
    except Exception as exc:
        return {
            "available": False,
            "enabled": False,
            "status": "error",
            "entry_count": 0,
            "page_count": 0,
            "raw_source_count": 0,
            "last_updated": None,
            "last_writer": "ai-agent",
            "path_configured": False,
            "path_source": "unknown",
            "toggle_available": False,
            "toggle_reason": "Unable to inspect LLM Wiki status safely.",
            "docs_url": _LLM_WIKI_DOCS_URL,
            "error": type(exc).__name__,
        }
