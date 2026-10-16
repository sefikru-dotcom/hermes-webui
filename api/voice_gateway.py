"""Voice gateway — ``/api/voice/session`` WebSocket transport adapter.

What this module is
-------------------
A *transport* adapter.  It upgrades ``GET /api/voice/session`` to a WebSocket
(RFC 6455 is implemented here by hand because the WebUI backend is a
synchronous ``http.server.ThreadingHTTPServer`` and ships no WebSocket
support), then drives the **existing** chat runtime — it never re-implements
it.

Why a loopback hop instead of imported internals
-----------------------------------------------
Every turn this gateway runs is started and observed through the very same
HTTP endpoints the text chat uses::

    POST /api/chat/start      -> starts the run, returns stream_id/session_id
    GET  /api/chat/stream     -> live SSE relay of the run (token/tool/approval/...)
    GET  /api/chat/cancel     -> cancels the active run

Those loopback requests carry the caller's **own** credentials verbatim (the
``Authorization`` device Bearer and/or the session cookie from the upgraded
connection).  Consequences, all deliberate:

* the voice client gets exactly the text client's rights — same ``session_id``,
  same memory, same workspace, same visibility/profile checks, same
  approval/clarify gate for dangerous actions.  There is no second agent and
  no privileged shortcut;
* an approval or clarify request raised mid-turn is *relayed* to the voice
  client as ``agent.approval.request`` / ``agent.clarify.request`` with the
  endpoint to answer on (``/api/approval/respond``, ``/api/clarify/respond``,
  both already device-Bearer allow-listed).  The gateway deliberately offers
  no ``approve`` message type, so a voice session cannot answer an approval
  through a path the text client does not have;
* a rejected turn (unknown session, read-only import, active run, stale
  runtime) surfaces as ``agent.error`` with the runtime's own status and
  message — the gateway does not invent its own copy of those rules.

Contract (v1, protocol ``hermes.voice.v1``), all frames are JSON text frames::

    server -> client            client -> server
    ------------------          ------------------
    session.ready               {"type":"user.text","text":"..."}
    turn.started                {"type":"user.text","text":"...","session_id":"..."}
    agent.text.delta            {"type":"cancel"}
    agent.text.interim          {"type":"ping"}
    agent.reasoning.delta       {"type":"close"}
    agent.tool
    agent.tool.finished
    agent.approval.request
    agent.clarify.request
    agent.status
    agent.finished              (terminal, exactly once per turn)
    agent.error
    pong                        (answer to {"type":"ping"})

Unknown client message types answer with ``agent.error``
(``code="unknown_event"``) instead of being ignored silently, and audio is not
transported here on purpose: the client transcribes first via the existing
``POST /api/transcribe`` and sends text.  See
``docs/voice-gateway-contract.md``.

Security notes
--------------
* The handshake authenticates through the normal ``check_auth`` path: the route
  is in ``NATIVE_BEARER_EXACT_PATHS`` (device Bearer) and also accepts a
  browser session cookie.  Nothing about this route is public.
* Cookie-credentialed (browser) upgrades additionally require a same-origin
  ``Origin``/``Referer`` — a WebSocket handshake is not covered by CORS, so
  without that check any web page could open an authenticated socket.  A
  Bearer-authenticated upgrade is exempt because the WebSocket API cannot set
  an ``Authorization`` header, so such a handshake cannot come from a page.
* No secret is logged.  Credentials are only relayed to ``127.0.0.1``-loopback.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import logging
import os
import ssl
import struct
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)

# ── route / protocol identity ────────────────────────────────────────────
WS_PATH = "/api/voice/session"
VOICE_PROTOCOL = "hermes.voice.v1"

# ── framing limits ───────────────────────────────────────────────────────
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

_DATA_OPCODES = (OP_CONTINUATION, OP_TEXT, OP_BINARY)
_CONTROL_OPCODES = (OP_CLOSE, OP_PING, OP_PONG)
_VALID_OPCODES = frozenset(_DATA_OPCODES + _CONTROL_OPCODES)
MAX_CONTROL_PAYLOAD = 125

DEFAULT_MAX_FRAME_BYTES = 1 << 20  # 1 MiB — bounds a single frame
DEFAULT_MAX_MESSAGE_BYTES = 1 << 16  # 64 KiB — bounds an assembled client message

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_UNSUPPORTED_DATA = 1003
CLOSE_TOO_LARGE = 1009


class WsProtocolError(Exception):
    """A peer violated RFC 6455; carries the close code to answer with."""

    def __init__(self, message: str, *, code: int = CLOSE_PROTOCOL_ERROR):
        super().__init__(message)
        self.code = code


# ─────────────────────────────────────────────────────────────────────────
# 1. RFC 6455 handshake helpers (pure)
# ─────────────────────────────────────────────────────────────────────────
def compute_accept(key: str) -> str:
    """``Sec-WebSocket-Accept`` = base64(sha1(key + GUID)) (RFC 6455 §4.2.2)."""
    digest = hashlib.sha1(str(key).encode("ascii") + WS_GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def is_valid_ws_key(key: str) -> bool:
    """A client key must be base64 of exactly 16 bytes (RFC 6455 §4.1)."""
    if not key:
        return False
    try:
        raw = base64.b64decode(str(key).strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) == 16


def header_has_token(handler, header_name: str, token: str) -> bool:
    """Case-insensitive containment of a comma-separated header token."""
    try:
        raw = handler.headers.get(header_name) or ""
    except Exception:
        return False
    wanted = str(token).strip().lower()
    return wanted in {part.strip().lower() for part in str(raw).split(",")}


def is_websocket_upgrade(handler) -> bool:
    """True when the request headers ask for a WebSocket upgrade."""
    return (
        header_has_token(handler, "Upgrade", "websocket")
        and header_has_token(handler, "Connection", "upgrade")
    )


def build_handshake_headers(handler, *, subprotocol: str | None = None) -> list[tuple[str, str]]:
    """Return the response headers for a 101 response.

    Raises :class:`WsProtocolError` when the version or key is unusable.
    """
    version = ""
    try:
        version = (handler.headers.get("Sec-WebSocket-Version") or "").strip()
    except Exception:
        version = ""
    if version != "13":
        raise WsProtocolError("unsupported websocket version", code=CLOSE_PROTOCOL_ERROR)
    key = ""
    try:
        key = (handler.headers.get("Sec-WebSocket-Key") or "").strip()
    except Exception:
        key = ""
    if not is_valid_ws_key(key):
        raise WsProtocolError("invalid Sec-WebSocket-Key", code=CLOSE_PROTOCOL_ERROR)
    headers = [
        ("Upgrade", "websocket"),
        ("Connection", "Upgrade"),
        ("Sec-WebSocket-Accept", compute_accept(key)),
        ("Sec-WebSocket-Version", "13"),
    ]
    if subprotocol:
        headers.append(("Sec-WebSocket-Protocol", subprotocol))
    return headers


def negotiate_subprotocol(handler, *, offered: str = VOICE_PROTOCOL) -> str | None:
    """Echo the voice subprotocol only when the client actually offered it."""
    try:
        raw = handler.headers.get("Sec-WebSocket-Protocol") or ""
    except Exception:
        return None
    offered_set = {p.strip() for p in str(raw).split(",") if p.strip()}
    return offered if offered in offered_set else None


# ─────────────────────────────────────────────────────────────────────────
# 2. RFC 6455 framing (pure)
# ─────────────────────────────────────────────────────────────────────────
def apply_mask(payload: bytes, mask_key: bytes) -> bytes:
    """XOR a payload with a 4-byte masking key (client frames are masked)."""
    if not payload:
        return b""
    key = bytes(mask_key)
    if len(key) != 4:
        raise ValueError("mask key must be 4 bytes")
    # bytes(x ^ key[i & 3]) — no numpy/toolkit dependency, correct for all sizes.
    return bytes(b ^ key[i & 3] for i, b in enumerate(payload))


def encode_frame(
    opcode: int,
    payload: bytes | bytearray | memoryview = b"",
    *,
    fin: bool = True,
    mask: bool = False,
    mask_key: bytes | None = None,
) -> bytes:
    """Serialise one frame. Server→client frames are never masked."""
    data = bytes(payload or b"")
    if opcode not in _VALID_OPCODES:
        raise ValueError(f"invalid opcode {opcode!r}")
    if opcode in _CONTROL_OPCODES:
        if not fin:
            raise ValueError("control frames must not be fragmented")
        if len(data) > MAX_CONTROL_PAYLOAD:
            raise ValueError("control frame payload too large")
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(data)
    header = bytearray([first])
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + data
    key = bytes(mask_key) if mask_key is not None else os.urandom(4)
    if len(key) != 4:
        raise ValueError("mask key must be 4 bytes")
    return bytes(header) + key + apply_mask(data, key)


def encode_close(code: int = CLOSE_NORMAL, reason: str = "") -> bytes:
    """Build a close frame payload+frame; empty code means 'no status'."""
    payload = b""
    if code:
        payload = struct.pack("!H", int(code)) + str(reason or "").encode("utf-8")[:123]
    return encode_frame(OP_CLOSE, payload)


def parse_close_payload(payload: bytes) -> tuple[int, str]:
    """Validate a close payload: (code, reason). 0 means 'no status present'."""
    data = bytes(payload or b"")
    if not data:
        return 0, ""
    if len(data) == 1:
        raise WsProtocolError("close frame with 1-byte payload")
    code = struct.unpack("!H", data[:2])[0]
    if code < 1000 or code in (1004, 1005, 1006) or (1015 < code < 3000) or code > 4999:
        raise WsProtocolError(f"invalid close code {code}")
    try:
        reason = data[2:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WsProtocolError("close reason is not valid utf-8") from exc
    return code, reason


class Frame:
    """One decoded RFC 6455 frame."""

    __slots__ = ("opcode", "payload", "fin")

    def __init__(self, opcode: int, payload: bytes = b"", fin: bool = True):
        self.opcode = opcode
        self.payload = payload
        self.fin = fin

    def __eq__(self, other):
        return (
            isinstance(other, Frame)
            and other.opcode == self.opcode
            and other.payload == self.payload
            and other.fin == self.fin
        )

    def __repr__(self):  # pragma: no cover - debug only
        return f"Frame(opcode=0x{self.opcode:x}, fin={self.fin}, payload={self.payload!r})"


class FrameDecoder:
    """Incremental, socket-free RFC 6455 frame parser (server side).

    ``feed()`` accepts arbitrary byte chunks (TCP boundaries are irrelevant)
    and returns every complete frame available so far.  Protocol violations
    raise :class:`WsProtocolError` carrying the close code to answer with; the
    decoder state is *not* reused after a raise.
    """

    def __init__(
        self,
        *,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        require_mask: bool = True,
    ):
        self._buffer = bytearray()
        self.max_frame_bytes = int(max_frame_bytes)
        self.require_mask = bool(require_mask)

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes | bytearray) -> list[Frame]:
        if chunk:
            self._buffer.extend(chunk)
        frames: list[Frame] = []
        buf = self._buffer
        while True:
            start_len = len(buf)
            if start_len < 2:
                break
            b0 = buf[0]
            b1 = buf[1]
            fin = bool(b0 & 0x80)
            if b0 & 0x70:
                raise WsProtocolError("reserved bits must be zero")
            opcode = b0 & 0x0F
            if opcode not in _VALID_OPCODES:
                raise WsProtocolError(f"reserved opcode 0x{opcode:x}")
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            is_control = opcode in _CONTROL_OPCODES
            if is_control:
                if not fin:
                    raise WsProtocolError("fragmented control frame")
                if length > MAX_CONTROL_PAYLOAD:
                    raise WsProtocolError("control frame payload too large")
            if self.require_mask and not masked:
                raise WsProtocolError("client frames must be masked")
            offset = 2
            if length == 126:
                if start_len < 4:
                    break
                length = struct.unpack("!H", bytes(buf[2:4]))[0]
                offset = 4
            elif length == 127:
                if start_len < 10:
                    break
                length = struct.unpack("!Q", bytes(buf[2:10]))[0]
                offset = 10
                if length & (1 << 63):
                    raise WsProtocolError("payload length must not set the MSB")
            if not is_control and length > self.max_frame_bytes:
                raise WsProtocolError("frame too large", code=CLOSE_TOO_LARGE)
            mask_key = b""
            if masked:
                if start_len < offset + 4:
                    break
                mask_key = bytes(buf[offset : offset + 4])
                offset += 4
            if start_len < offset + length:
                break
            payload = bytes(buf[offset : offset + length])
            del buf[: offset + length]
            if mask_key:
                payload = apply_mask(payload, mask_key)
            frames.append(Frame(opcode=opcode, payload=payload, fin=fin))
        return frames


class WsMessage:
    """A complete peer message assembled from one or more frames."""

    __slots__ = ("kind", "payload", "code", "reason")

    def __init__(self, kind: str, payload: bytes = b"", *, code: int = 0, reason: str = ""):
        self.kind = kind  # 'text' | 'binary' | 'close' | 'ping' | 'pong'
        self.payload = payload
        self.code = code
        self.reason = reason

    def text(self) -> str:
        return self.payload.decode("utf-8")

    def __repr__(self):  # pragma: no cover - debug only
        return f"WsMessage(kind={self.kind!r}, code={self.code}, len={len(self.payload)})"


class MessageAssembler:
    """Frame stream -> complete peer messages (pure, stateful).

    Enforces: control frames are standalone, continuation frames must follow a
    started message, data frames must not interleave, text messages are valid
    UTF-8, and the assembled message stays under ``max_message_bytes``.
    """

    def __init__(self, *, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES):
        self.max_message_bytes = int(max_message_bytes)
        self._fragments: list[bytes] = []
        self._fragment_opcode: int | None = None
        self._fragment_bytes = 0

    def reset(self) -> None:
        self._fragments = []
        self._fragment_opcode = None
        self._fragment_bytes = 0

    def feed(self, frame: Frame) -> list[WsMessage]:
        opcode = frame.opcode
        if opcode in _CONTROL_OPCODES:
            if not frame.fin:
                raise WsProtocolError("fragmented control frame")
            if opcode == OP_CLOSE:
                code, reason = parse_close_payload(frame.payload)
                return [WsMessage("close", b"", code=code, reason=reason)]
            if opcode == OP_PING:
                return [WsMessage("ping", frame.payload)]
            return [WsMessage("pong", frame.payload)]
        if opcode == OP_CONTINUATION:
            if self._fragment_opcode is None:
                raise WsProtocolError("continuation frame without a started message")
            self._fragments.append(frame.payload)
            self._fragment_bytes += len(frame.payload)
        else:
            if self._fragment_opcode is not None:
                raise WsProtocolError("new data frame while a message is fragmented")
            self._fragments = [frame.payload]
            self._fragment_bytes = len(frame.payload)
            self._fragment_opcode = opcode
        if self._fragment_bytes > self.max_message_bytes:
            self.reset()
            raise WsProtocolError("message too large", code=CLOSE_TOO_LARGE)
        if not frame.fin:
            return []
        opcode = self._fragment_opcode
        payload = b"".join(self._fragments)
        self.reset()
        if opcode == OP_BINARY:
            return [WsMessage("binary", payload)]
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WsProtocolError("text message is not valid utf-8") from exc
        return [WsMessage("text", payload)]


# ─────────────────────────────────────────────────────────────────────────
# 3. SSE parsing (pure) — the chat run relay is an SSE endpoint
# ─────────────────────────────────────────────────────────────────────────
class SseDecoder:
    """Incremental ``text/event-stream`` parser: bytes in, events out.

    Returns ``(event_name, data_string)`` tuples.  Comment lines (``:ka``)
    are dropped, ``data:`` lines are joined with ``\\n`` (SSE spec), ``event:``
    sets the name and defaults to ``message``, CRLF and LF endings are both
    accepted, and a partial trailing line is buffered until the next chunk.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._event = ""
        self._data: list[str] = []

    def feed(self, chunk: bytes | str) -> list[tuple[str, str]]:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            text = bytes(chunk).decode("utf-8", errors="replace")
        else:
            text = str(chunk)
        self._buffer += text
        out: list[tuple[str, str]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            if line == "":
                if self._data:
                    out.append((self._event or "message", "\n".join(self._data)))
                self._event = ""
                self._data = []
                continue
            if line.startswith(":"):
                continue  # comment / keepalive
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                self._event = value
            elif field == "data":
                self._data.append(value)
            # id:/retry: and unknown fields are ignored — the voice contract
            # has no resume cursor; a fresh turn replays from the top.
        return out


def decode_sse_data(data: str) -> dict:
    """Best-effort JSON decode of an SSE ``data`` payload into a dict."""
    if not data:
        return {}
    try:
        parsed = json.loads(data)
    except (ValueError, TypeError):
        return {"text": data}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


# ─────────────────────────────────────────────────────────────────────────
# 4. chat-run -> voice-contract event mapping (pure, stateful per turn)
# ─────────────────────────────────────────────────────────────────────────
EVENT_SESSION_READY = "session.ready"
EVENT_TURN_STARTED = "turn.started"
EVENT_TEXT_DELTA = "agent.text.delta"
EVENT_TEXT_INTERIM = "agent.text.interim"
EVENT_REASONING_DELTA = "agent.reasoning.delta"
EVENT_TOOL = "agent.tool"
EVENT_TOOL_FINISHED = "agent.tool.finished"
EVENT_APPROVAL_REQUEST = "agent.approval.request"
EVENT_CLARIFY_REQUEST = "agent.clarify.request"
EVENT_STATUS = "agent.status"
EVENT_FINISHED = "agent.finished"
EVENT_ERROR = "agent.error"
EVENT_PONG = "pong"
EVENT_PROTOCOL_ERROR = "protocol.error"

SERVER_EVENTS = (
    EVENT_SESSION_READY,
    EVENT_TURN_STARTED,
    EVENT_TEXT_DELTA,
    EVENT_TEXT_INTERIM,
    EVENT_REASONING_DELTA,
    EVENT_TOOL,
    EVENT_TOOL_FINISHED,
    EVENT_APPROVAL_REQUEST,
    EVENT_CLARIFY_REQUEST,
    EVENT_STATUS,
    EVENT_FINISHED,
    EVENT_ERROR,
    EVENT_PONG,
    EVENT_PROTOCOL_ERROR,
)

CLIENT_EVENTS = ("user.text", "cancel", "ping", "close")

#: chat SSE events that terminate a turn.  ``done`` is the semantic end of the
#: answer; ``stream_end`` closes the relay.  Some replay paths deliver only
#: ``stream_end``, so both are mapped and the translator de-duplicates.
_FINISH_EVENTS = {
    "done": "done",
    "stream_end": "stream_end",
    "cancel": "cancelled",
}

APPROVAL_RESPOND_PATH = "/api/approval/respond"
CLARIFY_RESPOND_PATH = "/api/clarify/respond"
TRANSCRIBE_PATH = "/api/transcribe"


class VoiceEventTranslator:
    """Map one chat run's SSE events onto voice-gateway contract events.

    Pure and per-turn: no sockets, no globals.  Guarantees ``agent.finished``
    is emitted at most once for the turn (``done`` + ``stream_end`` both map to
    it) and can force a terminal event via :meth:`finish_pending` when the
    relay dies without one.
    """

    def __init__(self, *, turn_id: str, session_id: str | None = None):
        self.turn_id = turn_id
        self.session_id = session_id
        self._seq = 0
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def _emit(self, event_type: str, payload: dict | None = None) -> dict:
        self._seq += 1
        frame = {"type": event_type, "v": 1, "turn_id": self.turn_id, "seq": self._seq}
        if self.session_id:
            frame["session_id"] = self.session_id
        if payload:
            frame.update(payload)
        return frame

    def translate(self, event: str, data: dict | None = None) -> list[dict]:
        """Return zero or more contract frames for one chat SSE event."""
        data = data or {}
        if event in _FINISH_EVENTS:
            if self._finished:
                return []
            self._finished = True
            payload: dict = {"reason": _FINISH_EVENTS[event]}
            for key in ("summary", "status", "answer", "error", "code"):
                if data.get(key) is not None:
                    payload[key] = data[key]
            sid = data.get("session_id")
            if sid:
                payload["session_id"] = sid
                self.session_id = str(sid)
            return [self._emit(EVENT_FINISHED, payload)]
        if event == "token":
            text = data.get("text")
            if not isinstance(text, str) or text == "":
                return []
            return [self._emit(EVENT_TEXT_DELTA, {"text": text})]
        if event == "interim_assistant":
            text = data.get("text")
            if not isinstance(text, str) or text == "":
                return []
            return [self._emit(EVENT_TEXT_INTERIM, {"text": text})]
        if event == "reasoning":
            text = data.get("text")
            if not isinstance(text, str) or text == "":
                return []
            return [self._emit(EVENT_REASONING_DELTA, {"text": text})]
        if event == "tool":
            return [self._emit(EVENT_TOOL, {"name": data.get("name") or data.get("tool"),
                                            "tool": data})]
        if event == "tool_complete":
            return [self._emit(EVENT_TOOL_FINISHED, {"name": data.get("name") or data.get("tool"),
                                                     "tool": data})]
        if event == "approval":
            return [self._emit(EVENT_APPROVAL_REQUEST, {
                "approval": data,
                "respond_via": APPROVAL_RESPOND_PATH,
            })]
        if event == "clarify":
            return [self._emit(EVENT_CLARIFY_REQUEST, {
                "clarify": data,
                "respond_via": CLARIFY_RESPOND_PATH,
            })]
        if event in ("compressing", "compressed"):
            return [self._emit(EVENT_STATUS, {"phase": event})]
        if event == "warning":
            return [self._emit(EVENT_STATUS, {
                "level": "warning",
                "message": data.get("message") or data.get("text") or "",
            })]
        if event in ("error", "apperror"):
            payload = {"message": data.get("message") or data.get("error") or "agent error"}
            if data.get("code"):
                payload["code"] = data["code"]
            if data.get("retryable") is not None:
                payload["retryable"] = data["retryable"]
            return [self._emit(EVENT_ERROR, payload)]
        # title / title_status / state_saved / todo_state / metering / goal* /
        # pending_steer_leftover / bg_task_complete: no voice-contract meaning.
        return []

    def finish_pending(self, reason: str = "stream_closed") -> list[dict]:
        """Force the terminal frame when the relay ended without ``done``."""
        if self._finished:
            return []
        self._finished = True
        return [self._emit(EVENT_FINISHED, {"reason": reason})]


# ─────────────────────────────────────────────────────────────────────────
# 5. loopback chat client — the turn driver
# ─────────────────────────────────────────────────────────────────────────
class ChatLoopbackClient:
    """Drive the real chat HTTP endpoints over loopback with the caller's creds.

    Only these three already-existing endpoints are used; no chat logic lives
    here.  ``authorization``/``cookie`` are relayed verbatim from the upgraded
    connection so the loopback request carries the exact same identity.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        scheme: str = "http",
        authorization: str | None = None,
        cookie: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        start_timeout: float = 30.0,
        stream_timeout: float = 3600.0,
    ):
        self.host = host
        self.port = int(port)
        self.scheme = scheme
        self.authorization = authorization
        self.cookie = cookie
        self.ssl_context = ssl_context
        self.start_timeout = float(start_timeout)
        self.stream_timeout = float(stream_timeout)

    # -- plumbing ---------------------------------------------------------
    def _connect(self, timeout: float):
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout, context=self.ssl_context
            )
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        if self.cookie:
            headers["Cookie"] = self.cookie
        if extra:
            headers.update(extra)
        return headers

    # -- endpoints --------------------------------------------------------
    def start_turn(self, *, session_id: str | None, message: str) -> tuple[int, dict]:
        """``POST /api/chat/start`` -> (status, payload)."""
        body = json.dumps(
            {"message": message, "session_id": session_id or ""}
        ).encode("utf-8")
        headers = self._headers(
            {"Content-Type": "application/json", "Content-Length": str(len(body))}
        )
        conn = self._connect(self.start_timeout)
        try:
            conn.request("POST", "/api/chat/start", body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = int(response.status)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return status, payload

    def cancel_turn(self, stream_id: str) -> bool:
        """``GET /api/chat/cancel?stream_id=`` — same cancel path the UI uses."""
        if not stream_id:
            return False
        query = urllib.parse.urlencode({"stream_id": stream_id})
        conn = self._connect(self.start_timeout)
        try:
            conn.request("GET", f"/api/chat/cancel?{query}", headers=self._headers())
            response = conn.getresponse()
            response.read()
            return 200 <= int(response.status) < 300
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def iter_run_events(self, stream_id: str):
        """Yield ``(event, data_dict)`` from ``GET /api/chat/stream``.

        The connection is held open for the run's lifetime and closed on EOF;
        the SSE body has no Content-Length, so chunks arrive incrementally.
        Raises :class:`ChatLoopbackError` when the stream cannot be opened.
        """
        if not stream_id:
            raise ChatLoopbackError("stream_id required", status=0)
        query = urllib.parse.urlencode({"stream_id": stream_id})
        conn = self._connect(self.stream_timeout)
        try:
            conn.request(
                "GET",
                f"/api/chat/stream?{query}",
                headers=self._headers({"Accept": "text/event-stream"}),
            )
            response = conn.getresponse()
            status = int(response.status)
            if status != 200:
                raw = response.read()
                message = ""
                try:
                    message = (json.loads(raw.decode("utf-8")) or {}).get("error", "")
                except Exception:
                    message = raw.decode("utf-8", errors="replace")[:300]
                raise ChatLoopbackError(message or f"stream status {status}", status=status)
            decoder = SseDecoder()
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                for event, data in decoder.feed(chunk):
                    yield event, decode_sse_data(data)
        finally:
            try:
                conn.close()
            except Exception:
                pass


class ChatLoopbackError(RuntimeError):
    """The loopback chat request could not be served."""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = int(status)


def resolve_loopback_target(handler) -> tuple[str, int, str, ssl.SSLContext | None]:
    """Resolve (host, port, scheme, ssl_context) for the self-call.

    ``HERMES_WEBUI_VOICE_LOOPBACK`` (``http://host:port``) overrides the derived
    address; otherwise the bound server address is used, with a wildcard bind
    mapped to loopback.  TLS deployments self-call with an unverified context,
    because the loopback hop never leaves the host and the server's own
    certificate usually is not browser-trusted.
    """
    raw = (os.getenv("HERMES_WEBUI_VOICE_LOOPBACK") or "").strip()
    if raw:
        parsed = urllib.parse.urlparse(raw if "://" in raw else f"http://{raw}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or int(os.getenv("HERMES_WEBUI_PORT") or 8787)
        scheme = (parsed.scheme or "http").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported loopback scheme {scheme!r}")
        context = ssl._create_unverified_context() if scheme == "https" else None
        return host, port, scheme, context
    server = getattr(handler, "server", None)
    address = getattr(server, "server_address", None) or ("127.0.0.1", 8787)
    host = str(address[0] or "127.0.0.1")
    if host in ("0.0.0.0", "::", "", "::0"):
        host = "127.0.0.1"
    port = int(address[1] or 8787)
    tls = getattr(server, "ssl_context", None) is not None
    scheme = "https" if tls else "http"
    context = ssl._create_unverified_context() if tls else None
    return host, port, scheme, context


# ─────────────────────────────────────────────────────────────────────────
# 6. per-connection session
# ─────────────────────────────────────────────────────────────────────────
_KEEPALIVE_SECONDS = float(os.getenv("HERMES_WEBUI_VOICE_KEEPALIVE") or 25.0)
_IDLE_CLOSE_SECONDS = float(os.getenv("HERMES_WEBUI_VOICE_IDLE_TIMEOUT") or 300.0)
_READ_CHUNK = 8192


class VoiceSession:
    """One upgraded WebSocket connection bound to one chat session_id."""

    def __init__(
        self,
        handler,
        *,
        session_id: str | None,
        client: ChatLoopbackClient,
        keepalive_seconds: float = _KEEPALIVE_SECONDS,
        idle_close_seconds: float = _IDLE_CLOSE_SECONDS,
    ):
        self.handler = handler
        self.run_client = client
        self.session_id = (session_id or "").strip() or None
        self.keepalive_seconds = float(keepalive_seconds)
        self.idle_close_seconds = float(idle_close_seconds)
        self._decoder = FrameDecoder()
        self._assembler = MessageAssembler()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._last_inbound = time.time()
        self._active_stream_id: str | None = None
        self._turn_in_flight = False
        self._turn_counter = 0
        self._keepalive_thread: threading.Thread | None = None

    # -- write side -------------------------------------------------------
    def _write_raw(self, data: bytes) -> bool:
        with self._write_lock:
            if self._closed.is_set():
                return False
            try:
                self.handler.wfile.write(data)
                self.handler.wfile.flush()
                return True
            except Exception:
                self._closed.set()
                return False

    def send_json(self, payload: dict) -> bool:
        try:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - defensive
            encoded = json.dumps({"type": EVENT_ERROR, "code": "encoding_error"}).encode("utf-8")
        return self._write_raw(encode_frame(OP_TEXT, encoded))

    def send_ping(self, payload: bytes = b"") -> bool:
        return self._write_raw(encode_frame(OP_PING, payload))

    def send_pong(self, payload: bytes = b"") -> bool:
        return self._write_raw(encode_frame(OP_PONG, payload))

    def send_close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        self._write_raw(encode_close(code, reason))

    def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        if self._closed.is_set():
            return
        try:
            self.send_close(code, reason)
        finally:
            self._closed.set()

    # -- lifecycle --------------------------------------------------------
    def serve(self) -> None:
        """Run the connection loop until the peer closes or goes silent."""
        self.send_json({
            "type": EVENT_SESSION_READY,
            "v": 1,
            "protocol": VOICE_PROTOCOL,
            "session_id": self.session_id,
            "server_events": list(SERVER_EVENTS),
            "client_events": list(CLIENT_EVENTS),
            "respond_via": {
                "approval": APPROVAL_RESPOND_PATH,
                "clarify": CLARIFY_RESPOND_PATH,
                "transcribe": TRANSCRIBE_PATH,
            },
            "keepalive_seconds": self.keepalive_seconds,
            "ts": round(time.time(), 3),
        })
        self._start_keepalive()
        rfile = self.handler.rfile
        try:
            while not self._closed.is_set():
                # read1 (not read): return as soon as ANY bytes are available, so
                # small frames are never delayed until a 4 KB TCP fill.
                chunk = rfile.read1(_READ_CHUNK)
                if not chunk:
                    break
                self._last_inbound = time.time()
                try:
                    frames = self._decoder.feed(chunk)
                    for frame in frames:
                        for message in self._assembler.feed(frame):
                            self._handle_message(message)
                except WsProtocolError as exc:
                    self.send_json({
                        "type": EVENT_PROTOCOL_ERROR,
                        "v": 1,
                        "code": "ws_protocol_error",
                        "message": str(exc),
                    })
                    self.close(exc.code, str(exc)[:120])
                    return
                if self._closed.is_set():
                    break
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            self._stop_keepalive()

    def _start_keepalive(self) -> None:
        if self.keepalive_seconds <= 0:
            return

        def _loop():
            while not self._closed.wait(self.keepalive_seconds):
                idle = time.time() - self._last_inbound
                if self.idle_close_seconds and idle > self.idle_close_seconds:
                    logger.info(
                        "voice gateway: closing idle websocket session=%s idle=%.0fs",
                        self.session_id, idle,
                    )
                    self.close(CLOSE_GOING_AWAY, "idle timeout")
                    return
                if not self.send_ping(b"\x00"):
                    return

        self._keepalive_thread = threading.Thread(
            target=_loop, name="voice-ws-keepalive", daemon=True
        )
        self._keepalive_thread.start()

    def _stop_keepalive(self) -> None:
        thread = self._keepalive_thread
        self._keepalive_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)

    # -- inbound ----------------------------------------------------------
    def _handle_message(self, message: WsMessage) -> None:
        if message.kind == "close":
            code = message.code or CLOSE_NORMAL
            self._closed.set()
            self._write_raw(encode_close(code, ""))
            return
        if message.kind == "ping":
            self.send_pong(message.payload[:MAX_CONTROL_PAYLOAD])
            return
        if message.kind == "pong":
            return
        if message.kind == "binary":
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "code": "binary_not_supported",
                "message": "this endpoint speaks JSON text frames only",
            })
            return
        self._handle_text(message.text())

    def _handle_text(self, raw: str) -> None:
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "code": "invalid_json",
                "message": "client frames must be JSON objects",
            })
            return
        if not isinstance(payload, dict):
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "code": "invalid_json",
                "message": "client frames must be JSON objects",
            })
            return
        kind = str(payload.get("type") or "").strip()
        if kind == "ping":
            self.send_json({"type": EVENT_PONG, "v": 1, "ts": round(time.time(), 3)})
            return
        if kind == "user.text":
            self._start_turn(payload)
            return
        if kind == "cancel":
            self._cancel_turn()
            return
        if kind == "close":
            self.close(CLOSE_NORMAL, "client requested close")
            return
        if kind == "user.audio":
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "code": "audio_not_supported",
                "message": "transcribe audio via POST /api/transcribe, then send user.text",
                "transcribe_via": TRANSCRIBE_PATH,
            })
            return
        self.send_json({
            "type": EVENT_ERROR, "v": 1, "code": "unknown_event",
            "message": f"unsupported client event {kind!r}",
            "client_events": list(CLIENT_EVENTS),
        })

    # -- turns ------------------------------------------------------------
    def _start_turn(self, payload: dict) -> None:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "code": "empty_text",
                "message": "user.text requires a non-empty 'text' string",
            })
            return
        # A per-connection turn is serialised; whether the SESSION may run at
        # all stays the runtime's call (it answers with its own status).
        if self._turn_in_flight:
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "code": "turn_in_progress",
                "message": "a turn is already running on this voice session",
            })
            return
        requested = payload.get("session_id")
        if isinstance(requested, str) and requested.strip():
            self.session_id = requested.strip()
        self._turn_counter += 1
        turn_id = f"voice-{self._turn_counter}-{int(time.time() * 1000) % 100000}"
        self._turn_in_flight = True
        thread = threading.Thread(
            target=self._run_turn,
            args=(turn_id, text),
            name=f"voice-turn-{self._turn_counter}",
            daemon=True,
        )
        thread.start()

    def _cancel_turn(self) -> None:
        stream_id = self._active_stream_id
        if not stream_id:
            self.send_json({
                "type": EVENT_STATUS, "v": 1, "phase": "idle",
                "message": "no turn is running",
            })
            return
        try:
            self.run_client.cancel_turn(stream_id)
        except Exception as exc:  # pragma: no cover - transport noise
            logger.debug("voice gateway: cancel failed: %s", exc)
        self.send_json({"type": EVENT_STATUS, "v": 1, "phase": "cancelling",
                        "stream_id": stream_id})

    def _run_turn(self, turn_id: str, text: str) -> None:
        # The translator exists before the runtime is even asked, so every turn
        # — accepted, rejected or interrupted — ends with exactly one
        # agent.finished frame (see docs/voice-gateway.md).
        translator = VoiceEventTranslator(turn_id=turn_id, session_id=self.session_id)
        finish_reason = "stream_closed"
        try:
            status, payload = self.run_client.start_turn(
                session_id=self.session_id, message=text
            )
            if not (200 <= int(status) < 300):
                finish_reason = "error"
                self.send_json({
                    "type": EVENT_ERROR, "v": 1, "turn_id": turn_id,
                    "code": str(payload.get("code") or "turn_rejected"),
                    "status": int(status),
                    "message": str(payload.get("error") or "chat start rejected"),
                })
                return
            session_id = str(payload.get("session_id") or "").strip() or self.session_id
            stream_id = str(payload.get("stream_id") or "").strip()
            if session_id:
                self.session_id = session_id
                translator.session_id = session_id
            if not stream_id:
                finish_reason = "error"
                self.send_json({
                    "type": EVENT_ERROR, "v": 1, "turn_id": turn_id,
                    "code": "no_stream_id",
                    "message": "chat start returned no stream_id",
                })
                return
            self._active_stream_id = stream_id
            self.send_json({
                "type": EVENT_TURN_STARTED, "v": 1, "turn_id": turn_id,
                "session_id": session_id, "stream_id": stream_id,
            })
            for event, data in self.run_client.iter_run_events(stream_id):
                for frame in translator.translate(event, data):
                    if not self.send_json(frame):
                        return
                if translator.finished:
                    break
        except ChatLoopbackError as exc:
            finish_reason = "error"
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "turn_id": turn_id,
                "code": "stream_unavailable" if exc.status == 0 else "stream_rejected",
                "status": exc.status,
                "message": str(exc),
            })
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("voice gateway: turn %s failed: %s", turn_id, exc)
            finish_reason = "error"
            self.send_json({
                "type": EVENT_ERROR, "v": 1, "turn_id": turn_id,
                "code": "turn_failed",
                "message": exc.__class__.__name__,
            })
        finally:
            if not translator.finished:
                for frame in translator.finish_pending(finish_reason):
                    self.send_json(frame)
            self._active_stream_id = None
            self._turn_in_flight = False


# ─────────────────────────────────────────────────────────────────────────
# 7. route entry point (called from api/routes.handle_get)
# ─────────────────────────────────────────────────────────────────────────
def _default_client_factory(handler, parsed) -> ChatLoopbackClient:
    try:
        host, port, scheme, context = resolve_loopback_target(handler)
    except Exception:
        host, port, scheme, context = "127.0.0.1", int(os.getenv("HERMES_WEBUI_PORT") or 8787), "http", None
    authorization = None
    cookie = None
    try:
        authorization = (handler.headers.get("Authorization") or "").strip() or None
        cookie = (handler.headers.get("Cookie") or "").strip() or None
    except Exception:
        pass
    return ChatLoopbackClient(
        host=host, port=port, scheme=scheme,
        authorization=authorization, cookie=cookie,
        ssl_context=context,
    )


def _reject(handler, message: str, status: int, extra: dict | None = None) -> bool:
    from api.helpers import j as _json_response

    body = {"error": message}
    if extra:
        body.update(extra)
    _json_response(handler, body, status=status, extra_headers={"Connection": "close"})
    return True


def _write_error_frame(handler, payload: dict) -> bool:
    """Best-effort single JSON frame on an already-upgraded socket."""
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.wfile.write(encode_frame(OP_TEXT, data))
        handler.wfile.flush()
        return True
    except Exception:
        return False


def handle_voice_session(handler, parsed, *, client_factory=None) -> bool:
    """Handle ``GET /api/voice/session`` (WebSocket upgrade + voice contract).

    Always returns ``True`` — the route is terminal.  Returns a JSON error for
    a non-upgrade request instead of 404, so a misconfigured client sees the
    real reason.
    """
    if not is_websocket_upgrade(handler):
        return _reject(
            handler,
            "WebSocket upgrade required",
            400,
            {"hint": "GET /api/voice/session with Upgrade: websocket", "protocol": VOICE_PROTOCOL},
        )
    # A WebSocket handshake is not subject to CORS, so a cookie-credentialed
    # upgrade must prove same-origin explicitly (a page on another origin could
    # otherwise open an authenticated socket and drive the agent).  Bearer
    # upgrades are exempt: the browser WebSocket API cannot set an
    # Authorization header, so they cannot originate from a page.
    if not getattr(handler, "_native_auth_device", None):
        from api.routes import _check_same_origin_browser_request

        if not _check_same_origin_browser_request(handler):
            return _reject(handler, "Cross-origin request rejected", 403)
    try:
        headers = build_handshake_headers(
            handler, subprotocol=negotiate_subprotocol(handler)
        )
    except WsProtocolError as exc:
        return _reject(handler, str(exc), 400, {"protocol": VOICE_PROTOCOL})
    query = urllib.parse.parse_qs(parsed.query or "")
    session_id = (query.get("session_id", [""])[0] or "").strip()

    handler.send_response_only(101, "Switching Protocols")
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    try:
        handler.log_request(101)
    except Exception:
        pass
    # Hand the socket to the framing loop: blocking reads (setup() armed a 30 s
    # request timeout, far too short for a live voice session) and a
    # connection-scoped handler so the base class does not parse a second
    # request on this socket after we return.
    try:
        handler.connection.settimeout(None)
    except Exception:
        pass
    handler.close_connection = True

    factory = client_factory or _default_client_factory
    try:
        client = factory(handler, parsed)
    except Exception as exc:
        logger.warning("voice gateway: loopback client unavailable: %s", exc)
        _write_error_frame(handler, {
            "type": EVENT_ERROR, "v": 1, "code": "loopback_unavailable",
            "message": "chat runtime is not reachable from this process",
        })
        return True

    session = VoiceSession(handler, session_id=session_id, client=client)
    logger.info(
        "voice gateway: session up session=%s remote=%s",
        session.session_id, getattr(handler, "client_address", ("?",))[0],
    )
    try:
        session.serve()
    except Exception:  # pragma: no cover - defensive
        logger.exception("voice gateway: session loop failed")
    finally:
        try:
            session.close(CLOSE_NORMAL, "")
        except Exception:
            pass
        logger.info("voice gateway: session down session=%s", session.session_id)
    return True


__all__ = [
    "WS_PATH",
    "VOICE_PROTOCOL",
    "WsProtocolError",
    "Frame",
    "FrameDecoder",
    "MessageAssembler",
    "WsMessage",
    "SseDecoder",
    "VoiceEventTranslator",
    "ChatLoopbackClient",
    "ChatLoopbackError",
    "VoiceSession",
    "handle_voice_session",
    "compute_accept",
    "is_valid_ws_key",
    "encode_frame",
    "encode_close",
    "parse_close_payload",
    "apply_mask",
    "negotiate_subprotocol",
    "resolve_loopback_target",
    "SERVER_EVENTS",
    "CLIENT_EVENTS",
]
