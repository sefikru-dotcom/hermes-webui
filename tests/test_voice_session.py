"""Request-level tests for the voice gateway (api/voice_session.py).

Transport under test: SSE downstream (GET /api/voice/stream) + POST upstream
(POST /api/voice/{session,interrupt,close}), over a real loopback
``QuietHTTPServer`` with the real ``check_auth`` and the real route dispatch —
no stubs of the gateway itself.

What is proven here:
  * the exact response shape of POST /api/voice/session (key *set*, not "contains");
  * 401 with no device Bearer on all four paths, and 401 for a garbage Bearer;
  * a voice channel is bound to one profile and one session: a foreign profile
    or a mismatched session_id answers 403, never 200-with-empty;
  * a second create for the same session returns the same voice_id;
  * interrupt -> the client sees ``speech.stop`` and a repeat call stays safe;
  * the stream projects the durable run journal onto the fixed vocabulary
    (agent.started / agent.text.delta / agent.finished);
  * Last-Event-ID resumes through the run journal, not an in-memory queue;
  * TTL expiry and close tear the channel down;
  * neither the device token nor the full voice_id is written to the log.

The chat runtime is never started: journal rows are appended directly with the
same ``api.run_journal.append_run_event`` the streaming layer uses, so the
gateway is exercised exactly as it runs in production.
"""

import http.client
import json
import logging
import socket
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

WIRE_EVENTS = {
    "session.ready",
    "transcript.partial",
    "transcript.final",
    "agent.started",
    "agent.text.delta",
    "tool.started",
    "tool.finished",
    "agent.finished",
    "speech.stop",
}


# ── helpers ────────────────────────────────────────────────────────────────


# This host runs the production WebUI plus concurrent builds, so the box can be
# under real memory pressure: the client timeouts are generous on purpose.  A
# tight timeout here reports the machine's load as a gateway failure.
HTTP_TIMEOUT = 60.0
SSE_TIMEOUT = 60.0


def _request(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HTTP_TIMEOUT)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        return response.status, payload
    finally:
        conn.close()


def _json_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post_json(port, path, token, payload):
    return _request(
        port, "POST", path, headers=_json_headers(token), body=json.dumps(payload).encode("utf-8")
    )


def _mint_native_token(pairings):
    created = pairings.create_pairing()
    assert pairings.approve_pairing(created["code"], session_cookie="verified")
    exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
    return exchanged["access_token"]


class _SSEClient:
    """Minimal raw-socket SSE client (no library, so the wire format is proven)."""

    def __init__(self, port, path, token, last_event_id=None, timeout=SSE_TIMEOUT):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buffer = b""
        self.status = None
        self.headers = {}
        lines = [
            f"GET {path} HTTP/1.1",
            "Host: 127.0.0.1",
            f"Authorization: Bearer {token}",
            "Accept: text/event-stream",
            "Connection: keep-alive",
        ]
        if last_event_id:
            lines.append(f"Last-Event-ID: {last_event_id}")
        request = "\r\n".join(lines) + "\r\n\r\n"
        self.sock.sendall(request.encode("utf-8"))
        self._read_head()

    def _recv(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise EOFError("stream closed")
        self.buffer += chunk

    def _read_head(self):
        while b"\r\n\r\n" not in self.buffer:
            self._recv()
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        text = head.decode("utf-8", "replace").split("\r\n")
        self.status = int(text[0].split(" ")[1])
        for line in text[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                self.headers[name.strip().lower()] = value.strip()

    def next_event(self, timeout=5.0):
        deadline = time.time() + timeout
        while True:
            if b"\n\n" in self.buffer:
                block, _, self.buffer = self.buffer.partition(b"\n\n")
                event = {"id": None, "event": None, "data": None, "comment": None}
                for line in block.decode("utf-8", "replace").split("\n"):
                    if not line:
                        continue
                    if line.startswith(":"):
                        event["comment"] = line[1:].strip()
                    elif line.startswith("id:"):
                        event["id"] = line[3:].strip()
                    elif line.startswith("event:"):
                        event["event"] = line[6:].strip()
                    elif line.startswith("data:"):
                        event["data"] = json.loads(line[5:].strip())
                if event["event"] or event["comment"]:
                    return event
                continue
            if time.time() > deadline:
                raise TimeoutError("no SSE event within %.1fs" % timeout)
            self.sock.settimeout(max(0.05, deadline - time.time()))
            self._recv()

    def next_event_named(self, name, timeout=5.0):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            event = self.next_event(timeout=max(0.05, deadline - time.time()))
            if event["event"] == name:
                return event
            seen.append(event["event"])
        raise AssertionError(f"event {name!r} not seen; got {seen}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def _append_token(sessions_root, session_id, run_id, text):
    from api.run_journal import append_run_event

    return append_run_event(
        session_id, run_id, "token", {"text": text}, session_dir=sessions_root
    )


@pytest.fixture()
def voice_server(monkeypatch):
    """Real server, real auth, real dispatch; all state in a temp dir."""
    import api.auth as auth
    import api.device_pairings as pairings
    import api.models as models
    import api.voice_session as voice_session
    import server

    clients = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        voice_dir = tmp_root / "voice-state"
        sessions_dir = tmp_root / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("HERMES_WEBUI_VOICE_STATE_DIR", str(voice_dir))
        monkeypatch.setenv("HERMES_WEBUI_VOICE_HEARTBEAT_SECONDS", "0.3")
        monkeypatch.setenv("HERMES_WEBUI_VOICE_POLL_SECONDS", "0.05")
        monkeypatch.setenv("HERMES_WEBUI_VOICE_SWEEP_SECONDS", "0.5")
        monkeypatch.setenv("HERMES_WEBUI_VOICE_PERSIST_SECONDS", "0")

        monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)
        monkeypatch.setattr(pairings, "STATE_DIR", tmp_root)
        monkeypatch.setattr(pairings, "_DB_PATH", tmp_root / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(auth, "verify_session", lambda value: False)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)

        # Every session_id in these tests is owned by the default profile.
        monkeypatch.setattr(
            voice_session,
            "_load_session_metadata",
            lambda sid: types.SimpleNamespace(profile="default", session_id=sid),
        )
        # Fresh registry per test (module state is process-global by design).
        monkeypatch.setattr(voice_session, "_REGISTRY", voice_session._VoiceRegistry())

        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        class Ctx:
            port = httpd.server_address[1]
            root = tmp_root
            # api.models.SESSION_DIR is what the gateway resolves run journals
            # against, so tests must append there and nowhere else.
            sessions = sessions_dir
            module = voice_session

            @staticmethod
            def token():
                return _mint_native_token(pairings)

            @staticmethod
            def stream(token, voice_id, last_event_id=None):
                client = _SSEClient(
                    httpd.server_address[1],
                    f"/api/voice/stream?voice_id={voice_id}",
                    token,
                    last_event_id=last_event_id,
                )
                clients.append(client)
                return client

        try:
            yield Ctx
        finally:
            for client in clients:
                client.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def _create(ctx, token, session_id):
    status, payload = _post_json(
        ctx.port, "/api/voice/session", token, {"session_id": session_id}
    )
    assert status == 200, payload
    return payload


# ── tests ──────────────────────────────────────────────────────────────────


def test_create_returns_exact_response_shape(voice_server):
    ctx = voice_server
    token = ctx.token()
    status, payload = _post_json(
        ctx.port, "/api/voice/session", token, {"session_id": "sess-voice-1"}
    )
    assert status == 200
    assert set(payload) == {"voice_id", "session_id", "expires_in"}
    assert payload["session_id"] == "sess-voice-1"
    assert isinstance(payload["voice_id"], str) and payload["voice_id"].startswith("v1_")
    assert isinstance(payload["expires_in"], int) and payload["expires_in"] > 0


def test_create_requires_session_id(voice_server):
    ctx = voice_server
    token = ctx.token()
    status, payload = _post_json(ctx.port, "/api/voice/session", token, {})
    assert status == 400
    assert payload == {"error": "session_id is required"}


def test_create_for_unknown_session_is_404(voice_server, monkeypatch):
    ctx = voice_server

    def _raise(sid):
        raise KeyError(sid)

    monkeypatch.setattr(ctx.module, "_load_session_metadata", _raise)
    status, payload = _post_json(
        ctx.port, "/api/voice/session", ctx.token(), {"session_id": "sess-missing"}
    )
    assert status == 404
    assert payload == {"error": "unknown session"}


def test_repeat_create_returns_the_same_voice_id(voice_server):
    ctx = voice_server
    token = ctx.token()
    first = _create(ctx, token, "sess-voice-2")
    second = _create(ctx, token, "sess-voice-2")
    assert first["voice_id"] == second["voice_id"]
    assert second["session_id"] == "sess-voice-2"


def test_each_session_gets_its_own_channel(voice_server):
    ctx = voice_server
    token = ctx.token()
    assert _create(ctx, token, "sess-a")["voice_id"] != _create(ctx, token, "sess-b")["voice_id"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/voice/session"),
        ("GET", "/api/voice/stream?voice_id=v1_whatever"),
        ("POST", "/api/voice/interrupt"),
        ("POST", "/api/voice/close"),
    ],
)
def test_missing_token_is_401_on_every_voice_path(voice_server, method, path):
    ctx = voice_server
    status, payload = _request(ctx.port, method, path, headers={"Content-Type": "application/json"}, body=b"{}" if method == "POST" else None)
    assert status == 401
    assert payload == {"error": "Authentication required"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/voice/session"),
        ("GET", "/api/voice/stream?voice_id=v1_whatever"),
        ("POST", "/api/voice/interrupt"),
        ("POST", "/api/voice/close"),
    ],
)
def test_garbage_token_fails_closed(voice_server, method, path):
    ctx = voice_server
    headers = {"Authorization": "Bearer hdev_invalidcredential", "Content-Type": "application/json"}
    status, payload = _request(ctx.port, method, path, headers=headers, body=b"{}" if method == "POST" else None)
    assert status == 401
    assert payload == {"error": "Authentication required"}


def test_stream_without_voice_id_is_400(voice_server):
    ctx = voice_server
    status, payload = _request(
        ctx.port, "GET", "/api/voice/stream", headers={"Authorization": f"Bearer {ctx.token()}"}
    )
    assert status == 400
    assert payload == {"error": "voice_id is required"}


def test_stream_with_unknown_voice_id_is_404(voice_server):
    ctx = voice_server
    status, payload = _request(
        ctx.port,
        "GET",
        "/api/voice/stream?voice_id=v1_doesnotexist000000",
        headers={"Authorization": f"Bearer {ctx.token()}"},
    )
    assert status == 404
    assert payload == {"error": "voice session not found"}


def test_foreign_profile_voice_id_is_403(voice_server, monkeypatch):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-3")["voice_id"]

    monkeypatch.setattr(ctx.module, "_active_profile", lambda: "another-profile")

    for method, path, body in (
        ("POST", "/api/voice/interrupt", {"voice_id": voice_id}),
        ("POST", "/api/voice/close", {"voice_id": voice_id}),
    ):
        status, payload = _post_json(ctx.port, path, token, body)
        assert status == 403, (path, payload)
        assert payload == {"error": "voice session belongs to another profile"}

    status, payload = _request(
        ctx.port,
        "GET",
        f"/api/voice/stream?voice_id={voice_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 403
    assert payload == {"error": "voice session belongs to another profile"}


def test_mismatched_session_id_in_body_is_403(voice_server):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-4")["voice_id"]
    status, payload = _post_json(
        ctx.port,
        "/api/voice/interrupt",
        token,
        {"voice_id": voice_id, "session_id": "some-other-session"},
    )
    assert status == 403
    assert payload == {"error": "voice session does not belong to this session"}


def test_interrupt_emits_speech_stop_and_repeat_is_safe(voice_server):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-5")["voice_id"]

    client = ctx.stream(token, voice_id)
    assert client.status == 200
    assert client.headers["content-type"].startswith("text/event-stream")
    ready = client.next_event_named("session.ready")
    assert ready["data"]["protocol"] == "hermes.voice.v1"
    assert ready["data"]["session_id"] == "sess-voice-5"
    assert set(ready["data"]["server_events"]) == WIRE_EVENTS

    first = _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id})
    assert first[0] == 200
    assert first[1] == {
        "ok": True,
        "voice_id": voice_id,
        "interrupted": True,
        "already_interrupted": False,
    }
    stop = client.next_event_named("speech.stop")
    assert stop["id"].startswith(f"{voice_id}:")
    assert stop["data"]["reason"] == "interrupt"
    assert stop["data"]["session_id"] == "sess-voice-5"

    second = _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id})
    assert second[0] == 200
    assert second[1]["ok"] is True
    assert second[1]["already_interrupted"] is True
    stop_again = client.next_event_named("speech.stop")
    assert stop_again["data"]["reason"] == "interrupt"

    # The channel is still usable after a double barge-in.
    status, payload = _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id})
    assert status == 200 and payload["ok"] is True


def test_stream_projects_run_journal_onto_wire_events(voice_server):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-6")["voice_id"]
    client = ctx.stream(token, voice_id)
    client.next_event_named("session.ready")

    run_id = "run-voice-6"
    _append_token(ctx.sessions, "sess-voice-6", run_id, "Привет, ")
    started = client.next_event_named("agent.started")
    assert started["data"]["run_id"] == run_id
    assert started["data"]["session_id"] == "sess-voice-6"

    _append_token(ctx.sessions, "sess-voice-6", run_id, "как дела?")
    deltas = [client.next_event_named("agent.text.delta") for _ in range(2)]
    assert [item["data"]["text"] for item in deltas] == ["Привет, ", "как дела?"]

    from api.run_journal import append_run_event

    append_run_event(
        "sess-voice-6", run_id, "done", {"session": {"huge": "x" * 5000}, "usage": {"input_tokens": 3}},
        session_dir=ctx.sessions,
    )
    finished = client.next_event_named("agent.finished")
    assert finished["data"]["reason"] == "done"
    assert finished["data"]["usage"] == {"input_tokens": 3}
    assert "session" not in finished["data"]  # the whole session is never shipped

    # Exactly one terminal frame per run: the trailing stream_end is suppressed.
    append_run_event("sess-voice-6", run_id, "stream_end", {}, session_dir=ctx.root)
    _append_token(ctx.sessions, "sess-voice-6", run_id, "ignored-after-terminal")
    deadline = time.time() + 1.0
    while time.time() < deadline:
        event = client.next_event(timeout=0.5)
        if event is None:
            continue  # keepalive only
        assert event["event"] not in {"agent.finished", "agent.text.delta"}, (
            "the run already ended; trailing rows must not reach the client"
        )
    ids = [deltas[0]["id"], deltas[1]["id"], finished["id"]]
    assert len(set(ids)) == 3
    for event_id in ids:
        voice_id_part, _, seq = event_id.rpartition(":")
        assert voice_id_part == voice_id and seq.isdigit()


def test_last_event_id_resumes_from_the_run_journal(voice_server):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-7")["voice_id"]
    run_id = "run-voice-7"

    first = ctx.stream(token, voice_id)
    first.next_event_named("session.ready")
    _append_token(ctx.sessions, "sess-voice-7", run_id, "первая ")
    first.next_event_named("agent.started")
    delta = first.next_event_named("agent.text.delta")
    assert delta["data"]["text"] == "первая "
    last_id = delta["id"]
    first.close()
    time.sleep(0.2)  # let the server notice the drop

    # These rows land while nobody is connected: an in-memory queue would lose
    # them, the durable run journal must not.
    _append_token(ctx.sessions, "sess-voice-7", run_id, "вторая ")
    _append_token(ctx.sessions, "sess-voice-7", run_id, "третья")

    second = ctx.stream(token, voice_id, last_event_id=last_id)
    ready = second.next_event_named("session.ready")
    assert ready["data"]["resume_requested"] is True
    assert ready["data"]["resume_status"] in {"ok", "cursor_ahead"}
    texts = [
        second.next_event_named("agent.text.delta")["data"]["text"] for _ in range(2)
    ]
    assert texts == ["вторая ", "третья"]


def test_close_tears_the_channel_down(voice_server):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-8")["voice_id"]
    client = ctx.stream(token, voice_id)
    client.next_event_named("session.ready")

    status, payload = _post_json(ctx.port, "/api/voice/close", token, {"voice_id": voice_id})
    assert status == 200
    assert payload == {"ok": True, "voice_id": voice_id, "closed": True}

    # The stream ends on its own instead of pinning a request thread.
    with pytest.raises((EOFError, TimeoutError)):
        for _ in range(20):
            event = client.next_event(timeout=0.5)
            assert event["event"] in WIRE_EVENTS

    status, payload = _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id})
    assert status == 404
    assert payload == {"error": "voice session not found"}

    # A fresh create after close yields a new channel.
    assert _create(ctx, token, "sess-voice-8")["voice_id"] != voice_id


def test_ttl_expiry_drops_the_channel(voice_server, monkeypatch):
    ctx = voice_server
    monkeypatch.setenv("HERMES_WEBUI_VOICE_TTL_SECONDS", "1")
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-9")["voice_id"]
    assert ctx.module.active_voice_session_count() == 1

    time.sleep(1.1)
    status, payload = _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id})
    assert status == 410
    assert payload == {"error": "voice session expired"}
    assert ctx.module.active_voice_session_count() == 0


def test_sweep_removes_expired_channels(voice_server, monkeypatch):
    ctx = voice_server
    monkeypatch.setenv("HERMES_WEBUI_VOICE_TTL_SECONDS", "1")
    token = ctx.token()
    _create(ctx, token, "sess-voice-10")
    time.sleep(1.1)
    assert ctx.module.sweep_expired() >= 1
    assert ctx.module.active_voice_session_count() == 0


def test_registry_survives_a_restart(voice_server):
    """The voice_id and its cursor are durable, not process-local."""
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-11")["voice_id"]

    before = ctx.module.resolve_voice_session(voice_id, profile="default")
    before_seq = before.seq

    # Simulate a process restart: drop the in-memory registry, keep the files.
    ctx.module._REGISTRY = ctx.module._VoiceRegistry()

    resolved = ctx.module.resolve_voice_session(voice_id, profile="default")
    assert resolved.session_id == "sess-voice-11"
    assert resolved.seq == before_seq
    # Re-attaching the same session returns the surviving id, not a new one.
    again, created = ctx.module.get_or_create_voice_session("sess-voice-11")
    assert created is False and again.voice_id == voice_id


def test_transcript_final_reaches_the_stream(voice_server):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-12")["voice_id"]
    client = ctx.stream(token, voice_id)
    client.next_event_named("session.ready")

    assert ctx.module.publish_transcript("sess-voice-12", "включи музыку", final=True) is True
    final = client.next_event_named("transcript.final")
    assert final["data"]["text"] == "включи музыку"
    assert final["data"]["final"] is True

    # No channel for that session -> nothing published, no exception.
    assert ctx.module.publish_transcript("sess-without-voice", "x", final=False) is False


def test_wire_ids_are_strictly_increasing_on_a_fresh_connect(voice_server):
    """A fresh channel starts at the high-water mark, never behind it.

    Regression: events recorded while nobody was attached stayed in the
    delivery queue and were flushed *after* session.ready, so their ids were
    lower than (and once equal to) the ready frame's. An SSE client orders and
    dedupes by id, so those frames would be dropped or applied out of order.
    """
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-15")["voice_id"]

    # Two barge-ins with no stream open: they land in the durable journal only.
    for _ in range(2):
        status, _payload = _post_json(
            ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id}
        )
        assert status == 200

    client = ctx.stream(token, voice_id)
    ready = client.next_event_named("session.ready")
    assert ready["data"]["resume_status"] == "fresh"

    _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id})
    stop = client.next_event_named("speech.stop")

    def _seq(frame):
        return int(frame["id"].rsplit(":", 1)[1])

    assert _seq(stop) > _seq(ready), (ready["id"], stop["id"])


def test_resume_does_not_duplicate_frames_already_replayed(voice_server):
    """Replay and the in-memory queue must not both deliver the same seq."""
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-16")["voice_id"]

    first = ctx.stream(token, voice_id)
    ready = first.next_event_named("session.ready")
    last_id = ready["id"]
    first.close()
    time.sleep(0.2)

    for _ in range(2):
        status, _payload = _post_json(
            ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id}
        )
        assert status == 200

    second = ctx.stream(token, voice_id, last_event_id=last_id)
    resumed = second.next_event_named("session.ready")
    assert resumed["data"]["resume_status"] == "ok"

    seen = []
    for _ in range(2):
        seen.append(second.next_event_named("speech.stop")["id"])
    assert len(set(seen)) == 2, seen

    # Nothing further is queued: the duplicates would arrive immediately.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        try:
            frame = second.next_event(timeout=0.3)
        except TimeoutError:
            break
        if frame["event"] == "speech.stop":
            raise AssertionError(f"duplicate speech.stop after replay: {frame['id']}")


def test_second_concurrent_stream_is_409(voice_server):
    ctx = voice_server
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-13")["voice_id"]
    first = ctx.stream(token, voice_id)
    first.next_event_named("session.ready")
    status, payload = _request(
        ctx.port,
        "GET",
        f"/api/voice/stream?voice_id={voice_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status == 409
    assert payload == {"error": "voice channel already has an active stream"}


def test_no_token_or_voice_id_in_logs(voice_server, caplog):
    ctx = voice_server
    caplog.set_level(logging.DEBUG)
    token = ctx.token()
    voice_id = _create(ctx, token, "sess-voice-14")["voice_id"]
    _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id})
    _post_json(ctx.port, "/api/voice/interrupt", token, {"voice_id": voice_id, "session_id": "other"})
    _post_json(ctx.port, "/api/voice/close", token, {"voice_id": voice_id})
    _request(
        ctx.port,
        "GET",
        f"/api/voice/stream?voice_id={voice_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    text = caplog.text
    assert token not in text, "device token leaked into logs"
    assert voice_id not in text, "full voice_id leaked into logs"
    # Masking keeps the operator-visible prefix only.
    assert voice_id[:4] in text
