"""Regression: an explicit user Stop must hard-interrupt the agent.

Incident evidence (session 42ff87f5ac64, run e4c2a85f...): ``/api/chat/cancel``
answered HTTP 200 ``cancelled: true`` at 23:16:31, yet the worker stayed inside
protected compression until 23:19:11 — every ``/api/chat/start`` in that window
answered 409. Root cause: the cancel path used the *soft* ``agent.interrupt()``
ABI, which only flips the cooperative flag; protected compression ignores it and
keeps the provider call running (``aux_interrupt_protection``), while ACTIVE_RUNS
keeps ``phase="cancelling"`` until the worker's finally.

The correct ABI is the hard stop: ``agent.hard_interrupt(...)`` (which sets
``_hard_interrupt_requested``, the gate compression checks) reached through
``agent.interrupt_compat.request_hard_interrupt`` so agents that only implement
the legacy ``interrupt(message)`` ABI keep working.
"""
import queue
import threading
import time

from api.config import (
    ACTIVE_RUNS,
    AGENT_INSTANCES,
    CANCEL_FLAGS,
    SESSION_AGENT_CACHE,
    SESSION_AGENT_CACHE_LOCK,
    STREAMS,
)
from api.streaming import cancel_stream

STREAM_ID = "run_stop_compression"


class _HardAwareAgent:
    """Stand-in for AIAgent's interrupt ABI (``agent.interrupt_control``)."""

    def __init__(self):
        self.session_id = "sess_stop_compression"
        self._hard_interrupt_requested = threading.Event()
        self.soft_calls = []
        self.hard_calls = []
        self.compression_released = threading.Event()
        self.compression_saw_hard = False

    def interrupt(self, message=None, *, hard_cancel=False, tool_reason=None):
        self.soft_calls.append(message)
        if hard_cancel:
            self._hard_interrupt_requested.set()

    def hard_interrupt(self, message=None, *, tool_reason=None):
        self.hard_calls.append(message)
        self._hard_interrupt_requested.set()

    def run_protected_compression(self, deadline=3.0):
        """Mirror ``conversation_compression._run_summary_dispatch``.

        The protected auxiliary call ignores the soft interrupt and aborts only
        when ``_hard_interrupt_requested`` is set.
        """
        self.compression_saw_hard = self._hard_interrupt_requested.wait(deadline)
        self.compression_released.set()


class _LegacyAgent:
    """Third-party / old agent double exposing only ``interrupt(message)``."""

    def __init__(self):
        self.session_id = "sess_stop_compression_legacy"
        self.calls = []

    def interrupt(self, message=None):
        self.calls.append(message)


def _reset_stream_state():
    STREAMS.clear()
    CANCEL_FLAGS.clear()
    AGENT_INSTANCES.clear()
    ACTIVE_RUNS.clear()
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()


def setup_function(_fn):
    _reset_stream_state()


def teardown_function(_fn):
    _reset_stream_state()


def _register_live_stream(stream_id, agent):
    STREAMS[stream_id] = queue.Queue()
    CANCEL_FLAGS[stream_id] = threading.Event()
    AGENT_INSTANCES[stream_id] = agent


def test_stop_hard_interrupts_agent_inside_protected_compression():
    agent = _HardAwareAgent()
    _register_live_stream(STREAM_ID, agent)

    worker = threading.Thread(target=agent.run_protected_compression, daemon=True)
    worker.start()
    # Let the compression gate settle into its protected wait before Stop lands.
    time.sleep(0.05)

    assert cancel_stream(STREAM_ID) is True

    assert agent.compression_released.wait(1.0), (
        "Stop did not release protected compression — the worker keeps running "
        "and /api/chat/start stays 409 until the 180s unwind ceiling"
    )
    assert agent.compression_saw_hard is True
    assert agent.hard_calls == ["Cancelled by user"], (
        "explicit Stop must use the hard interrupt ABI, got %r" % (agent.hard_calls,)
    )
    assert agent.soft_calls == [], (
        "soft interrupt() alone cannot stop protected compression, got %r"
        % (agent.soft_calls,)
    )
    worker.join(timeout=1.0)
    assert not worker.is_alive()


def test_soft_only_interrupt_does_not_release_protected_compression():
    """Control: the legacy soft ABI is exactly what wedged the incident run."""

    class _SoftOnlyAgent:
        def __init__(self):
            self.session_id = "sess_stop_compression_soft"
            self.calls = []

        def interrupt(self, message=None):
            self.calls.append(message)

        def run_protected_compression(self, deadline=0.4):
            # No hard gate exists, so the protected call runs to its deadline.
            self.released_after = time.monotonic()
            time.sleep(deadline)
            self.released_after = time.monotonic() - self.released_after

    agent = _SoftOnlyAgent()
    _register_live_stream(STREAM_ID, agent)
    worker = threading.Thread(target=agent.run_protected_compression, daemon=True)
    worker.start()
    time.sleep(0.05)

    assert cancel_stream(STREAM_ID) is True
    worker.join(timeout=2.0)

    assert agent.calls == ["Cancelled by user"]
    assert agent.released_after >= 0.3


def test_stop_falls_back_to_legacy_interrupt_abi():
    agent = _LegacyAgent()
    _register_live_stream(STREAM_ID, agent)

    assert cancel_stream(STREAM_ID) is True

    assert agent.calls == ["Cancelled by user"], (
        "agents without hard_interrupt must still receive interrupt(message)"
    )
