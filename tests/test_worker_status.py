import json
import os
import subprocess
from pathlib import Path


def test_worker_status_persists_lifecycle_atomically(tmp_path, monkeypatch):
    from api import worker_status
    monkeypatch.setattr(worker_status, "STATE_DIR", tmp_path)
    worker_status.update_worker_status(
        event_type="subagent.start", child_session_id="child-1",
        delegation_id="d1", goal="inspect code", profile="default",
    )
    worker_status.update_worker_status(
        event_type="subagent.complete", child_session_id="child-1",
        status="failed", duration_seconds=12.4, failure_reason="boom",
        profile="default",
    )
    got = worker_status.statuses()["child-1"]
    assert got["status"] == "failed"
    assert got["duration_seconds"] == 12.4
    assert got["goal"] == "inspect code"
    assert not list(tmp_path.glob(".*worker_status*"))


def test_unknown_legacy_worker_is_not_completed_from_ended_at(tmp_path, monkeypatch):
    from api import worker_status
    monkeypatch.setattr(worker_status, "STATE_DIR", tmp_path)
    worker_status.update_worker_status(event_type="unrecognized", child_session_id="old")
    assert worker_status.statuses()["old"]["status"] if "status" in worker_status.statuses()["old"] else True
    assert "status" not in worker_status.statuses()["old"]


def test_sidebar_contract_and_ui_logic():
    root = Path(__file__).parents[1]
    cache = (root / "api/route_session_list_cache.py").read_text()
    js = (root / "static/sessions.js").read_text()
    assert '"worker_status"' in cache
    assert "worker_status_${status}" in js
    assert "Status unknown" in js
    assert "duration_seconds" in js


def test_static_js_syntax():
    root = Path(__file__).parents[1]
    subprocess.run(["node", "--check", str(root / "static/sessions.js")], check=True)
