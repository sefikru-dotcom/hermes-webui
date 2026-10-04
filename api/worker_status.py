"""Persistent, profile-scoped delegated-worker status for the sidebar."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from api.config import STATE_DIR

_LOCK = threading.RLock()
_FILENAME = "worker_status.json"
_TERMINAL = {"completed", "failed", "stopped", "interrupted"}


def _path(profile: str | None = None) -> Path:
    # STATE_DIR is already profile-scoped in isolated deployments; retain the
    # profile suffix for shared state directories to avoid cross-profile bleed.
    name = str(profile or "").strip()
    if name and name not in {"default", "root"}:
        import hashlib
        suffix = hashlib.sha256(name.encode()).hexdigest()[:16]
        return STATE_DIR / f"worker_status_{suffix}.json"
    return STATE_DIR / _FILENAME


def _load(profile: str | None = None) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path(profile).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(data: dict[str, dict[str, Any]], profile: str | None = None) -> None:
    path = _path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def update_worker_status(*, event_type: str, child_session_id: str | None = None,
                         delegation_id: str | None = None, goal: str | None = None,
                         status: str | None = None, duration_seconds: float | None = None,
                         failure_reason: str | None = None, last_action: str | None = None, profile: str | None = None) -> dict | None:
    sid = str(child_session_id or "").strip()
    if not sid:
        return None
    event = str(event_type or "").strip().lower()
    if event.endswith(".start"):
        state = "running"
    elif event.endswith(".complete"):
        raw = str(status or "").strip().lower()
        state = "failed" if raw in {"error", "failed", "failure", "timeout"} else "stopped" if raw in {"stopped", "cancelled", "canceled", "interrupted"} else "completed" if raw in {"completed", "success"} else "unknown"
    else:
        state = None
    with _LOCK:
        data = _load(profile)
        item = dict(data.get(sid) or {})
        item.update({"child_session_id": sid, "updated_at": time.time()})
        if delegation_id: item["delegation_id"] = str(delegation_id)
        if goal: item["goal"] = str(goal)[:240]
        if last_action: item["last_action"] = str(last_action)[:80]
        if state and not (item.get("status") in _TERMINAL and state == "running"):
            item["status"] = state
        if state == "running": item.setdefault("started_at", time.time())
        if event in {"subagent.tool", "subagent.text", "subagent.progress"} and item.get("status") not in _TERMINAL:
            item["status"] = "running"
            item.setdefault("started_at", time.time())
        item["owner_pid"] = os.getpid()
        if event.endswith(".complete"): item["ended_at"] = time.time()
        if duration_seconds is not None:
            try: item["duration_seconds"] = max(0.0, float(duration_seconds))
            except (TypeError, ValueError): pass
        if failure_reason: item["failure_reason"] = str(failure_reason)[:240]
        data[sid] = item
        _save(data, profile)
        return item


def statuses(profile: str | None = None) -> dict[str, dict[str, Any]]:
    with _LOCK:
        return _load(profile)


def attach_worker_status(rows: list[dict], profile: str | None = None) -> list[dict]:
    state = statuses(profile)
    for row in rows:
        sid = str(row.get("session_id") or "")
        if sid in state:
            item = dict(state[sid])
            if item.get("status") == "running" and item.get("owner_pid") != os.getpid():
                item["status"] = "unknown"
            row["worker_status"] = item
    return rows
