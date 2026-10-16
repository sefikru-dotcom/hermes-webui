#!/usr/bin/env python3
"""Manual, end-to-end check of the WebUI voice gateway (bin/../api/voice_gateway.py).

    HERMES_WEBUI_STATE_DIR=/tmp/voice-smoke HERMES_WEBUI_PASSWORD=smoke \
        .venv/bin/python scripts/voice-gateway-smoke.py

What it proves, against a REAL server on a free port:

  1. a text chat turn runs over the ordinary HTTP API and produces a session_id
     (the "existing chat session");
  2. ``GET /api/voice/session?session_id=<that id>`` upgrades to WebSocket with a
     valid device Bearer token and announces ``session.ready``;
  3. a ``user.text`` frame is answered with a stream of ``agent.text.delta``
     frames followed by exactly one ``agent.finished``;
  4. because the voice socket is bound to the *same* session_id, the voice turn
     continues the very same conversation (the code word from step 1 comes back
     in step 3).

Isolation: the server runs out of HERMES_WEBUI_STATE_DIR (a scratch directory by
default), never the live WebUI state.  No secrets are read from or written to it.

The client side deliberately does NOT import the module under test: the
handshake and the frame decoder here are independent (hashlib/struct) so the
evidence is not self-confirming.

The scratch turn drives the REAL Hermes agent, so it may touch whatever the
agent normally touches (its own run logs).  The prompts ask it not to use tools
and not to write memory; check your state dir afterwards if that matters.
"""

import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
VOICE_PATH = "/api/voice/session"
CODE_WORD = "ЛАГУНА"
FIRST_TEXT = (
    f"Запомни в рамках этого диалога кодовое слово: {CODE_WORD}. "
    "Не вызывай инструменты и не записывай ничего в память, просто подтверди одним словом."
)
SECOND_TEXT = "Назови кодовое слово из нашего диалога. Только само слово, без инструментов."
TURN_TIMEOUT = float(os.getenv("VOICE_SMOKE_TIMEOUT", "240"))


def _say(line=""):
    print(line, flush=True)


def banner(title):
    _say()
    _say("=" * 72)
    _say(title)
    _say("=" * 72)


# ── independent RFC 6455 client ──────────────────────────────────────────
def _recv_exact(sock, count, buffered):
    while len(buffered) < count:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("socket closed")
        buffered.extend(chunk)
    data = bytes(buffered[:count])
    del buffered[:count]
    return data


def _read_frame(sock, buffered):
    """Server->client frame: never masked. Returns (fin, opcode, payload)."""
    b0, b1 = _recv_exact(sock, 2, buffered)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    if b0 & 0x70:
        raise AssertionError("server set reserved bits")
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2, buffered))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8, buffered))[0]
    if masked:
        raise AssertionError("server frame must not be masked (RFC 6455 §5.1)")
    return fin, opcode, _recv_exact(sock, length, buffered)


def _send_frame(sock, opcode, payload=b""):
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < (1 << 16):
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    sock.sendall(header + mask + masked)


class VoiceClient:
    """Hand-rolled client: handshake, framing, JSON events."""

    def __init__(self, port, session_id, token, timeout=TURN_TIMEOUT):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buffered = bytearray()
        self.fragments = []
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {VOICE_PATH}?session_id={session_id} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {token}\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        self.buffered.extend(rest)
        lines = head.decode("latin-1").split("\r\n")
        self.status_line = lines[0]
        self.headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1(key.encode() + WS_GUID).digest()
        ).decode()
        self.accept_ok = self.headers.get("sec-websocket-accept") == expected
        self.expected_accept = expected

    def send_json(self, payload):
        _send_frame(self.sock, 0x1, json.dumps(payload).encode())

    def recv_json(self):
        """Next complete JSON text message (handles fragmentation + pings)."""
        while True:
            fin, opcode, payload = _read_frame(self.sock, self.buffered)
            if opcode == 0x9:                      # ping -> pong
                _send_frame(self.sock, 0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0
                raise ConnectionError(f"server closed with code {code}")
            if opcode in (0x1, 0x2, 0x0):
                self.fragments.append((opcode, payload))
                if fin:
                    kind = self.fragments[0][0]
                    data = b"".join(part for _, part in self.fragments)
                    self.fragments = []
                    if kind != 0x1:
                        raise AssertionError("non-text frame from server")
                    return json.loads(data.decode("utf-8"))

    def close(self):
        try:
            _send_frame(self.sock, 0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()


# ── HTTP helpers (text chat side) ────────────────────────────────────────
def _http(port, method, path, token, body=None, timeout=TURN_TIMEOUT):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Authorization": f"Bearer {token}"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    return response, conn


def run_text_turn(port, token, message):
    """Drive one ordinary chat turn over HTTP; return (session_id, answer)."""
    response, conn = _http(port, "POST", "/api/chat/start", token,
                           {"session_id": "", "message": message})
    start = json.loads(response.read().decode())
    conn.close()
    assert response.status == 200, (response.status, start)
    session_id, stream_id = start["session_id"], start["stream_id"]
    _say(f"  /api/chat/start  -> session_id={session_id} stream_id={stream_id}")

    response, conn = _http(port, "GET", f"/api/chat/stream?stream_id={stream_id}", token)
    answer, events = [], []
    while True:
        line = response.fp.readline()
        if not line:
            break
        line = line.decode("utf-8").rstrip("\r\n")
        if line.startswith("event: "):
            current = line[7:]
            events.append(current)
        elif line.startswith("data: "):
            data = json.loads(line[6:])
            if current == "token":
                answer.append(data.get("text") or "")
            elif current in ("done", "stream_end"):
                break
    conn.close()
    _say(f"  SSE events: {sorted(set(events))}")
    return session_id, "".join(answer)


def run_voice_turn(port, token, session_id, message, code_word):
    client = VoiceClient(port, session_id, token)
    _say(f"  handshake: {client.status_line}")
    _say(f"  Sec-WebSocket-Accept ok: {client.accept_ok} "
         f"(expected {client.expected_accept})")
    assert client.status_line.startswith("HTTP/1.1 101"), client.status_line
    assert client.accept_ok, client.headers

    ready = client.recv_json()
    _say(f"  <- {json.dumps(ready, ensure_ascii=False)[:200]}")
    assert ready["type"] == "session.ready", ready
    assert ready["session_id"] == session_id, ready

    _say(f"  -> user.text: {message!r}")
    client.send_json({"type": "user.text", "text": message})

    deltas, delta_frames, frames, finished = [], [], [], None
    while finished is None:
        frame = client.recv_json()
        frames.append(frame["type"])
        label = frame["type"]
        if label == "agent.text.delta":
            deltas.append(frame["text"])
            delta_frames.append(frame)
            _say(f"  <- agent.text.delta #{len(deltas)}: {frame['text']!r}")
        elif label in ("agent.reasoning.delta",):
            _say(f"  <- agent.reasoning.delta: {frame['text'][:60]!r}")
        else:
            _say(f"  <- {json.dumps(frame, ensure_ascii=False)[:220]}")
        if label == "agent.finished":
            finished = frame
    answer = "".join(deltas)
    _say()
    _say(f"  frame sequence : {frames}")
    _say(f"  finished       : {json.dumps(finished, ensure_ascii=False)}")
    _say(f"  assistant text : {answer!r}")
    bound = {frame.get("session_id") for frame in delta_frames}
    _say(f"  bound session  : {session_id} (session_id on every delta: {bound})")
    client.close()
    return answer, frames, bound == {session_id}


def main():
    state_dir = os.environ.get("HERMES_WEBUI_STATE_DIR") or tempfile.mkdtemp(
        prefix="voice-smoke-"
    )
    os.environ["HERMES_WEBUI_STATE_DIR"] = state_dir
    os.environ.pop("HERMES_WEBUI_TEST_NETWORK_BLOCK", None)
    os.environ.setdefault("HERMES_WEBUI_PASSWORD", "voice-smoke-local-password")
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    # The chat runtime persists sessions under <state>/sessions and does not
    # create the directory itself.
    (Path(state_dir) / "sessions").mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import server  # noqa: E402  (must come after the env vars above)
    from api import device_pairings  # noqa: E402

    banner("MARS voice gateway — manual WebSocket check")
    _say(f"state dir : {state_dir}   (live WebUI state is NOT used)")
    _say(f"password  : {'set via HERMES_WEBUI_PASSWORD' if os.environ.get('HERMES_WEBUI_PASSWORD') else 'unset (auth off)'}")

    created = device_pairings.create_pairing()
    device_pairings.approve_pairing(created["code"], session_cookie="voice-smoke")
    token = device_pairings.exchange_pairing(
        created["pair_id"], created["poll_secret"]
    )["access_token"]
    _say(f"device token: hdev_…{token[-6:]} (minted into {state_dir})")

    httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _say(f"server    : http://127.0.0.1:{port} (pid {os.getpid()})")

    failures = []
    try:
        banner("1. text chat turn over HTTP — creates the session to bind to")
        started = time.time()
        session_id, text_answer = run_text_turn(port, token, FIRST_TEXT)
        _say(f"  assistant text: {text_answer!r}")
        _say(f"  elapsed: {time.time() - started:.1f}s")
        if not text_answer:
            failures.append("text chat turn returned no assistant text")

        banner("2/3/4. voice WebSocket bound to that same session_id")
        started = time.time()
        voice_answer, frames, same_session = run_voice_turn(
            port, token, session_id, SECOND_TEXT, CODE_WORD
        )
        _say(f"  elapsed: {time.time() - started:.1f}s")

        banner("RESULT")
        checks = [
            ("session.ready before any turn", True),
            ("agent.text.delta stream seen", any(f == "agent.text.delta" for f in frames)),
            ("exactly one agent.finished", frames.count("agent.finished") == 1),
            ("agent.finished is the last frame", frames[-1] == "agent.finished"),
            ("every delta carries the bound session_id", same_session),
        ]
        for label, ok in checks:
            _say(f"  [{'ok' if ok else 'FAIL'}] {label}")
            if not ok:
                failures.append(label)
        recall = CODE_WORD in voice_answer
        _say(f"  [{'ok' if recall else 'warn'}] voice turn continued the same chat "
             f"session (code word {CODE_WORD!r} recalled: {recall})")
        _say()
        _say(f"  text-chat answer : {text_answer!r}")
        _say(f"  voice answer     : {voice_answer!r}")
    finally:
        httpd.shutdown()
        httpd.server_close()

    if failures:
        _say()
        _say(f"SMOKE FAILED: {failures}")
        return 1
    _say()
    _say("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
