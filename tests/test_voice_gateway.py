"""Unit tests for api/voice_gateway.py — pure logic, no sockets, no runtime.

Covers the three pieces the WebSocket route is built from:

* RFC 6455 handshake helpers (``compute_accept`` / header negotiation),
* RFC 6455 framing (``encode_frame`` / ``FrameDecoder`` / ``MessageAssembler``),
* the chat-SSE -> voice-contract mapping (``SseDecoder`` /
  ``VoiceEventTranslator``),

plus the wiring facts that must never drift (route path in the dispatch table
and in the device-Bearer allow-list).
"""

import base64
import json
import struct

import pytest

from api import voice_gateway as vg


# ── fakes ────────────────────────────────────────────────────────────────
class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in: headers + a writable body."""

    def __init__(self, headers=None, *, command="GET", body=b""):
        self.headers = dict(headers or {})
        self.command = command
        self.rfile = None
        self.wfile = _RecordingWfile()
        self._body = body
        self.status = None
        self.sent_headers = []
        self.ended = False
        self.close_connection = False
        self._native_auth_device = None

    def send_response(self, status, message=None):
        self.status = status

    def send_response_only(self, status, message=None):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        self.ended = True

    def log_request(self, *args, **kwargs):
        pass

    def payload(self):
        return json.loads(self.wfile.value.decode("utf-8"))


class _RecordingWfile:
    def __init__(self):
        self.value = b""

    def write(self, data):
        self.value += bytes(data)

    def flush(self):
        pass


def _masked_frame(opcode, payload=b"", *, fin=True, mask_key=b"\x01\x02\x03\x04"):
    return vg.encode_frame(opcode, payload, fin=fin, mask=True, mask_key=mask_key)


def _decode_all(decoder, data, *, assembler=None):
    messages = []
    for frame in decoder.feed(data):
        if assembler is None:
            messages.append(frame)
        else:
            messages.extend(assembler.feed(frame))
    return messages


# ── 1. handshake ─────────────────────────────────────────────────────────
def test_compute_accept_matches_rfc6455_example():
    # RFC 6455 §1.3 worked example.
    assert vg.compute_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_compute_accept_is_sha1_base64_of_key_plus_guid():
    key = base64.b64encode(b"0123456789abcdef").decode()
    expected = base64.b64encode(
        __import__("hashlib").sha1(key.encode() + vg.WS_GUID).digest()
    ).decode()
    assert vg.compute_accept(key) == expected
    assert len(base64.b64decode(vg.compute_accept(key))) == 20


@pytest.mark.parametrize(
    "key,valid",
    [
        ("dGhlIHNhbXBsZSBub25jZQ==", True),
        (base64.b64encode(b"x" * 16).decode(), True),
        ("", False),
        ("not-base64!!", False),
        (base64.b64encode(b"short").decode(), False),          # decodes, wrong length
        (base64.b64encode(b"x" * 17).decode(), False),
    ],
)
def test_is_valid_ws_key(key, valid):
    assert vg.is_valid_ws_key(key) is valid


def test_is_websocket_upgrade_requires_both_tokens_case_insensitively():
    good = _FakeHandler({"Upgrade": "WebSocket", "Connection": "keep-alive, Upgrade"})
    assert vg.is_websocket_upgrade(good) is True
    assert vg.is_websocket_upgrade(_FakeHandler({"Upgrade": "websocket"})) is False
    assert vg.is_websocket_upgrade(_FakeHandler({"Connection": "Upgrade"})) is False
    assert vg.is_websocket_upgrade(_FakeHandler({})) is False
    # substring must not match: 'websockets' != 'websocket'
    assert vg.is_websocket_upgrade(
        _FakeHandler({"Upgrade": "websockets", "Connection": "upgrade"})
    ) is False


def test_build_handshake_headers_happy_path():
    key = base64.b64encode(b"0123456789abcdef").decode()
    handler = _FakeHandler({"Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13"})
    headers = dict(vg.build_handshake_headers(handler))
    assert headers["Upgrade"] == "websocket"
    assert headers["Connection"] == "Upgrade"
    assert headers["Sec-WebSocket-Version"] == "13"
    assert headers["Sec-WebSocket-Accept"] == vg.compute_accept(key)
    assert "Sec-WebSocket-Protocol" not in headers

    with_sub = dict(vg.build_handshake_headers(handler, subprotocol=vg.VOICE_PROTOCOL))
    assert with_sub["Sec-WebSocket-Protocol"] == vg.VOICE_PROTOCOL


@pytest.mark.parametrize("version", ["", "8", "12", "14", "banana"])
def test_build_handshake_headers_rejects_bad_version(version):
    key = base64.b64encode(b"0123456789abcdef").decode()
    handler = _FakeHandler({"Sec-WebSocket-Key": key, "Sec-WebSocket-Version": version})
    with pytest.raises(vg.WsProtocolError):
        vg.build_handshake_headers(handler)


def test_build_handshake_headers_tolerates_surrounding_whitespace():
    key = base64.b64encode(b"0123456789abcdef").decode()
    handler = _FakeHandler({"Sec-WebSocket-Key": f" {key} ",
                            "Sec-WebSocket-Version": " 13 "})
    headers = dict(vg.build_handshake_headers(handler))
    assert headers["Sec-WebSocket-Accept"] == vg.compute_accept(key)


@pytest.mark.parametrize("key", ["", "abc", "!!!", base64.b64encode(b"short").decode()])
def test_build_handshake_headers_rejects_bad_key(key):
    handler = _FakeHandler({"Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13"})
    with pytest.raises(vg.WsProtocolError):
        vg.build_handshake_headers(handler)


def test_negotiate_subprotocol_echoes_only_when_offered():
    offered = _FakeHandler({"Sec-WebSocket-Protocol": f"foo, {vg.VOICE_PROTOCOL}"})
    assert vg.negotiate_subprotocol(offered) == vg.VOICE_PROTOCOL
    assert vg.negotiate_subprotocol(_FakeHandler({"Sec-WebSocket-Protocol": "foo"})) is None
    assert vg.negotiate_subprotocol(_FakeHandler({})) is None


# ── 2. framing ───────────────────────────────────────────────────────────
def test_apply_mask_is_its_own_inverse_and_validates_key():
    payload = b"hello voice gateway"
    key = b"\x01\x02\x03\x04"
    masked = vg.apply_mask(payload, key)
    assert masked != payload
    assert vg.apply_mask(masked, key) == payload
    assert vg.apply_mask(b"", key) == b""
    with pytest.raises(ValueError):
        vg.apply_mask(b"x", b"abc")


def test_encode_frame_length_encodings():
    small = vg.encode_frame(vg.OP_TEXT, b"a")
    assert small == bytes([0x81, 0x01]) + b"a"          # fin+text, 1-byte len

    medium = vg.encode_frame(vg.OP_TEXT, b"a" * 126)
    assert medium[0] == 0x81
    assert medium[1] == 126
    assert struct.unpack("!H", medium[2:4])[0] == 126
    assert medium[4:] == b"a" * 126

    large = vg.encode_frame(vg.OP_TEXT, b"b" * 70000)
    assert large[1] == 127
    assert struct.unpack("!Q", large[2:10])[0] == 70000
    assert len(large) == 10 + 70000


def test_encode_frame_sets_mask_bit_and_key():
    frame = _masked_frame(vg.OP_TEXT, b"hey")
    assert frame[0] == 0x81
    assert frame[1] & 0x80                  # mask bit
    assert frame[2:6] == b"\x01\x02\x03\x04"
    assert vg.apply_mask(frame[6:], b"\x01\x02\x03\x04") == b"hey"


def test_encode_frame_fin_flag():
    assert vg.encode_frame(vg.OP_TEXT, b"a", fin=False)[0] == 0x01
    assert vg.encode_frame(vg.OP_CONTINUATION, b"a", fin=True)[0] == 0x80


def test_encode_frame_rejects_invalid_opcode_and_control_violations():
    with pytest.raises(ValueError):
        vg.encode_frame(0x3, b"x")                       # reserved opcode
    with pytest.raises(ValueError):
        vg.encode_frame(vg.OP_PING, b"x", fin=False)     # fragmented control
    with pytest.raises(ValueError):
        vg.encode_frame(vg.OP_PING, b"x" * 126)          # control too large
    # 125 bytes is the control limit and must still work
    assert vg.encode_frame(vg.OP_PING, b"x" * 125)


def test_encode_close_and_parse_close_payload_roundtrip():
    frame = vg.encode_close(1000, "bye")
    decoded = _decode_all(vg.FrameDecoder(require_mask=False), frame)
    assert len(decoded) == 1
    assert decoded[0].opcode == vg.OP_CLOSE
    assert vg.parse_close_payload(decoded[0].payload) == (1000, "bye")
    assert vg.parse_close_payload(b"") == (0, "")


def test_parse_close_payload_rejects_bad_payloads():
    with pytest.raises(vg.WsProtocolError):
        vg.parse_close_payload(b"\x03")                  # 1-byte payload
    with pytest.raises(vg.WsProtocolError):
        vg.parse_close_payload(struct.pack("!H", 999))    # out-of-range code
    with pytest.raises(vg.WsProtocolError):
        vg.parse_close_payload(struct.pack("!H", 1006))   # reserved code
    with pytest.raises(vg.WsProtocolError):
        vg.parse_close_payload(struct.pack("!H", 1000) + b"\xff\xfe")  # bad utf-8


def test_frame_decoder_single_frame():
    decoder = vg.FrameDecoder()
    frames = decoder.feed(_masked_frame(vg.OP_TEXT, "привет".encode()))
    assert frames == [vg.Frame(vg.OP_TEXT, "привет".encode(), True)]
    assert decoder.buffered_bytes == 0


def test_frame_decoder_survives_arbitrary_tcp_splits():
    raw = _masked_frame(vg.OP_TEXT, b"split me")
    for cut in range(1, len(raw)):
        decoder = vg.FrameDecoder()
        out = decoder.feed(raw[:cut]) + decoder.feed(raw[cut:])
        assert out == [vg.Frame(vg.OP_TEXT, b"split me")], cut


def test_frame_decoder_multiple_frames_in_one_chunk():
    raw = _masked_frame(vg.OP_TEXT, b"one") + _masked_frame(vg.OP_PING, b"p")
    frames = vg.FrameDecoder().feed(raw)
    assert frames == [vg.Frame(vg.OP_TEXT, b"one"), vg.Frame(vg.OP_PING, b"p")]


def test_frame_decoder_requires_masked_client_frames():
    with pytest.raises(vg.WsProtocolError) as excinfo:
        vg.FrameDecoder().feed(vg.encode_frame(vg.OP_TEXT, b"nope"))
    assert excinfo.value.code == vg.CLOSE_PROTOCOL_ERROR
    # the unmasked direction is the server's own: allowed when opted in
    assert vg.FrameDecoder(require_mask=False).feed(
        vg.encode_frame(vg.OP_TEXT, b"fine")
    ) == [vg.Frame(vg.OP_TEXT, b"fine")]


@pytest.mark.parametrize(
    "raw,reason",
    [
        (bytes([0x91, 0x80]) + b"\x01\x02\x03\x04", "reserved bits"),
        (bytes([0x83, 0x80]) + b"\x01\x02\x03\x04", "reserved opcode"),
        (bytes([0x09, 0x80]) + b"\x01\x02\x03\x04", "fragmented control"),
        (bytes([0x89, 0xFE]) + struct.pack("!H", 126) + b"\x01\x02\x03\x04" + b"x" * 126,
         "control frame payload too large"),
    ],
)
def test_frame_decoder_rejects_protocol_violations(raw, reason):
    with pytest.raises(vg.WsProtocolError) as excinfo:
        vg.FrameDecoder().feed(raw)
    assert reason in str(excinfo.value)


def test_frame_decoder_enforces_max_frame_bytes():
    decoder = vg.FrameDecoder(max_frame_bytes=8)
    with pytest.raises(vg.WsProtocolError) as excinfo:
        decoder.feed(_masked_frame(vg.OP_TEXT, b"x" * 9))
    assert excinfo.value.code == vg.CLOSE_TOO_LARGE
    # control frames are exempt from the data-frame limit
    assert vg.FrameDecoder(max_frame_bytes=8).feed(_masked_frame(vg.OP_PING, b"x" * 9))


def test_frame_decoder_rejects_msb_length():
    raw = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", 1 << 63) + b"\x01\x02\x03\x04"
    with pytest.raises(vg.WsProtocolError):
        vg.FrameDecoder().feed(raw)


def test_message_assembler_single_and_fragmented_text():
    assembler = vg.MessageAssembler()
    messages = _decode_all(
        vg.FrameDecoder(), _masked_frame(vg.OP_TEXT, b"whole"), assembler=assembler
    )
    assert len(messages) == 1 and messages[0].kind == "text" and messages[0].text() == "whole"

    assembler = vg.MessageAssembler()
    raw = (
        _masked_frame(vg.OP_TEXT, b"frag", fin=False)
        + _masked_frame(vg.OP_CONTINUATION, b"men", fin=False)
        + _masked_frame(vg.OP_CONTINUATION, b"ted", fin=True)
    )
    messages = _decode_all(vg.FrameDecoder(), raw, assembler=assembler)
    assert [m.text() for m in messages] == ["fragmented"]


def test_message_assembler_allows_control_between_fragments():
    assembler = vg.MessageAssembler()
    raw = (
        _masked_frame(vg.OP_TEXT, b"a", fin=False)
        + _masked_frame(vg.OP_PING, b"hb")
        + _masked_frame(vg.OP_CONTINUATION, b"b", fin=True)
    )
    messages = _decode_all(vg.FrameDecoder(), raw, assembler=assembler)
    assert [m.kind for m in messages] == ["ping", "text"]
    assert messages[0].payload == b"hb"
    assert messages[1].text() == "ab"


@pytest.mark.parametrize(
    "raw,reason",
    [
        (_masked_frame(vg.OP_CONTINUATION, b"x"), "without a started message"),
        (_masked_frame(vg.OP_TEXT, b"a", fin=False) + _masked_frame(vg.OP_TEXT, b"b"),
         "while a message is fragmented"),
        (_masked_frame(vg.OP_TEXT, b"\xff\xfe"), "valid utf-8"),
    ],
)
def test_message_assembler_rejects_bad_sequences(raw, reason):
    assembler = vg.MessageAssembler()
    with pytest.raises(vg.WsProtocolError) as excinfo:
        _decode_all(vg.FrameDecoder(), raw, assembler=assembler)
    assert reason in str(excinfo.value)


def test_message_assembler_enforces_max_message_bytes_across_fragments():
    assembler = vg.MessageAssembler(max_message_bytes=4)
    with pytest.raises(vg.WsProtocolError) as excinfo:
        _decode_all(
            vg.FrameDecoder(),
            _masked_frame(vg.OP_TEXT, b"ab", fin=False)
            + _masked_frame(vg.OP_CONTINUATION, b"cde", fin=True),
            assembler=assembler,
        )
    assert excinfo.value.code == vg.CLOSE_TOO_LARGE


def test_message_assembler_passes_binary_through_unparsed():
    assembler = vg.MessageAssembler()
    messages = _decode_all(
        vg.FrameDecoder(), _masked_frame(vg.OP_BINARY, b"\xff\xfe"), assembler=assembler
    )
    assert messages[0].kind == "binary" and messages[0].payload == b"\xff\xfe"


def test_message_assembler_close_frame_carries_code_and_reason():
    assembler = vg.MessageAssembler()
    messages = _decode_all(
        vg.FrameDecoder(), _masked_frame(vg.OP_CLOSE, struct.pack("!H", 1001) + b"away"),
        assembler=assembler,
    )
    assert messages[0].kind == "close"
    assert (messages[0].code, messages[0].reason) == (1001, "away")


# ── 3. SSE parsing ───────────────────────────────────────────────────────
def test_sse_decoder_basic_event():
    decoder = vg.SseDecoder()
    assert decoder.feed(b'event: token\ndata: {"text":"hi"}\n\n') == [
        ("token", '{"text":"hi"}')
    ]


def test_sse_decoder_defaults_event_name_and_joins_multiline_data():
    decoder = vg.SseDecoder()
    assert decoder.feed("data: line1\ndata: line2\n\n") == [("message", "line1\nline2")]


def test_sse_decoder_drops_comments_and_handles_crlf():
    decoder = vg.SseDecoder()
    events = decoder.feed(":ka\r\nevent: token\r\ndata: x\r\n\r\n")
    assert events == [("token", "x")]


def test_sse_decoder_buffers_partial_lines_across_chunks():
    decoder = vg.SseDecoder()
    assert decoder.feed(b"event: tok") == []
    assert decoder.feed(b"en\ndata: 1") == []
    assert decoder.feed(b"23\n\n") == [("token", "123")]


def test_sse_decoder_two_events_in_one_chunk_and_ignores_id_retry():
    decoder = vg.SseDecoder()
    events = decoder.feed("id: 7\nretry: 100\nevent: a\ndata: 1\n\nevent: b\ndata: 2\n\n")
    assert events == [("a", "1"), ("b", "2")]


def test_sse_decoder_empty_data_block_is_not_an_event():
    decoder = vg.SseDecoder()
    assert decoder.feed("event: token\n\n") == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a":1}', {"a": 1}),
        ("", {}),
        ("not json", {"text": "not json"}),
        ("[1,2]", {"value": [1, 2]}),
        ('"str"', {"value": "str"}),
    ],
)
def test_decode_sse_data(raw, expected):
    assert vg.decode_sse_data(raw) == expected


# ── 4. chat-SSE -> voice contract translation ────────────────────────────
def _frames(translator, event, data=None):
    return translator.translate(event, data or {})


def test_translator_stamps_envelope_fields():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    frame = _frames(t, "token", {"text": "hi"})[0]
    assert frame["type"] == vg.EVENT_TEXT_DELTA
    assert frame["v"] == 1
    assert frame["turn_id"] == "t1"
    assert frame["session_id"] == "s1"
    assert frame["seq"] == 1
    assert frame["text"] == "hi"


def test_translator_envelope_omits_session_when_unknown_and_seq_increments():
    t = vg.VoiceEventTranslator(turn_id="t1")
    first = _frames(t, "token", {"text": "a"})[0]
    second = _frames(t, "token", {"text": "b"})[0]
    assert "session_id" not in first
    assert (first["seq"], second["seq"]) == (1, 2)


def test_translator_token_and_reasoning_and_interim_mapping():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    assert _frames(t, "token", {"text": "d"})[0]["type"] == vg.EVENT_TEXT_DELTA
    assert _frames(t, "reasoning", {"text": "r"})[0]["type"] == vg.EVENT_REASONING_DELTA
    assert _frames(t, "interim_assistant", {"text": "i"})[0]["type"] == vg.EVENT_TEXT_INTERIM


@pytest.mark.parametrize(
    "event,data",
    [
        ("token", {}),
        ("token", {"text": ""}),
        ("token", {"text": 5}),
        ("reasoning", {}),
        ("interim_assistant", {"text": None}),
    ],
)
def test_translator_drops_empty_text_events(event, data):
    t = vg.VoiceEventTranslator(turn_id="t1")
    assert _frames(t, event, data) == []


def test_translator_tool_events_keep_payload():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    started = _frames(t, "tool", {"name": "terminal", "id": "c1"})[0]
    assert started["type"] == vg.EVENT_TOOL
    assert started["name"] == "terminal"
    assert started["tool"] == {"name": "terminal", "id": "c1"}
    done = _frames(t, "tool_complete", {"name": "terminal", "id": "c1"})[0]
    assert done["type"] == vg.EVENT_TOOL_FINISHED


def test_translator_approval_and_clarify_point_at_the_text_chat_endpoints():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    approval = _frames(t, "approval", {"id": "a1", "tool": "terminal"})[0]
    assert approval["type"] == vg.EVENT_APPROVAL_REQUEST
    assert approval["respond_via"] == vg.APPROVAL_RESPOND_PATH
    assert approval["approval"] == {"id": "a1", "tool": "terminal"}
    clarify = _frames(t, "clarify", {"id": "q1"})[0]
    assert clarify["type"] == vg.EVENT_CLARIFY_REQUEST
    assert clarify["respond_via"] == vg.CLARIFY_RESPOND_PATH


def test_translator_status_events():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    assert _frames(t, "compressing", {})[0]["phase"] == "compressing"
    assert _frames(t, "compressed", {})[0]["type"] == vg.EVENT_STATUS
    warning = _frames(t, "warning", {"message": "fallback"})[0]
    assert (warning["type"], warning["level"], warning["message"]) == (
        vg.EVENT_STATUS, "warning", "fallback")


def test_translator_error_mapping():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    frame = _frames(t, "apperror", {"message": "boom", "code": "rate_limit",
                                    "retryable": True})[0]
    assert frame["type"] == vg.EVENT_ERROR
    assert frame["message"] == "boom"
    assert frame["code"] == "rate_limit"
    assert frame["retryable"] is True
    # generic 'error' with only an 'error' key still produces a message
    assert _frames(t, "error", {"error": "nope"})[0]["message"] == "nope"
    assert _frames(t, "apperror", {})[0]["message"] == "agent error"


@pytest.mark.parametrize(
    "event",
    ["title", "title_status", "state_saved", "todo_state", "metering",
     "session_snapshot", "pending_steer_leftover", "bg_task_complete", "unknown"],
)
def test_translator_ignores_events_without_voice_meaning(event):
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    assert _frames(t, event, {"text": "x"}) == []
    assert t.finished is False


@pytest.mark.parametrize("event", ["done", "stream_end", "cancel"])
def test_translator_finish_events_emit_finished_exactly_once(event):
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    first = _frames(t, event, {"session_id": "s1"})[0]
    assert first["type"] == vg.EVENT_FINISHED
    assert t.finished is True
    # a second finish signal (done then stream_end) must not duplicate
    assert _frames(t, "done", {}) == []
    assert _frames(t, "stream_end", {}) == []
    assert _frames(t, "cancel", {}) == []


def test_translator_finish_reason_and_session_adoption():
    t = vg.VoiceEventTranslator(turn_id="t1")
    frame = _frames(t, "stream_end", {"session_id": "s9"})[0]
    assert frame["reason"] == "stream_end"
    assert frame["session_id"] == "s9"
    assert t.session_id == "s9"
    # later frames now carry the adopted session id
    t2 = vg.VoiceEventTranslator(turn_id="t1")
    _frames(t2, "done", {"session_id": "s2", "status": "ok"})
    assert t2.session_id == "s2"


def test_translator_finished_payload_passes_through_known_keys_only():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    frame = _frames(
        t, "done",
        {"summary": "answer", "status": "ok", "answer": "a", "error": None,
         "code": None, "session_id": "s1", "usage": {"input_tokens": 1}},
    )[0]
    assert frame["summary"] == "answer"
    assert frame["status"] == "ok"
    assert frame["answer"] == "a"
    assert "usage" not in frame           # not part of the voice contract
    assert "error" not in frame and "code" not in frame


def test_translator_finish_pending_is_idempotent():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    forced = t.finish_pending("stream_closed")
    assert [f["type"] for f in forced] == [vg.EVENT_FINISHED]
    assert forced[0]["reason"] == "stream_closed"
    assert t.finish_pending() == []

    # ...and it does nothing once the relay already finished the turn
    t2 = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    _frames(t2, "done", {})
    assert t2.finish_pending() == []


def test_translator_never_emits_an_event_outside_the_published_contract():
    t = vg.VoiceEventTranslator(turn_id="t1", session_id="s1")
    seen = []
    for event, data in [
        ("token", {"text": "a"}), ("reasoning", {"text": "r"}),
        ("interim_assistant", {"text": "i"}), ("tool", {"name": "x"}),
        ("tool_complete", {"name": "x"}), ("approval", {"id": 1}),
        ("clarify", {"id": 2}), ("compressing", {}), ("warning", {"message": "w"}),
        ("apperror", {"message": "e"}), ("done", {"session_id": "s1"}),
        ("stream_end", {}), ("title", {}),
    ]:
        for frame in _frames(t, event, data):
            seen.append(frame["type"])
    assert seen
    assert set(seen) <= set(vg.SERVER_EVENTS)


# ── 5. wiring pins (route + auth) ────────────────────────────────────────
def test_route_path_is_pinned_in_dispatch_and_auth_allowlist():
    import api.auth as auth
    from api import routes

    assert vg.WS_PATH == "/api/voice/session"
    assert routes._VOICE_WS_PATHS == frozenset({vg.WS_PATH})
    assert vg.WS_PATH in auth.NATIVE_BEARER_EXACT_PATHS
    assert vg.WS_PATH not in auth.PUBLIC_PATHS


def test_handle_get_dispatches_the_voice_route():
    import inspect

    from api import routes

    source = inspect.getsource(routes.handle_get)
    assert "_VOICE_WS_PATHS" in source
    assert "handle_voice_session" in source
    # terminal route: dispatched before the generic static/session handlers
    assert source.index("_VOICE_WS_PATHS") < source.index("_serve_static")


def test_server_route_needs_no_new_handler_method():
    """The upgrade rides on GET, so no do_* method is added to Handler."""
    import inspect

    import server

    source = inspect.getsource(server.Handler)
    assert "voice" not in source.lower()


def test_handle_voice_session_rejects_non_upgrade_requests():
    handler = _FakeHandler({}, command="GET")
    assert vg.handle_voice_session(handler, _parsed("/api/voice/session")) is True
    assert handler.status == 400
    payload = handler.payload()
    assert payload["error"] == "WebSocket upgrade required"
    assert payload["protocol"] == vg.VOICE_PROTOCOL
    assert ("Connection", "close") in handler.sent_headers


def test_handle_voice_session_rejects_bad_version_with_400_not_101():
    handler = _FakeHandler(
        {"Upgrade": "websocket", "Connection": "Upgrade",
         "Sec-WebSocket-Version": "8", "Sec-WebSocket-Key": "abc"},
        command="GET",
    )
    handler._native_auth_device = {"id": "dev"}      # skip the origin guard
    assert vg.handle_voice_session(handler, _parsed("/api/voice/session")) is True
    assert handler.status == 400
    assert "version" in handler.payload()["error"]


def _parsed(path):
    import urllib.parse

    return urllib.parse.urlparse(path)


def test_write_error_frame_encodes_a_single_ws_text_frame():
    handler = _FakeHandler({}, command="GET")
    assert vg._write_error_frame(handler, {"type": "agent.error", "code": "x"}) is True
    decoded = _decode_all(vg.FrameDecoder(require_mask=False), handler.wfile.value)
    assert len(decoded) == 1
    assert decoded[0].opcode == vg.OP_TEXT
    assert json.loads(decoded[0].payload.decode()) == {"type": "agent.error", "code": "x"}
