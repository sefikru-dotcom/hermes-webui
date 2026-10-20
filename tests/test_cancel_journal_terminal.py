"""Regression: Stop must leave a durable terminal row in the run journal.

``cancel_stream()`` tears the SSE channel down eagerly (STREAMS/queue) so a new
turn can start, but the journal — the only source of truth for
replay-after-reload — was never told the run ended. When the worker is wedged
(protected compression, provider stall) it never emits its own ``cancel`` frame,
so the journal has no terminal event: replay after a reload never settles and
the client keeps spinning on a run that is already dead.

Explicit Stop therefore records the terminal ``cancel`` row itself, exactly
once, without fighting the worker's own terminal frame.
"""
import io
import queue
import threading

import pytest

import api.models as models
import api.streaming as streaming
from api.config import (
    ACTIVE_RUNS,
    AGENT_INSTANCES,
    CANCEL_FLAGS,
    SESSION_AGENT_CACHE,
    SESSION_AGENT_CACHE_LOCK,
    STREAMS,
    register_active_run,
    register_stream_owner,
    unregister_stream_owner,
)
from api.run_journal import (
    RunJournalWriter,
    find_run_summary,
    latest_run_summary,
    read_run_events,
    stale_interrupted_event,
)

SESSION_ID = "sess_stop_journal"
STREAM_ID = "run_stop_journal"


class _StubSession:
    """Minimal session double: cancel_stream() only touches these fields."""

    def __init__(self, session_id, stream_id):
        self.session_id = session_id
        self.profile = "default"
        self.active_stream_id = stream_id
        self.messages = []
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.pending_user_source = None
        self.saved = 0

    def save(self):
        self.saved += 1


class _LegacyAgent:
    def __init__(self, session_id=SESSION_ID):
        self.session_id = session_id
        self.calls = []

    def interrupt(self, message=None):
        self.calls.append(message)


@pytest.fixture
def journal_env(tmp_path, monkeypatch):
    """Isolate the run journal on disk and reset all live-stream registries."""
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    STREAMS.clear()
    CANCEL_FLAGS.clear()
    AGENT_INSTANCES.clear()
    ACTIVE_RUNS.clear()
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()
    yield tmp_path
    STREAMS.clear()
    CANCEL_FLAGS.clear()
    AGENT_INSTANCES.clear()
    ACTIVE_RUNS.clear()
    unregister_stream_owner(STREAM_ID)
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()


def _seed_inflight_run(monkeypatch, *, live_stream=True, active_run=True):
    """Put one non-terminal row in the journal plus the live worker registries."""
    RunJournalWriter(SESSION_ID, STREAM_ID).append_sse_event("message", {"delta": "hi"})
    session = _StubSession(SESSION_ID, STREAM_ID)
    monkeypatch.setattr(streaming, "get_session", lambda sid: session)
    if live_stream:
        STREAMS[STREAM_ID] = queue.Queue()
        CANCEL_FLAGS[STREAM_ID] = threading.Event()
    if active_run:
        register_active_run(
            STREAM_ID, session_id=SESSION_ID, phase="running", started_at=0.0
        )
    AGENT_INSTANCES[STREAM_ID] = _LegacyAgent()
    register_stream_owner(STREAM_ID, SESSION_ID)
    return session


def _terminal_rows():
    events = read_run_events(SESSION_ID, STREAM_ID)["events"]
    return [event for event in events if event.get("terminal")]


def test_stop_records_terminal_cancel_in_run_journal(journal_env, monkeypatch):
    _seed_inflight_run(monkeypatch)

    assert streaming.cancel_stream(STREAM_ID) is True

    summary = latest_run_summary(SESSION_ID, STREAM_ID)
    assert summary["terminal"] is True, "replay has no terminal row to settle on"
    assert summary["terminal_state"] == "interrupted-by-user"

    terminals = _terminal_rows()
    assert [event["event"] for event in terminals] == ["cancel"]
    assert terminals[0]["payload"]["type"] == "cancelled"
    assert terminals[0]["payload"]["status"] == "cancelled"

    # The durable row is the replay authority now; the synthetic
    # "lost-worker-bookkeeping" fallback must not be what the client sees.
    assert stale_interrupted_event(SESSION_ID, STREAM_ID) is None
    assert find_run_summary(STREAM_ID)["terminal"] is True


def test_stop_journal_terminal_is_not_duplicated_by_worker_or_second_stop(
    journal_env, monkeypatch
):
    _seed_inflight_run(monkeypatch)

    assert streaming.cancel_stream(STREAM_ID) is True

    # The worker unwinds after the hard stop and tries to emit its own cancel
    # frame; that must not add a second terminal row to the journal.
    late_worker_row = RunJournalWriter(SESSION_ID, STREAM_ID).append_sse_event(
        "cancel", {"message": "Cancelled by user"}
    )
    assert late_worker_row is None

    # A second Stop click (stream already detached, worker still in ACTIVE_RUNS).
    assert streaming.cancel_stream(STREAM_ID) is True

    terminals = _terminal_rows()
    assert [event["event"] for event in terminals] == ["cancel"]
    assert latest_run_summary(SESSION_ID, STREAM_ID)["terminal_state"] == "interrupted-by-user"


def test_detached_wedged_run_gets_terminal_journal_row(journal_env, monkeypatch):
    """The incident shape: STREAMS already gone, worker still registered."""
    _seed_inflight_run(monkeypatch, live_stream=False)

    assert streaming.cancel_stream(STREAM_ID) is True

    summary = latest_run_summary(SESSION_ID, STREAM_ID)
    assert summary["terminal"] is True
    assert summary["terminal_state"] == "interrupted-by-user"
    assert [event["event"] for event in _terminal_rows()] == ["cancel"]


def test_replay_after_stop_delivers_cancel_terminal(journal_env, monkeypatch):
    from api import routes

    _seed_inflight_run(monkeypatch)
    assert streaming.cancel_stream(STREAM_ID) is True

    class _Handler:
        def __init__(self):
            self.wfile = io.BytesIO()

    handler = _Handler()
    assert routes._replay_run_journal(handler, STREAM_ID, after_seq=0) is True

    body = handler.wfile.getvalue().decode("utf-8")
    assert "event: cancel" in body
    # No synthetic crash notice on top of the real cancel.
    assert "event: apperror" not in body


def test_completed_run_is_not_retroactively_cancelled(journal_env, monkeypatch):
    """A Stop that loses the race to a finished turn must not add a terminal row."""
    RunJournalWriter(SESSION_ID, STREAM_ID).append_sse_event("message", {"delta": "hi"})
    RunJournalWriter(SESSION_ID, STREAM_ID).append_sse_event("done", {"session": {}})
    session = _StubSession(SESSION_ID, STREAM_ID)
    session.active_stream_id = None  # worker already finalized this run
    monkeypatch.setattr(streaming, "get_session", lambda sid: session)
    STREAMS[STREAM_ID] = queue.Queue()
    CANCEL_FLAGS[STREAM_ID] = threading.Event()
    AGENT_INSTANCES[STREAM_ID] = _LegacyAgent()
    register_stream_owner(STREAM_ID, SESSION_ID)

    assert streaming.cancel_stream(STREAM_ID) is True

    assert [event["event"] for event in _terminal_rows()] == ["done"]
    assert latest_run_summary(SESSION_ID, STREAM_ID)["terminal_state"] == "completed"
