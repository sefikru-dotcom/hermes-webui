"""Regression tests: provider-wait progress must reach the WebUI SSE stream.

Before the fix ``_run_agent_streaming`` never passed ``thinking_callback`` into
``AIAgent``. ``AIAgent._emit_wait_notice`` rewrites the *live status line*
through that callback ("⏳ waiting on <model> — 90s with no response yet",
"⚠ no response from provider in 60s — reconnecting...", "⏳ waiting on provider —
retrying in 30s (attempt 1/5)") and the CLI, TUI and Desktop all render it — the
WebUI bound nothing, so every honest wait/recovery notice was dropped and a slow
provider looked like an anonymous spinner for minutes.

The fix binds ``thinking_callback`` and routes ONLY the agent's honest
wait/recovery notices into the existing ``warning`` SSE event
(``type: 'provider_wait'``) — the transient composer-status surface the
frontend already renders for retry/fallback lines. The cosmetic CLI spinner
text, reasoning content and secret-shaped values must never leak into it, and
the stream must not be spammed by repeated heartbeats.

``_agent_status_callback`` also used to drop every ``kind == 'warn'`` status
(the agent's own user-visible warnings: context-overflow blocked, compression
summary failed, auxiliary task failed), so a degraded turn looked healthy. Those
now surface as ``warning`` / ``type: 'agent_warn'``.
"""

from __future__ import annotations

import queue
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session


ROOT = Path(__file__).resolve().parents[1]

HEARTBEAT_NOTICE = (
    "⏳ waiting on test-model — 61s with no response yet "
    "(provider may be slow or overloaded; auto-reconnect at 180s)"
)
RECONNECT_NOTICE = "⚠ no response from provider in 60s — reconnecting..."
RETRY_NOTICE = "⏳ waiting on provider — retrying in 30s (attempt 1/5)"
CONTINUATION_NOTICE = (
    "↻ model returned reasoning with no final answer — asking it to continue (1/3)"
)
# Real quiet-mode spinner text (agent/display.py THINKING_VERBS) written through
# the same callback before every request — cosmetic, must never reach the WebUI.
CLI_SPINNER_TEXTS = (
    "🌸 pondering...",
    "✨ processing...",
    "🌙 reasoning...",
    "💫 cogitating...",
    "🪐 musing...",
)


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    for store in (
        config.STREAMS,
        config.CANCEL_FLAGS,
        config.AGENT_INSTANCES,
        config.STREAM_PARTIAL_TEXT,
        getattr(config, "STREAM_REASONING_TEXT", {}),
        getattr(config, "STREAM_LIVE_TOOL_CALLS", {}),
        getattr(config, "SESSION_AGENT_CACHE", {}),
        getattr(config, "SESSION_AGENT_LOCKS", {}),
    ):
        store.clear()
    yield
    for store in (
        config.STREAMS,
        config.CANCEL_FLAGS,
        config.AGENT_INSTANCES,
        config.STREAM_PARTIAL_TEXT,
        getattr(config, "STREAM_REASONING_TEXT", {}),
        getattr(config, "STREAM_LIVE_TOOL_CALLS", {}),
        getattr(config, "SESSION_AGENT_CACHE", {}),
        getattr(config, "SESSION_AGENT_LOCKS", {}),
    ):
        store.clear()


@pytest.fixture(autouse=True)
def _neutralize_credential_self_heal(monkeypatch):
    monkeypatch.setattr(streaming, "_attempt_credential_self_heal", lambda *a, **k: None)
    yield


@pytest.fixture(autouse=True)
def _mock_hermes_modules():
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider",
        "api_key": "synthetic-key",
        "base_url": None,
    }
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=None)

    injected = {
        "hermes_cli": fake_hermes_cli,
        "hermes_cli.runtime_provider": fake_runtime_module,
        "hermes_state": fake_hermes_state,
    }
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in injected}
    sys.modules.update(injected)
    yield
    for name, prev in saved.items():
        if prev is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev


class RecordingAgent:
    """Minimal AIAgent stand-in that captures the driver callbacks.

    The keyword parameters are spelled out because ``_run_agent_streaming``
    gates optional kwargs on ``inspect.signature(AIAgent.__init__)`` — a bare
    ``**kwargs`` stand-in would hide ``thinking_callback`` from that gate and
    make every assertion below vacuous.
    """

    def __init__(
        self,
        *,
        model=None,
        provider=None,
        base_url=None,
        api_key=None,
        platform=None,
        quiet_mode=False,
        enabled_toolsets=None,
        fallback_model=None,
        session_id=None,
        session_db=None,
        prefill_messages=None,
        stream_delta_callback=None,
        reasoning_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        clarify_callback=None,
        status_callback=None,
        thinking_callback=None,
        interim_assistant_callback=None,
        max_iterations=None,
        max_tokens=None,
        request_overrides=None,
        **kwargs,
    ):
        self.model = model
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.session_id = session_id
        self.stream_delta_callback = stream_delta_callback
        self.reasoning_callback = reasoning_callback
        self.tool_progress_callback = tool_progress_callback
        self.status_callback = status_callback
        # The defect: this used to always be None for WebUI turns.
        self.thinking_callback = thinking_callback
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.context_compressor = None
        self._last_error = None
        self.ephemeral_system_prompt = None
        self.answer = "Settled answer"

    def run_conversation(self, **kwargs):
        raise NotImplementedError

    def interrupt(self, _message):
        pass


def _prepare_session(session_id: str, stream_id: str, *, pending_user_message: str) -> Session:
    session = Session(session_id=session_id, title="Test Session")
    session.messages = []
    session.context_messages = []
    session.pending_user_message = pending_user_message
    session.pending_attachments = []
    session.pending_started_at = 1234567890.0
    session.pending_user_source = "webui"
    session.active_stream_id = stream_id
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _run_stream(session, stream_id, agent_cls, *, workspace, ephemeral=False):
    fake_queue = queue.Queue()
    streaming.STREAMS[stream_id] = fake_queue
    config.STREAM_PARTIAL_TEXT[stream_id] = ""

    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=agent_cls), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=str(workspace),
            stream_id=stream_id,
            ephemeral=ephemeral,
        )

    return fake_queue


def _events(fake_queue):
    return [(item[0], item[1]) for item in list(fake_queue.queue)]


def _warnings(fake_queue, warning_type=None):
    out = [data for event, data in _events(fake_queue) if event == "warning"]
    if warning_type is not None:
        out = [payload for payload in out if payload.get("type") == warning_type]
    return out


def _agent_class(script, *, answer="Settled answer"):
    class ScriptedAgent(RecordingAgent):
        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            script(self)
            return {"messages": history + [{"role": "assistant", "content": self.answer}]}

    ScriptedAgent.answer = answer
    return ScriptedAgent


# ── the defect itself ────────────────────────────────────────────────────────


def test_thinking_callback_is_bound_on_webui_agents(tmp_path):
    """The regression: WebUI agents were constructed without thinking_callback."""
    seen = {}

    def script(agent):
        seen["callback"] = agent.thinking_callback

    agent_cls = _agent_class(script)
    session = _prepare_session("wait_bind", "stream_wait_bind", pending_user_message="hi")
    _run_stream(session, "stream_wait_bind", agent_cls, workspace=tmp_path)

    assert callable(seen["callback"]), "thinking_callback was not passed into AIAgent"


def test_provider_wait_notice_reaches_sse_verbatim(tmp_path):
    def script(agent):
        agent.thinking_callback(HEARTBEAT_NOTICE)

    agent_cls = _agent_class(script)
    session = _prepare_session("wait_notice", "stream_wait_notice", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_wait_notice", agent_cls, workspace=tmp_path)

    notices = _warnings(fake_queue, "provider_wait")
    assert [payload["message"] for payload in notices] == [HEARTBEAT_NOTICE]
    assert notices[0]["session_id"] == "wait_notice"
    # Routed through the existing transient `warning` surface: no new SSE event
    # name was invented for it.
    assert not any(event == "provider_wait" for event, _ in _events(fake_queue))


@pytest.mark.parametrize("notice", [RECONNECT_NOTICE, RETRY_NOTICE, CONTINUATION_NOTICE])
def test_recovery_notices_are_forwarded(tmp_path, notice):
    def script(agent):
        agent.thinking_callback(notice)

    agent_cls = _agent_class(script)
    session = _prepare_session("recovery", "stream_recovery", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_recovery", agent_cls, workspace=tmp_path)

    assert [payload["message"] for payload in _warnings(fake_queue, "provider_wait")] == [notice]


@pytest.mark.parametrize("spinner", CLI_SPINNER_TEXTS)
def test_cli_spinner_text_is_not_forwarded(tmp_path, spinner):
    """`announce_api_call` writes kawaii spinner text through the same callback."""
    def script(agent):
        for _ in range(3):
            agent.thinking_callback(spinner)
        agent.thinking_callback("")

    agent_cls = _agent_class(script)
    session = _prepare_session("spinner", "stream_spinner", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_spinner", agent_cls, workspace=tmp_path)

    assert _warnings(fake_queue) == []


def test_clear_and_reasoning_never_reach_the_status_surface(tmp_path):
    reasoning = (
        "Let me think about the user's question in detail.\n"
        "First, waiting on nothing — this is raw chain of thought.\n"
        "Then I will answer."
    )

    def script(agent):
        agent.thinking_callback(HEARTBEAT_NOTICE)
        agent.thinking_callback("")  # agent clears the line on recovery
        if agent.reasoning_callback is not None:
            agent.reasoning_callback(reasoning)
        agent.stream_delta_callback("answer")

    agent_cls = _agent_class(script)
    session = _prepare_session("no_reasoning_leak", "stream_no_reasoning_leak", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_no_reasoning_leak", agent_cls, workspace=tmp_path)

    payloads = _warnings(fake_queue)
    assert [payload["message"] for payload in payloads] == [HEARTBEAT_NOTICE]
    assert not any("chain of thought" in payload["message"] for payload in payloads)


def test_secret_shaped_values_are_redacted_in_wait_notices(tmp_path):
    leaky = "⏳ waiting on provider — retrying in 5s (api_key=sk-live-abcdef0123456789ABCDEF)"

    def script(agent):
        agent.thinking_callback(leaky)

    agent_cls = _agent_class(script)
    session = _prepare_session("wait_secret", "stream_wait_secret", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_wait_secret", agent_cls, workspace=tmp_path)

    messages = [payload["message"] for payload in _warnings(fake_queue, "provider_wait")]
    assert messages, "expected the retry notice to surface"
    assert "sk-live-abcdef0123456789ABCDEF" not in messages[0]
    assert "[REDACTED]" in messages[0]
    # Nothing anywhere in the stream carries the raw value.
    assert not any("sk-live-abcdef0123456789ABCDEF" in str(data) for _, data in _events(fake_queue))


# ── anti-spam ────────────────────────────────────────────────────────────────


def test_repeated_identical_notices_are_coalesced(tmp_path):
    def script(agent):
        for _ in range(5):
            agent.thinking_callback(HEARTBEAT_NOTICE)

    agent_cls = _agent_class(script)
    session = _prepare_session("wait_dedupe", "stream_wait_dedupe", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_wait_dedupe", agent_cls, workspace=tmp_path)

    assert len(_warnings(fake_queue, "provider_wait")) == 1


def test_bridge_rate_limits_heartbeats_but_not_recovery():
    clock = {"now": 1000.0}
    bridge = streaming._ProviderWaitNoticeBridge(min_interval=30.0, clock=lambda: clock["now"])

    first = bridge.observe(HEARTBEAT_NOTICE, session_id="s")
    assert first and first["type"] == "provider_wait"

    # A different heartbeat 2s later is a rewrite, not news: dropped.
    clock["now"] += 2.0
    assert bridge.observe("⏳ waiting on test-model — 63s with no response yet", session_id="s") is None

    # ... but a reconnect notice inside the same window is the state change the
    # user is waiting for, so it must not be coalesced away.
    assert bridge.observe(RECONNECT_NOTICE, session_id="s") is not None

    # After the interval a fresh heartbeat surfaces again.
    clock["now"] += 31.0
    assert bridge.observe("⏳ waiting on test-model — 94s with no response yet", session_id="s") is not None


def test_bridge_forgets_state_when_the_agent_clears_the_line():
    bridge = streaming._ProviderWaitNoticeBridge(min_interval=30.0, clock=lambda: 0.0)

    assert bridge.observe(HEARTBEAT_NOTICE, session_id="s") is not None
    assert bridge.observe("", session_id="s") is None  # recovery clears the line
    assert bridge.observe(HEARTBEAT_NOTICE, session_id="s") is not None


def test_wait_notice_classifier_rejects_non_notices():
    for text in (
        "",
        None,
        "   ",
        "Settled answer prose that mentions waiting on something eventually",
        "pondering...",
        "⏳ waiting on model\nsecond line",
        "x" * 400,
    ):
        assert streaming._provider_wait_notice_text(text) == ""


# ── agent `warn` statuses (previously dropped entirely) ──────────────────────


def test_agent_warn_status_reaches_sse(tmp_path):
    warn = (
        "⚠️ Session context (~210,000 tokens) exceeds the model context window "
        "(~200,000 tokens) with compression disabled (compression.enabled: false)."
    )

    def script(agent):
        agent.status_callback("warn", warn)

    agent_cls = _agent_class(script)
    session = _prepare_session("warn_status", "stream_warn_status", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_warn_status", agent_cls, workspace=tmp_path)

    payloads = _warnings(fake_queue, "agent_warn")
    assert [payload["message"] for payload in payloads] == [warn]


def test_agent_warn_status_is_deduped_and_capped(tmp_path):
    def script(agent):
        agent.status_callback("warn", "⚠️ Compression summary failed: timeout")
        agent.status_callback("warn", "⚠️ Compression summary failed: timeout")
        for index in range(20):
            agent.status_callback("warn", f"⚠️ Auxiliary title generation failed: attempt {index}")

    agent_cls = _agent_class(script)
    session = _prepare_session("warn_cap", "stream_warn_cap", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_warn_cap", agent_cls, workspace=tmp_path)

    payloads = _warnings(fake_queue, "agent_warn")
    messages = [payload["message"] for payload in payloads]
    assert messages[0] == "⚠️ Compression summary failed: timeout"
    assert messages.count("⚠️ Compression summary failed: timeout") == 1
    assert len(messages) <= streaming._AGENT_WARN_MAX_PER_TURN


def test_plain_lifecycle_chatter_is_still_dropped(tmp_path):
    def script(agent):
        agent.status_callback("lifecycle", "🔄 Making API call #1/50...")
        agent.status_callback("lifecycle", "Compressing context (preflight)")
        agent.status_callback("lifecycle", "💾 Session saved")

    agent_cls = _agent_class(script)
    session = _prepare_session("lifecycle_drop", "stream_lifecycle_drop", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_lifecycle_drop", agent_cls, workspace=tmp_path)

    assert _warnings(fake_queue) == []


def test_fallback_notice_keeps_its_documented_shape(tmp_path):
    def script(agent):
        agent.status_callback("lifecycle", "⚠️ Rate limited — switching to fallback model")

    agent_cls = _agent_class(script)
    session = _prepare_session("fallback_shape", "stream_fallback_shape", pending_user_message="hi")
    fake_queue = _run_stream(session, "stream_fallback_shape", agent_cls, workspace=tmp_path)

    fallbacks = _warnings(fake_queue, "fallback")
    assert [payload["message"] for payload in fallbacks] == ["⚠️ Rate limited — switching to fallback model"]
    assert all("session_id" not in payload for payload in fallbacks)


# ── cached agent: per-turn rebinding ────────────────────────────────────────


def test_cached_agent_rebinds_thinking_callback_to_the_new_turn(tmp_path):
    """A reused cached agent must emit into THIS turn's queue, not the last one."""
    def script(agent):
        agent.thinking_callback(HEARTBEAT_NOTICE)

    agent_cls = _agent_class(script)
    session = _prepare_session("cached_rebind", "stream_cached_rebind_1", pending_user_message="one")
    first_queue = _run_stream(session, "stream_cached_rebind_1", agent_cls, workspace=tmp_path)
    assert _warnings(first_queue, "provider_wait"), "first turn lost its wait notice"

    session.pending_user_message = "two"
    session.active_stream_id = "stream_cached_rebind_2"
    session.save()
    models.SESSIONS[session.session_id] = session
    second_queue = _run_stream(session, "stream_cached_rebind_2", agent_cls, workspace=tmp_path)

    assert _warnings(second_queue, "provider_wait"), (
        "second turn emitted nothing — the cached agent kept the previous turn's closure"
    )


# ── routing target is an existing frontend surface ──────────────────────────


def test_warning_event_is_an_existing_frontend_status_surface():
    """We route into `warning` because the frontend already renders it."""
    src = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = src.find("source.addEventListener('warning'")
    assert start != -1, "the `warning` SSE listener is gone — pick another existing event"
    end = src.find("source.addEventListener('error'", start)
    block = src[start:end]
    assert "setComposerStatus(" in block
    assert "if(!S.session||S.session.session_id!==activeSid) return;" in block
