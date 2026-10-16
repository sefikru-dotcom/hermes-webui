"""Endpoint tests for GET /api/voice/session — real server, real socket.

These drive an actual ``server.QuietHTTPServer`` over a real TCP connection,
perform the RFC 6455 handshake by hand, and speak the voice text contract on
the wire.  Auth is the real ``api.auth.check_auth`` path with a real minted
device Bearer, so the route's allow-list admission is exercised, not mocked.

Two flavours of turn are covered:

* ``_FakeLoopback`` — the loopback client is replaced, so the test pins the
  *transport* (framing, the event contract, session_id binding);
* the real ``ChatLoopbackClient`` against stubbed ``/api/chat/start`` and
  ``/api/chat/stream`` handlers — the test pins the *adapter hop* (loopback
  target resolution, credentials relay, SSE parsing over HTTP).
"""

import base64
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path
import tempfile

import pytest

from api import voice_gateway as vg

_READY_TIMEOUT = 10.0


# ── wire-level WebSocket client (no third-party deps) ────────────────────
class WsClient:
    """A minimal RFC 6455 client speaking the voice contract."""

    def __init__(self, port, path, *, headers=None, timeout=_READY_TIMEOUT):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.key = base64.b64encode(os.urandom(16)).decode()
        self.status = None
        self.response_headers = {}
        self.handshake_raw = b""
        self._body = b""
        self._residual = b""
        self._decoder = vg.FrameDecoder(require_mask=False)
        self._assembler = vg.MessageAssembler()
        self._pending = []
        request_headers = {
            "Host": f"127.0.0.1:{port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": self.key,
            "Sec-WebSocket-Version": "13",
        }
        request_headers.update(headers or {})
        request = f"GET {path} HTTP/1.1\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in request_headers.items()
        ) + "\r\n"
        self.sock.sendall(request.encode())
        self._read_response_head()

    # -- handshake -------------------------------------------------------
    def _read_response_head(self):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        self.handshake_raw = buf
        lines = head.decode("latin-1").split("\r\n")
        if lines and lines[0].startswith("HTTP/"):
            self.status = int(lines[0].split(" ", 2)[1])
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip():
                self.response_headers[name.strip().lower()] = value.strip()
        if self.status == 101:
            # Anything after the header block is already frame data.
            self._ingest(rest)
        else:
            self._residual = rest
            self._read_body()

    def _ingest(self, data):
        if not data:
            return
        for frame in self._decoder.feed(data):
            for message in self._assembler.feed(frame):
                self._pending.append(message)

    def _read_body(self):
        """Drain a non-101 response body (Content-Length, else until EOF)."""
        raw = self._residual
        self._residual = b""
        length = self.response_headers.get("content-length")
        need = int(length) if (length or "").isdigit() else None
        while need is None or len(raw) < need:
            try:
                chunk = self.sock.recv(4096)
            except (socket.timeout, ConnectionError):
                break
            if not chunk:
                break
            raw += chunk
        self._body = raw

    def body_bytes(self):
        return self._body

    def json_body(self):
        return json.loads(self.body_bytes().decode("utf-8"))

    # -- frames ----------------------------------------------------------
    def _fill(self):
        """Read from the socket until at least one complete message is buffered."""
        while not self._pending:
            chunk = self.sock.recv(8192)
            if not chunk:
                raise ConnectionError("socket closed")
            self._ingest(chunk)

    def send_frame(self, opcode, payload=b""):
        self.sock.sendall(
            vg.encode_frame(opcode, payload, mask=True, mask_key=os.urandom(4))
        )

    def send_text(self, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_frame(vg.OP_TEXT, data)

    def recv(self):
        """Next complete peer message."""
        self._fill()
        return self._pending.pop(0)

    def recv_json(self):
        message = self.recv()
        assert message.kind == "text", message
        return json.loads(message.text())

    def recv_until(self, event_type, *, max_frames=200):
        for _ in range(max_frames):
            payload = self.recv_json()
            if payload.get("type") == event_type:
                return payload
        raise AssertionError(f"{event_type} not seen")

    def collect_turn(self, *, max_frames=400):
        """Every frame until the turn's terminal agent.finished (inclusive)."""
        seen = []
        for _ in range(max_frames):
            payload = self.recv_json()
            seen.append(payload)
            if payload.get("type") == vg.EVENT_FINISHED:
                return seen
        raise AssertionError("turn never finished")

    def close(self):
        try:
            self.send_frame(vg.OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ── loopback stand-ins ───────────────────────────────────────────────────
class _FakeLoopback:
    """ChatLoopbackClient stand-in with a scripted run."""

    def __init__(self, events, *, start_status=200, start_payload=None,
                 stream_error=None, hold=None, hold_after=0):
        self.events = list(events)
        self.start_status = start_status
        self.start_payload = start_payload
        self.stream_error = stream_error
        self.hold = hold          # threading.Event: keeps a turn "in flight"
        self.hold_after = hold_after
        self.started = []
        self.cancelled = []
        self.streamed = []

    def start_turn(self, *, session_id, message):
        self.started.append({"session_id": session_id, "message": message})
        payload = self.start_payload
        if payload is None:
            payload = {"session_id": session_id or "gen-1", "stream_id": "run-1",
                       "status": "started"}
        return self.start_status, dict(payload)

    def cancel_turn(self, stream_id):
        self.cancelled.append(stream_id)
        if self.hold is not None:
            self.hold.set()
        return True

    def iter_run_events(self, stream_id):
        self.streamed.append(stream_id)
        if self.stream_error is not None:
            raise self.stream_error
        for index, item in enumerate(self.events):
            yield item
            if self.hold is not None and index == self.hold_after:
                # Hold the turn open until the test cancels it (or the wait lapses).
                self.hold.wait(10)


def _sse_bytes(events):
    return "".join(
        f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for name, data in events
    ).encode("utf-8")


# ── fixtures ─────────────────────────────────────────────────────────────
def _sentinel_bytes(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return True


@pytest.fixture()
def voice_server(monkeypatch):
    """Real server + real auth + a minted device Bearer. Yields (port, token)."""
    import api.auth as auth
    import api.device_pairings as pairings
    import server

    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        # Cookie auth stays dead so "no Bearer" means 401 unambiguously.
        monkeypatch.setattr(auth, "verify_session", lambda value: False)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)

        created = pairings.create_pairing()
        assert pairings.approve_pairing(created["code"], session_cookie="verified")
        exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
        token = exchanged["access_token"]

        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield httpd.server_address[1], token
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ── 1. gate: auth, upgrade, origin ───────────────────────────────────────
def test_route_requires_authentication(voice_server):
    port, _token = voice_server
    client = WsClient(port, vg.WS_PATH)
    try:
        assert client.status == 401
        assert client.json_body() == {"error": "Authentication required"}
    finally:
        client.close()


def test_route_rejects_garbage_bearer(voice_server):
    port, _token = voice_server
    client = WsClient(port, vg.WS_PATH, headers=_bearer("Bearer not-a-real-credential"))
    try:
        assert client.status == 401
    finally:
        client.close()


def test_non_upgrade_request_gets_400_json_not_404(voice_server):
    port, token = voice_server
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", vg.WS_PATH, headers=_bearer(token))
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        assert response.status == 400
        assert payload["error"] == "WebSocket upgrade required"
        assert payload["protocol"] == vg.VOICE_PROTOCOL
    finally:
        conn.close()


def test_cross_origin_cookie_upgrade_is_rejected(voice_server, monkeypatch):
    """A non-Bearer upgrade needs same-origin proof (WS is not CORS-guarded)."""
    import api.auth as auth

    port, _token = voice_server
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)  # no Bearer in play
    client = WsClient(port, vg.WS_PATH, headers={
        "Origin": "http://evil.example", "Cookie": "hermes_session=whatever",
    })
    try:
        assert client.status == 403
        assert client.json_body()["error"] == "Cross-origin request rejected"
    finally:
        client.close()


def test_same_origin_cookie_upgrade_is_allowed(voice_server, monkeypatch):
    import api.auth as auth

    port, _token = voice_server
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: _FakeLoopback([]))
    client = WsClient(port, vg.WS_PATH, headers={
        "Origin": f"http://127.0.0.1:{port}", "Host": f"127.0.0.1:{port}",
    })
    try:
        assert client.status == 101
        assert client.recv_json()["type"] == vg.EVENT_SESSION_READY
    finally:
        client.close()


def test_handshake_accept_header_matches_rfc6455(voice_server):
    port, token = voice_server
    monkeypatch_client = _FakeLoopback([])
    import api.voice_gateway as gateway

    original = gateway._default_client_factory
    gateway._default_client_factory = lambda handler, parsed: monkeypatch_client
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        assert client.status == 101
        assert client.response_headers["upgrade"].lower() == "websocket"
        assert client.response_headers["sec-websocket-accept"] == vg.compute_accept(
            client.key
        )
        assert client.response_headers["sec-websocket-version"] == "13"
    finally:
        client.close()
        gateway._default_client_factory = original


def test_subprotocol_is_echoed_only_when_offered(voice_server):
    import api.voice_gateway as gateway

    port, token = voice_server
    original = gateway._default_client_factory
    gateway._default_client_factory = lambda handler, parsed: _FakeLoopback([])
    offered = WsClient(port, vg.WS_PATH, headers={
        **_bearer(token), "Sec-WebSocket-Protocol": f"other, {vg.VOICE_PROTOCOL}",
    })
    plain = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        assert offered.response_headers["sec-websocket-protocol"] == vg.VOICE_PROTOCOL
        assert "sec-websocket-protocol" not in plain.response_headers
    finally:
        offered.close()
        plain.close()
        gateway._default_client_factory = original


# ── 2. session.ready / keepalive / control frames ────────────────────────
def test_session_ready_announces_protocol_and_bound_session(voice_server, monkeypatch):
    port, token = voice_server
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: _FakeLoopback([]))
    client = WsClient(port, f"{vg.WS_PATH}?session_id=sess-42", headers=_bearer(token))
    try:
        ready = client.recv_json()
        assert ready["type"] == vg.EVENT_SESSION_READY
        assert ready["protocol"] == vg.VOICE_PROTOCOL
        assert ready["session_id"] == "sess-42"
        assert ready["client_events"] == list(vg.CLIENT_EVENTS)
        assert set(vg.SERVER_EVENTS) <= set(ready["server_events"])
        assert ready["respond_via"]["approval"] == vg.APPROVAL_RESPOND_PATH
        assert ready["respond_via"]["transcribe"] == vg.TRANSCRIBE_PATH
    finally:
        client.close()


def test_ping_is_answered_with_pong(voice_server, monkeypatch):
    port, token = voice_server
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: _FakeLoopback([]))
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "ping"})
        assert client.recv_json()["type"] == vg.EVENT_PONG
        # protocol-level ping too (not JSON)
        client.send_frame(vg.OP_PING, b"hb")
        message = client.recv()
        assert message.kind == "pong" and message.payload == b"hb"
    finally:
        client.close()


def test_unknown_client_event_answers_agent_error(voice_server, monkeypatch):
    port, token = voice_server
    loopback = _FakeLoopback([])
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "agent.text", "text": "hi"})
        error = client.recv_json()
        assert error["type"] == vg.EVENT_ERROR
        assert error["code"] == "unknown_event"
        assert loopback.started == []          # nothing was driven
    finally:
        client.close()


def test_audio_frames_are_refused_with_pointer_to_transcribe(voice_server, monkeypatch):
    port, token = voice_server
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: _FakeLoopback([]))
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "user.audio", "audio": "AAAA"})
        error = client.recv_json()
        assert error["code"] == "audio_not_supported"
        assert error["transcribe_via"] == vg.TRANSCRIBE_PATH
        client.send_frame(vg.OP_BINARY, b"\x00\x01")
        assert client.recv_json()["code"] == "binary_not_supported"
    finally:
        client.close()


def test_non_json_frame_is_rejected_without_dropping_the_socket(voice_server, monkeypatch):
    port, token = voice_server
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: _FakeLoopback([]))
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        client.send_frame(vg.OP_TEXT, b"not json at all")
        assert client.recv_json()["code"] == "invalid_json"
        client.send_text({"type": "ping"})
        assert client.recv_json()["type"] == vg.EVENT_PONG
    finally:
        client.close()


def test_unmasked_client_frame_gets_protocol_error_close(voice_server, monkeypatch):
    port, token = voice_server
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: _FakeLoopback([]))
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        # Server-side decoder requires masked client frames (RFC 6455 §5.1).
        client.sock.sendall(vg.encode_frame(vg.OP_TEXT, b'{"type":"ping"}'))
        notice = client.recv_json()
        assert notice["type"] == vg.EVENT_PROTOCOL_ERROR
        assert notice["code"] == "ws_protocol_error"
        message = client.recv()
        assert message.kind == "close"
        assert message.code == vg.CLOSE_PROTOCOL_ERROR
    finally:
        client.close()


def test_empty_text_is_rejected(voice_server, monkeypatch):
    port, token = voice_server
    loopback = _FakeLoopback([])
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "user.text", "text": "   "})
        assert client.recv_json()["code"] == "empty_text"
        assert loopback.started == []
    finally:
        client.close()


# ── 3. a full turn over the wire ─────────────────────────────────────────
def test_turn_streams_deltas_then_finished(voice_server, monkeypatch):
    port, token = voice_server
    loopback = _FakeLoopback([
        ("token", {"text": "При"}),
        ("reasoning", {"text": "think"}),
        ("token", {"text": "вет"}),
        ("tool", {"name": "terminal", "id": "c1"}),
        ("tool_complete", {"name": "terminal", "id": "c1"}),
        ("done", {"session_id": "sess-9", "status": "ok"}),
        ("stream_end", {"session_id": "sess-9"}),
    ])
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)

    client = WsClient(port, f"{vg.WS_PATH}?session_id=sess-9", headers=_bearer(token))
    try:
        assert client.recv_json()["type"] == vg.EVENT_SESSION_READY
        client.send_text({"type": "user.text", "text": "привет"})

        started = client.recv_until(vg.EVENT_TURN_STARTED)
        assert started["session_id"] == "sess-9"
        assert started["stream_id"] == "run-1"

        frames = client.collect_turn()
        assert [f["type"] for f in frames] == [
            vg.EVENT_TEXT_DELTA,
            vg.EVENT_REASONING_DELTA,
            vg.EVENT_TEXT_DELTA,
            vg.EVENT_TOOL,
            vg.EVENT_TOOL_FINISHED,
            vg.EVENT_FINISHED,
        ]
        assert [f["text"] for f in frames if f["type"] == vg.EVENT_TEXT_DELTA] == ["При", "вет"]
        assert "".join(f["text"] for f in frames
                       if f["type"] == vg.EVENT_TEXT_DELTA) == "Привет"
        assert frames[-1]["reason"] == "done"
        assert all(f["session_id"] == "sess-9" for f in frames)
        assert all(f["turn_id"] == started["turn_id"] for f in frames)
        assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))

        # exactly one terminal frame for the turn
        assert sum(1 for f in frames if f["type"] == vg.EVENT_FINISHED) == 1
        assert loopback.started == [{"session_id": "sess-9", "message": "привет"}]
        assert loopback.streamed == ["run-1"]
    finally:
        client.close()


def test_second_turn_reuses_the_bound_session_without_a_new_upgrade(
    voice_server, monkeypatch
):
    port, token = voice_server
    loopback = _FakeLoopback([("token", {"text": "one"}), ("done", {"session_id": "s1"})])
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, f"{vg.WS_PATH}?session_id=s1", headers=_bearer(token))
    try:
        client.recv_json()
        for text in ("первый", "второй"):
            client.send_text({"type": "user.text", "text": text})
            client.recv_until(vg.EVENT_TURN_STARTED)
            client.collect_turn()
        assert [call["session_id"] for call in loopback.started] == ["s1", "s1"]
        assert [call["message"] for call in loopback.started] == ["первый", "второй"]
        # distinct turn ids per turn
        assert loopback.streamed == ["run-1", "run-1"]
    finally:
        client.close()


def test_turn_started_carries_the_session_created_when_none_was_supplied(
    voice_server, monkeypatch
):
    port, token = voice_server
    loopback = _FakeLoopback(
        [("done", {"session_id": "brand-new"})],
        start_payload={"session_id": "brand-new", "stream_id": "run-7"},
    )
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        assert client.recv_json()["session_id"] is None
        client.send_text({"type": "user.text", "text": "hi"})
        started = client.recv_until(vg.EVENT_TURN_STARTED)
        assert started["session_id"] == "brand-new"
        frames = client.collect_turn()
        assert frames[-1]["session_id"] == "brand-new"
        assert loopback.started[0]["session_id"] is None
    finally:
        client.close()


def test_cancel_uses_the_running_stream_id(voice_server, monkeypatch):
    port, token = voice_server
    hold = threading.Event()
    loopback = _FakeLoopback(
        [("token", {"text": "slow"}), ("done", {"session_id": "s1"})], hold=hold,
    )
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, f"{vg.WS_PATH}?session_id=s1", headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "user.text", "text": "hi"})
        client.recv_until(vg.EVENT_TURN_STARTED)
        assert client.recv_json()["type"] == vg.EVENT_TEXT_DELTA
        client.send_text({"type": "cancel"})
        status = client.recv_json()
        assert (status["type"], status["phase"]) == (vg.EVENT_STATUS, "cancelling")
        assert status["stream_id"] == "run-1"
        assert loopback.cancelled == ["run-1"]
        # the turn still terminates exactly once
        finished = client.recv_json()
        assert finished["type"] == vg.EVENT_FINISHED
        assert finished["reason"] == "done"
    finally:
        hold.set()
        client.close()


def test_cancel_with_no_active_turn_reports_idle(voice_server, monkeypatch):
    port, token = voice_server
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: _FakeLoopback([]))
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "cancel"})
        status = client.recv_json()
        assert status["type"] == vg.EVENT_STATUS and status["phase"] == "idle"
    finally:
        client.close()


def test_rejected_turn_surfaces_the_runtime_status_verbatim(voice_server, monkeypatch):
    port, token = voice_server
    loopback = _FakeLoopback(
        [], start_status=409,
        start_payload={"error": "session is read-only", "code": "read_only_session"},
    )
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, f"{vg.WS_PATH}?session_id=imported", headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "user.text", "text": "hi"})
        error = client.recv_json()
        assert error["type"] == vg.EVENT_ERROR
        assert error["status"] == 409
        assert error["code"] == "read_only_session"
        assert error["message"] == "session is read-only"
        finished = client.recv_json()
        assert finished["type"] == vg.EVENT_FINISHED
        assert finished["reason"] == "error"
        assert finished["session_id"] == "imported"
    finally:
        client.close()


def test_relay_dying_without_done_still_finishes_the_turn(voice_server, monkeypatch):
    port, token = voice_server
    loopback = _FakeLoopback(
        [("token", {"text": "partial"})],
        stream_error=vg.ChatLoopbackError("stream status 404", status=404),
    )
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, f"{vg.WS_PATH}?session_id=s1", headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "user.text", "text": "hi"})
        client.recv_until(vg.EVENT_TURN_STARTED)
        frames = client.collect_turn()
        assert [f["type"] for f in frames] == [vg.EVENT_ERROR, vg.EVENT_FINISHED]
        assert frames[0]["code"] == "stream_rejected"
        assert frames[1]["reason"] == "error"

        # the connection stays usable for the next turn
        client.send_text({"type": "ping"})
        assert client.recv_json()["type"] == vg.EVENT_PONG
    finally:
        client.close()


def test_turn_without_a_stream_id_errors_instead_of_hanging(voice_server, monkeypatch):
    port, token = voice_server
    loopback = _FakeLoopback([], start_payload={"session_id": "s1"})
    monkeypatch.setattr(vg, "_default_client_factory", lambda handler, parsed: loopback)
    client = WsClient(port, vg.WS_PATH, headers=_bearer(token))
    try:
        client.recv_json()
        client.send_text({"type": "user.text", "text": "hi"})
        error = client.recv_json()
        assert error["type"] == vg.EVENT_ERROR
        assert error["code"] == "no_stream_id"
        assert client.recv_json()["type"] == vg.EVENT_FINISHED
    finally:
        client.close()


# ── 4. the real loopback hop against stubbed chat endpoints ──────────────
@pytest.fixture()
def stubbed_chat_runtime(monkeypatch):
    """Replace only the two chat handlers the gateway calls over loopback."""
    from api import routes

    calls = {"start": [], "stream": []}

    def fake_chat_start(handler, body, diag=None):
        calls["start"].append(dict(body))
        return _sentinel_bytes(handler, 200, {
            "status": "started",
            "session_id": body.get("session_id") or "loopback-session",
            "stream_id": "loopback-run",
        })

    def fake_sse_stream(handler, parsed):
        query = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(parsed.query)
        calls["stream"].append(query.get("stream_id", [""])[0])
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        # Two chunks, split mid-event, to prove the client-side SSE parser
        # buffers partial frames exactly like a real relay.
        blob = _sse_bytes([
            ("token", {"text": "lo"}),
            ("token", {"text": "opback"}),
            ("done", {"session_id": "loopback-session", "status": "ok"}),
        ])
        handler.wfile.write(blob[:20])
        handler.wfile.flush()
        time.sleep(0.05)
        handler.wfile.write(blob[20:])
        handler.wfile.flush()
        handler.close_connection = True
        return True

    monkeypatch.setattr(routes, "_handle_chat_start", fake_chat_start)
    monkeypatch.setattr(routes, "_handle_sse_stream", fake_sse_stream)
    return calls


def test_real_loopback_hop_drives_the_existing_chat_endpoints(
    voice_server, stubbed_chat_runtime
):
    """No factory patching: the real ChatLoopbackClient + real HTTP + real auth."""
    port, token = voice_server
    client = WsClient(port, f"{vg.WS_PATH}?session_id=sess-loop", headers=_bearer(token))
    try:
        assert client.recv_json()["type"] == vg.EVENT_SESSION_READY
        client.send_text({"type": "user.text", "text": "проверка"})
        started = client.recv_until(vg.EVENT_TURN_STARTED)
        assert started["stream_id"] == "loopback-run"
        frames = client.collect_turn()
        assert [f["type"] for f in frames] == [
            vg.EVENT_TEXT_DELTA, vg.EVENT_TEXT_DELTA, vg.EVENT_FINISHED,
        ]
        assert "".join(f["text"] for f in frames
                       if f["type"] == vg.EVENT_TEXT_DELTA) == "loopback"
    finally:
        client.close()
    assert stubbed_chat_runtime["start"] == [
        {"message": "проверка", "session_id": "sess-loop"}
    ]
    assert stubbed_chat_runtime["stream"] == ["loopback-run"]


def test_loopback_target_resolution_prefers_environment_override(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_VOICE_LOOPBACK", "http://127.0.0.1:9999")

    class _Srv:
        server_address = ("0.0.0.0", 8787)
        ssl_context = None

    class _H:
        server = _Srv()

    host, port, scheme, context = vg.resolve_loopback_target(_H())
    assert (host, port, scheme, context) == ("127.0.0.1", 9999, "http", None)


def test_loopback_target_resolution_maps_wildcard_bind_to_loopback(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_VOICE_LOOPBACK", raising=False)

    class _Srv:
        server_address = ("0.0.0.0", 12345)
        ssl_context = None

    class _H:
        server = _Srv()

    assert vg.resolve_loopback_target(_H())[:3] == ("127.0.0.1", 12345, "http")
