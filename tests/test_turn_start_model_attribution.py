"""Turn-start model attribution: the earliest SSE frame must carry the model.

Symptom (owner report, Android client): while a turn streams, the answer card
is labeled with the locally picked model (e.g. ``gpt-6.1-sol``) while the reply
is actually served by a different one (e.g. ``claude-haiku-5.5``); re-entering
the session shows the right model. Root cause: clients only learned the served
model from the FINAL session payload (``done`` frame / re-fetched session), so
during the turn they fell back to their own stale picker state.

Fix under test: the turn-opening ``context_status`` frame — the first
deterministic frame both streaming backends emit, before the first token —
carries the authoritative model/provider under the same key names the final
session payload uses (``Session.compact()``: ``model`` / ``model_provider``).

- Local agent path: the values must be the RESOLVED pair the AIAgent is
  constructed with (``resolve_model_provider`` output), not the request's
  (possibly stale, client-picked) model/provider.
- Gateway path: the values must be the model/provider the turn dispatches to
  the gateway — the same pair stamped onto the session in the done payload.

Both assertions are on observable SSE frames, additive-key style: existing
``context_status`` keys (``session_id``, ``prefill``) must stay intact.
"""

import queue
import sys
import types

from api import models, streaming
from api.models import Session

REQUESTED_MODEL = "gpt-6.1-sol"          # what the (stale) client picker sent
REQUESTED_PROVIDER = "openai"
RESOLVED_MODEL = "claude-haiku-5.5"      # what the turn is actually routed with
RESOLVED_PROVIDER = "anthropic"


def _drain(q):
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


def _context_status_frames(events):
    return [data for name, data in events if name == "context_status"]


def _assert_opens_turn_with_model(events):
    """The turn-opening frame carries the served model, before any token."""
    frames = _context_status_frames(events)
    assert frames, "expected a turn-opening context_status frame"
    frame = frames[0]
    # Additive contract: pre-existing keys keep their meaning.
    assert "prefill" in frame
    # New turn-start attribution keys, named like the final session payload.
    assert frame.get("model") == RESOLVED_MODEL, (
        "context_status must carry the RESOLVED model the turn is routed with, "
        f"not the client-picked one (got {frame.get('model')!r})"
    )
    assert frame.get("model_provider") == RESOLVED_PROVIDER, (
        "context_status must carry the resolved provider "
        f"(got {frame.get('model_provider')!r})"
    )
    names = [name for name, _ in events]
    assert "token" in names, "harness sanity: fake agent must stream a token"
    assert names.index("context_status") < names.index("token"), (
        "model attribution must arrive BEFORE the first token so the client "
        "can label the answer card while it streams"
    )


class _FakeAgent:
    """Minimal stand-in that streams one token and returns a clean success."""

    def __init__(self, model=None, provider=None, base_url=None, api_key=None,
                 platform=None, quiet_mode=False, enabled_toolsets=None,
                 fallback_model=None, session_id=None, session_db=None,
                 stream_delta_callback=None, reasoning_callback=None,
                 tool_progress_callback=None, interim_assistant_callback=None,
                 clarify_callback=None, **kwargs):
        self.model = model
        self.provider = provider
        self.session_id = session_id
        self.stream_delta_callback = stream_delta_callback
        self.context_compressor = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_estimated_cost_usd = None
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.reasoning_config = None
        self.ephemeral_system_prompt = None
        self._last_error = None

    def run_conversation(self, **kwargs):
        if self.stream_delta_callback:
            self.stream_delta_callback("hello from the served model")
        return {
            "messages": [
                {"role": "user", "content": kwargs.get("persist_user_message", "")},
                {"role": "assistant", "content": "hello from the served model"},
            ],
        }

    def interrupt(self, _message):
        return None


def test_local_agent_turn_opens_with_resolved_model_frame(tmp_path, monkeypatch):
    """/api/chat/stream local backend: first frame carries the resolved model."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()

    sid = "turn-meta-local"
    stream_id = "stream-turn-meta-local"
    session = Session(
        session_id=sid,
        title="Turn meta test",
        workspace=str(tmp_path),
        model=REQUESTED_MODEL,
        model_provider=REQUESTED_PROVIDER,
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Hi"
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[sid] = session
    streaming.SESSIONS[sid] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    with monkeypatch.context() as m:
        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(streaming, "_get_ai_agent", lambda: _FakeAgent)
        # The authoritative routing decision: resolve_model_provider maps the
        # request onto the model/provider the AIAgent is built with.
        m.setattr(
            streaming,
            "resolve_model_provider",
            lambda *_args, **_kwargs: (RESOLVED_MODEL, RESOLVED_PROVIDER, None),
        )
        m.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        m.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text="Hi",
            model=REQUESTED_MODEL,
            workspace=str(tmp_path),
            stream_id=stream_id,
            model_provider=REQUESTED_PROVIDER,
        )

    events = [(name, data) for name, data, *_rest in _drain(event_queue)]
    _assert_opens_turn_with_model(events)
    frame = _context_status_frames(events)[0]
    assert frame["session_id"] == sid


def test_gateway_turn_opens_with_dispatched_model_frame(tmp_path, monkeypatch):
    """Gateway backend: first frame carries the model dispatched to gateway."""
    from collections import OrderedDict

    import api.gateway_chat as gateway_chat
    from api.config import STREAMS, create_stream_channel
    from api.models import new_session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.local")
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_API_KEY", "secret-token")
    monkeypatch.setattr(
        gateway_chat, "_gateway_reasoning_effort_for_request", lambda *a, **k: None
    )
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "loaded",
        "source": "test",
        "label": "test",
        "message_count": 1,
        "messages": [{"role": "assistant", "content": "prefill summary"}],
    })
    monkeypatch.setattr(
        streaming,
        "_prefill_messages_with_webui_context",
        lambda ctx, cfg: list(ctx["messages"]),
    )
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen",
                        lambda req, timeout=0: FakeResponse())

    s = new_session()
    stream_id = "stream-turn-meta-gateway"
    s.active_stream_id = stream_id
    s.pending_user_message = "Say hello"
    s.pending_attachments = []
    s.pending_started_at = 123
    s.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        s.session_id,
        "Say hello",
        RESOLVED_MODEL,
        str(tmp_path),
        stream_id,
        [],
        model_provider=RESOLVED_PROVIDER,
    )

    events = [(item[0], item[1]) for item in _drain(subscriber)]
    _assert_opens_turn_with_model(events)
    frame = _context_status_frames(events)[0]
    assert frame["session_id"] == s.session_id
