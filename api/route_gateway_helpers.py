"""Gateway identity and lifecycle helpers extracted from api.routes.

Keeps URL/JSON handlers in api.routes so existing monkeypatch tests and
status payload contracts stay on the route module. This module owns the
messaging identity cache and the profile-scoped gateway CLI runner.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from api.agent_sessions import MESSAGING_SOURCES

logger = logging.getLogger(__name__)

_MESSAGING_RAW_SOURCES = {str(s).strip().lower() for s in MESSAGING_SOURCES}
_MESSAGING_SESSION_METADATA_CACHE: dict[str, object] = {
    "path": None,
    "mtime": None,
    "identity": {},
}
_MESSAGING_SESSION_METADATA_LOCK = threading.Lock()

_GATEWAY_LIFECYCLE_TIMEOUT_SECONDS = 60

# Server-side single-flight guard for gateway lifecycle actions. The client
# disables its button while a request is in flight, but a scripted authed
# client could still fire overlapping start/stop/restart calls, spawning
# concurrent `hermes gateway` subprocesses. Serialize them here (mirrors the
# self-update _apply_lock pattern): a non-blocking acquire returns 409 on
# contention rather than launching a second overlapping subprocess.
_GATEWAY_ACTION_LOCK = threading.Lock()


def _normalize_messaging_source(raw_source) -> str:
    return str(raw_source or "").strip().lower()


def _is_known_messaging_source(raw_source) -> bool:
    return _normalize_messaging_source(raw_source) in _MESSAGING_RAW_SOURCES


def _safe_first(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _gateway_session_metadata_path():
    try:
        from api.profiles import get_active_hermes_home

        hermes_home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        hermes_home = (
            Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
            .expanduser()
            .resolve()
        )
    return hermes_home / "sessions" / "sessions.json"


def _load_gateway_session_identity_map() -> dict[str, dict]:
    path = _gateway_session_metadata_path()
    if not path.exists():
        return {}

    try:
        st = path.stat()
        cache = _MESSAGING_SESSION_METADATA_CACHE
        with _MESSAGING_SESSION_METADATA_LOCK:
            if cache["path"] == str(path) and cache["mtime"] == st.st_mtime:
                return cache["identity"].copy()
    except Exception:
        return {}

    try:
        raw_sessions = json.loads(path.read_text(encoding="utf-8"))
    except Exception as _json_err:
        logger.debug(
            "Failed to parse gateway sessions metadata from %s: %s", path, _json_err
        )
        return {}

    mapping: dict[str, dict] = {}
    if isinstance(raw_sessions, dict):
        for _entry in raw_sessions.values():
            if not isinstance(_entry, dict):
                continue
            session_id = _safe_first(_entry.get("session_id"))
            if not session_id:
                continue
            origin = (
                _entry.get("origin") if isinstance(_entry.get("origin"), dict) else {}
            )
            platform = _safe_first(origin.get("platform"), _entry.get("platform"))
            mapping[session_id] = {
                "session_key": _safe_first(
                    _entry.get("session_key"), _entry.get("key")
                ),
                "chat_id": _safe_first(origin.get("chat_id"), _entry.get("chat_id")),
                "thread_id": _safe_first(
                    origin.get("thread_id"), _entry.get("thread_id")
                ),
                "chat_type": _safe_first(
                    origin.get("chat_type"), _entry.get("chat_type")
                ),
                "user_id": _safe_first(origin.get("user_id"), _entry.get("user_id")),
                "platform": platform,
                "raw_source": platform,
            }

    with _MESSAGING_SESSION_METADATA_LOCK:
        _MESSAGING_SESSION_METADATA_CACHE["path"] = str(path)
        _MESSAGING_SESSION_METADATA_CACHE["mtime"] = st.st_mtime
        _MESSAGING_SESSION_METADATA_CACHE["identity"] = mapping
    return mapping.copy()


def _run_gateway_lifecycle_command(action: str) -> subprocess.CompletedProcess:
    if action not in {"start", "stop", "restart"}:
        raise ValueError("unsupported gateway action")

    from api import config as api_config
    from api.profiles import get_active_profile_name

    agent_dir = getattr(api_config, "_AGENT_DIR", None)
    if not agent_dir:
        raise FileNotFoundError("Hermes agent checkout not found")
    agent_dir = Path(agent_dir).expanduser().resolve()
    main_py = agent_dir / "hermes_cli" / "main.py"
    if not main_py.exists():
        raise FileNotFoundError("Hermes agent CLI entrypoint not found")

    cmd = [str(getattr(api_config, "PYTHON_EXE", sys.executable)), str(main_py)]
    profile_name = ""
    try:
        profile_name = str(get_active_profile_name() or "").strip()
    except Exception as exc:
        logger.debug("Could not resolve active profile for gateway lifecycle: %s", exc)
    if profile_name and profile_name != "default":
        cmd.extend(["--profile", profile_name])
    cmd.extend(["gateway", action])

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("BROWSER", "echo")
    return subprocess.run(
        cmd,
        cwd=str(agent_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=_GATEWAY_LIFECYCLE_TIMEOUT_SECONDS,
    )
