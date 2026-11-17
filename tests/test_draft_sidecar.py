"""Regression tests for the composer-draft sidecar (api/drafts.py).

The defect these cover: POST /api/session/draft ran a full get_session(), took
the per-session agent lock and called Session.save(), which re-serialised the
whole conversation (4-5s on multi-thousand-message sessions) for a debounced
autosave. Drafts now live in <SESSION_DIR>/drafts/<sid>.json and the session
JSON must stay byte-for-byte untouched.

Covered here:
  * round-trip through the real POST/GET route handlers;
  * no full Session.load / Session.save / agent lock on the autosave path;
  * concurrent partial writers never lose each other's fields or tear the file;
  * legacy inline ``composer_draft`` still works until a sidecar exists;
  * an explicit clear wins over the stale inline draft;
  * session deletion removes the sidecar;
  * a large session's draft autosave stays fast and leaves mtime unchanged.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.drafts as drafts
import api.models as models
import api.routes as routes


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield session_dir
    models.SESSIONS.clear()


def _make_session(sid="s1", messages=None, draft=None):
    session = models.Session(
        session_id=sid,
        title="Draft test",
        workspace=str(Path.cwd()),
        messages=messages if messages is not None else [{"role": "user", "content": "hi"}],
        composer_draft=draft,
    )
    session.save(skip_index=True)
    return session


def _capture(monkeypatch):
    captured = {}

    def _fake_j(handler, payload, status=200, extra_headers=None, **kwargs):
        captured["payload"] = payload
        captured["status"] = status
        return True

    import api.helpers as helpers

    monkeypatch.setattr(routes, "j", _fake_j)
    monkeypatch.setattr(helpers, "j", _fake_j)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: captured.get("body", {}))
    return captured


def _call_draft_route(monkeypatch, body=None, command="POST", query=""):
    captured = _capture(monkeypatch)
    if body is not None:
        captured["body"] = body
    handler = SimpleNamespace(
        command=command,
        _safe_webui_print=lambda *args, **kwargs: None,
    )
    parsed = SimpleNamespace(path="/api/session/draft", query=query)
    result = routes.handle_post(handler, parsed)
    return result, captured


def _session_file(store, sid="s1") -> Path:
    return store / f"{sid}.json"


# ── round-trip + untouched session file ─────────────────────────────────────

def test_draft_roundtrip_through_routes_leaves_session_file_untouched(store, monkeypatch):
    _make_session("s1")
    path = _session_file(store)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    result, captured = _call_draft_route(
        monkeypatch,
        {"session_id": "s1", "text": "hello draft", "files": [{"name": "a.txt", "size": 3}]},
    )
    assert result is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["draft"] == {
        "text": "hello draft",
        "files": [{"name": "a.txt", "size": 3}],
    }

    # The session JSON is byte-identical: no full save happened.
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime

    # Sidecar lives in its own subdirectory, so glob('*.json') session
    # enumeration (recovery, tombstones) cannot mistake it for a session.
    assert sorted(p.name for p in store.glob("*.json")) == ["s1.json"]
    sidecar = drafts.sidecar_path("s1")
    assert sidecar.exists()
    assert sidecar.parent.name == drafts.DRAFTS_DIRNAME
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["version"] == drafts.SIDECAR_VERSION
    assert payload["draft"]["text"] == "hello draft"

    # GET returns the sidecar draft.
    result, captured = _call_draft_route(
        monkeypatch, command="GET", query="session_id=s1"
    )
    assert result is True
    assert captured["payload"]["draft"]["text"] == "hello draft"

    # A cold get_session (the reload path) sees it too.
    models.SESSIONS.clear()
    session = models.get_session("s1")
    assert session.composer_draft["text"] == "hello draft"
    assert session.compact()["composer_draft"]["files"] == [{"name": "a.txt", "size": 3}]


def test_draft_autosave_never_full_loads_saves_or_takes_agent_lock(store, monkeypatch):
    _make_session("s1")
    models.SESSIONS.clear()  # force the route down the on-disk path

    def _boom(*args, **kwargs):
        raise AssertionError("draft autosave must not touch the full session path")

    monkeypatch.setattr(models.Session, "load", classmethod(_boom))
    monkeypatch.setattr(models.Session, "save", _boom)
    monkeypatch.setattr(routes, "_get_session_agent_lock", _boom)

    result, captured = _call_draft_route(
        monkeypatch, {"session_id": "s1", "text": "typed fast", "files": []}
    )
    assert result is True
    assert captured["payload"]["draft"]["text"] == "typed fast"
    assert drafts.resolve_draft("s1")["text"] == "typed fast"


def test_metadata_only_get_session_reports_sidecar_draft(store):
    _make_session("s1")
    drafts.write_draft("s1", text="meta draft", files=[])
    models.SESSIONS.clear()

    meta = models.get_session("s1", metadata_only=True)
    assert getattr(meta, "_loaded_metadata_only", False) is True
    assert meta.composer_draft == {"text": "meta draft", "files": []}


def test_in_memory_new_chat_draft_roundtrips_without_creating_session_file(store, monkeypatch):
    """A brand-new chat has no session file until its first message.

    The draft autosave must still work there (the pre-sidecar route resolved the
    session from the SESSIONS cache) and a page reload must find the draft
    through GET /api/session/draft — without materialising a zero-message file.
    """
    session = models.new_session(workspace=str(Path.cwd()))
    sid = session.session_id
    assert not (store / f"{sid}.json").exists()

    result, captured = _call_draft_route(
        monkeypatch, {"session_id": sid, "text": "new chat draft", "files": []}
    )
    assert result is True
    assert captured["payload"]["draft"]["text"] == "new chat draft"
    assert not (store / f"{sid}.json").exists()

    result, captured = _call_draft_route(monkeypatch, command="GET", query=f"session_id={sid}")
    assert result is True
    assert captured["payload"]["draft"]["text"] == "new chat draft"


def test_draft_route_404s_for_unknown_session(store, monkeypatch):
    result, captured = _call_draft_route(
        monkeypatch, {"session_id": "does-not-exist", "text": "x"}
    )
    assert result is True
    assert captured["status"] == 404
    assert not drafts.sidecar_path("does-not-exist").exists()


# ── concurrency ─────────────────────────────────────────────────────────────

def test_concurrent_partial_writers_merge_without_losing_fields(store):
    _make_session("s1")
    errors = []

    def _text_writer(index):
        try:
            for step in range(8):
                drafts.write_draft("s1", text=f"text-{index}-{step}")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def _file_writer(index):
        try:
            for step in range(8):
                drafts.write_draft("s1", files=[{"name": f"file-{index}-{step}.txt"}])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=_text_writer, args=(i,)) for i in range(3)]
    threads += [threading.Thread(target=_file_writer, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    resolved = drafts.resolve_draft("s1")
    assert resolved["text"].startswith("text-")
    assert len(resolved["files"]) == 1
    assert resolved["files"][0]["name"].startswith("file-")

    # The file is never torn: it parses after every writer finished.
    payload = json.loads(drafts.sidecar_path("s1").read_text(encoding="utf-8"))
    assert payload["draft"] == resolved


def test_reader_never_observes_a_torn_sidecar(store):
    _make_session("s1")
    stop = threading.Event()
    failures = []

    def _reader():
        path = drafts.sidecar_path("s1")
        while not stop.is_set():
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            try:
                json.loads(raw)
            except ValueError as exc:  # pragma: no cover - the defect
                failures.append(exc)
                return

    reader = threading.Thread(target=_reader)
    reader.start()
    try:
        for step in range(40):
            drafts.write_draft("s1", text="x" * (step * 50), files=[{"name": f"{step}.txt"}])
    finally:
        stop.set()
        reader.join(timeout=5)

    assert not failures


# ── legacy inline compatibility ─────────────────────────────────────────────

def test_legacy_inline_draft_is_used_until_sidecar_exists(store, monkeypatch):
    legacy = {"text": "old legacy", "files": [{"name": "old.txt"}]}
    _make_session("s1", draft=legacy)
    path = _session_file(store)
    inline_before = json.loads(path.read_text(encoding="utf-8"))["composer_draft"]
    assert inline_before == legacy

    # No sidecar yet: GET falls back to the inline copy.
    result, captured = _call_draft_route(monkeypatch, command="GET", query="session_id=s1")
    assert result is True
    assert captured["payload"]["draft"] == legacy

    # A text-only POST merges onto the legacy draft instead of dropping files.
    result, captured = _call_draft_route(monkeypatch, {"session_id": "s1", "text": "new text"})
    assert result is True
    assert captured["payload"]["draft"] == {"text": "new text", "files": [{"name": "old.txt"}]}

    models.SESSIONS.clear()
    assert models.get_session("s1").composer_draft == {
        "text": "new text",
        "files": [{"name": "old.txt"}],
    }


def test_clear_wins_over_stale_inline_draft(store, monkeypatch):
    legacy = {"text": "old legacy", "files": [{"name": "old.txt"}]}
    _make_session("s1", draft=legacy)
    path = _session_file(store)

    # Client clear path: POST {text: ""} (sessions.js _clearComposerDraft).
    result, captured = _call_draft_route(monkeypatch, {"session_id": "s1", "text": ""})
    assert result is True
    assert captured["payload"]["draft"] == {"text": "", "files": []}

    # The inline copy on disk is intentionally still the old draft (we never
    # saved the session) — but every read must prefer the empty sidecar.
    assert json.loads(path.read_text(encoding="utf-8"))["composer_draft"] == legacy
    models.SESSIONS.clear()
    assert models.get_session("s1").composer_draft == {"text": "", "files": []}
    assert models.Session.load("s1").composer_draft == {"text": "", "files": []}

    result, captured = _call_draft_route(monkeypatch, command="GET", query="session_id=s1")
    assert captured["payload"]["draft"] == {"text": "", "files": []}


# ── deletion ────────────────────────────────────────────────────────────────

def test_session_delete_removes_draft_sidecar(store, monkeypatch):
    _make_session("s1")
    drafts.write_draft("s1", text="will be deleted", files=[])
    sidecar = drafts.sidecar_path("s1")
    assert sidecar.exists()

    import api.background_process as background_process
    import api.config as config
    import api.run_journal as run_journal
    import api.terminal as terminal_module
    import api.turn_journal as turn_journal

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *a, **k: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: False)
    monkeypatch.setattr(routes, "_worktree_retained_payload_for_session_id", lambda sid: {})
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **k: None)
    monkeypatch.setattr(config, "_evict_session_agent", lambda sid: None)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)
    monkeypatch.setattr(turn_journal, "delete_turn_journal", lambda sid: None)
    monkeypatch.setattr(run_journal, "delete_run_journal", lambda sid: None)
    monkeypatch.setattr(background_process, "forget_bg_task_completion_dedup", lambda sid: None)
    monkeypatch.setattr(terminal_module, "close_terminal", lambda sid: None)

    captured = _capture(monkeypatch)
    captured["body"] = {"session_id": "s1"}
    handler = SimpleNamespace(
        command="POST",
        _safe_webui_print=lambda *args, **kwargs: None,
    )
    result = routes.handle_post(handler, SimpleNamespace(path="/api/session/delete", query=""))
    assert result is True
    assert captured["payload"]["ok"] is True

    assert not _session_file(store).exists()
    assert not sidecar.exists()
    assert not drafts.sidecar_path("s1").exists()


# ── large session performance ───────────────────────────────────────────────

def test_large_session_draft_autosave_is_fast_and_keeps_mtime(store, monkeypatch):
    body = "x" * 2000
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i}:{body}"}
        for i in range(1500)
    ]
    _make_session("big", messages=messages)
    path = _session_file(store, "big")
    size = path.stat().st_size
    assert size > 2_000_000, f"expected a multi-MB session, got {size} bytes"
    before_mtime = path.stat().st_mtime_ns
    before_bytes = path.read_bytes()

    timings = []
    for step in range(10):
        started = time.perf_counter()
        result, captured = _call_draft_route(
            monkeypatch,
            {"session_id": "big", "text": f"draft {step}", "files": []},
        )
        timings.append(time.perf_counter() - started)
        assert result is True
        assert captured["payload"]["draft"]["text"] == f"draft {step}"

    assert path.stat().st_mtime_ns == before_mtime
    assert path.read_bytes() == before_bytes
    assert max(timings) < 0.5, f"draft autosave too slow: {timings}"
    assert drafts.resolve_draft("big")["text"] == "draft 9"
