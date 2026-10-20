"""Local piper engine on POST /api/tts, with edge-tts fallback.

The Android client posts {"text": ...} with a device Bearer and plays whatever
Content-Type comes back. When the Microsoft token churns, edge synthesis dies
silently — so a request without an explicit edge voice must be served by the
local piper voice, in WAV, with a Content-Length, and only fall back to edge
when local synthesis is genuinely unavailable.

No test here touches the real piper model, the real edge_tts package, or the
network: the piper synthesis seam is monkeypatched and edge_tts is a fake
module in sys.modules.
"""
import io
import json
import sys
import types

import pytest

import api.route_tts as route_tts
import api.routes as routes

WAV_BYTES = b"RIFF" + b"\x00" * 40 + b"WAVEfmt "

# Captured before the autouse fixture swaps the seam, for the cache test.
_REAL_PIPER_SYNTHESIZE = getattr(route_tts, "_piper_synthesize_wav", None)


class _FakeHandler:
    _native_auth_device: dict | None = None

    def __init__(self, body: bytes, command: str = "POST", headers=None, client="7.7.7.7"):
        self.command = command
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = headers or {}
        self.headers.setdefault("Content-Length", str(len(body)))
        self.client_address = (client, 12345)
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def body(self):
        return self.wfile.getvalue()

    def payload(self):
        try:
            return json.loads(self.body().decode("utf-8"))
        except Exception:
            return None


def _post(body_dict, client="7.7.7.7", **kw):
    return _FakeHandler(json.dumps(body_dict).encode(), client=client, **kw)


def _reset_limiter():
    if hasattr(routes._handle_tts, "_tts_limiter"):
        del routes._handle_tts._tts_limiter


def _make_edge_tts_mock(audio_chunks):
    fake_module = types.ModuleType("edge_tts")

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            self.voice = voice
            self.kwargs = kwargs
            fake_module.last_voice = voice

        def stream_sync(self):
            for data in audio_chunks:
                yield {"type": "audio", "data": data}

    fake_module.Communicate = FakeCommunicate
    return fake_module


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Auth off, fresh limiter, temp voice dir, no real piper model."""
    import api.auth as _auth
    monkeypatch.setattr(_auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(routes, "is_auth_enabled", lambda: False, raising=False)
    monkeypatch.delenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", raising=False)
    _reset_limiter()

    voices_dir = tmp_path / "piper-voices"
    voices_dir.mkdir()
    (voices_dir / "ru_RU-dmitri-medium.onnx").write_bytes(b"fake-model")
    (voices_dir / "ru_RU-dmitri-medium.onnx.json").write_text("{}")

    monkeypatch.setattr(
        route_tts, "_tts_piper_config",
        lambda: {"voice": "ru_RU-dmitri-medium", "voices_dir": str(voices_dir)},
    )
    calls = []

    def _fake_synth(text, model_path, *, length_scale=None):
        calls.append({"text": text, "model": str(model_path), "length_scale": length_scale})
        return WAV_BYTES

    monkeypatch.setattr(route_tts, "_piper_synthesize_wav", _fake_synth)
    monkeypatch.setattr(route_tts, "_PIPER_VOICE_CACHE", {})
    yield {"calls": calls, "voices_dir": voices_dir}
    _reset_limiter()


def test_no_voice_routes_to_local_piper(monkeypatch, _isolated):
    """A request without a voice id (Android client) is served by piper as WAV."""
    monkeypatch.setitem(sys.modules, "edge_tts", _make_edge_tts_mock([b"\xff\xfb" * 10]))

    h = _post({"text": "Привет"}, client="7.7.7.1")
    assert routes._handle_tts(h, None) is True

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/wav"
    assert h.sent_headers.get("Content-Length") == str(len(WAV_BYTES))
    assert h.body() == WAV_BYTES
    assert _isolated["calls"], "piper synthesis was not attempted"
    assert _isolated["calls"][0]["model"].endswith("ru_RU-dmitri-medium.onnx")


def test_piper_voice_id_routes_to_piper(monkeypatch, _isolated):
    """An explicit piper voice id from the catalog selects the local engine."""
    monkeypatch.setitem(sys.modules, "edge_tts", _make_edge_tts_mock([b"\xff\xfb" * 10]))

    h = _post({"text": "Привет", "voice": "ru_RU-dmitri-medium"}, client="7.7.7.2")
    assert routes._handle_tts(h, None) is True

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/wav"
    assert len(_isolated["calls"]) == 1


def test_edge_voice_still_uses_edge(monkeypatch, _isolated):
    """Browser WebUI compatibility: an allowlisted edge voice keeps audio/mpeg."""
    edge_module = _make_edge_tts_mock([b"\xff\xfb\x90" * 4])
    monkeypatch.setitem(sys.modules, "edge_tts", edge_module)

    h = _post({"text": "hello", "voice": "en-US-AriaNeural"}, client="7.7.7.3")
    assert routes._handle_tts(h, None) is True

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/mpeg"
    assert edge_module.last_voice == "en-US-AriaNeural"
    assert _isolated["calls"] == []


def test_piper_failure_falls_back_to_edge(monkeypatch, _isolated):
    """A broken local engine must not silence the client: edge takes over."""
    edge_module = _make_edge_tts_mock([b"\xff\xfb\x90" * 6])
    monkeypatch.setitem(sys.modules, "edge_tts", edge_module)

    def _boom(text, model_path, *, length_scale=None):
        raise RuntimeError("onnxruntime exploded")

    monkeypatch.setattr(route_tts, "_piper_synthesize_wav", _boom)

    h = _post({"text": "Привет", "voice": "ru_RU-dmitri-medium"}, client="7.7.7.4")
    assert routes._handle_tts(h, None) is True

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/mpeg"
    # piper id is not an edge voice: the historical default is used for the fallback
    assert edge_module.last_voice == "ru-RU-SvetlanaNeural"


def test_piper_unavailable_falls_back_to_edge_with_requested_edge_voice(monkeypatch, _isolated):
    """When piper is unavailable and an edge voice was requested, keep that voice."""
    edge_module = _make_edge_tts_mock([b"\xff\xfb\x90" * 6])
    monkeypatch.setitem(sys.modules, "edge_tts", edge_module)
    monkeypatch.setattr(
        route_tts, "_piper_synthesize_wav",
        lambda text, model_path, *, length_scale=None: None,
    )

    h = _post({"text": "hello", "engine": "piper", "voice": "en-US-AriaNeural"}, client="7.7.7.5")
    assert routes._handle_tts(h, None) is True

    assert h.status == 200
    assert h.sent_headers.get("Content-Type") == "audio/mpeg"
    assert edge_module.last_voice == "en-US-AriaNeural"


def test_both_engines_unavailable_returns_clear_503(monkeypatch, _isolated):
    """piper down and edge_tts missing: explicit error, not silence."""
    monkeypatch.delitem(sys.modules, "edge_tts", raising=False)
    monkeypatch.setattr(
        route_tts, "_piper_synthesize_wav",
        lambda text, model_path, *, length_scale=None: None,
    )

    h = _post({"text": "Привет"}, client="7.7.7.6")
    routes._handle_tts(h, None)

    assert h.status == 503
    assert "edge" in (h.payload() or {}).get("error", "").lower()


def test_voice_path_injection_is_rejected(_isolated):
    """A path-shaped voice id never reaches the filesystem."""
    h = _post({"text": "Привет", "voice": "../../etc/passwd"}, client="7.7.7.7")
    routes._handle_tts(h, None)

    assert h.status == 400
    assert "invalid voice" in (h.payload() or {}).get("error", "")
    assert _isolated["calls"] == []


def test_unknown_voice_still_rejected(_isolated):
    """Allowlist behaviour is unchanged for ids that are neither edge nor piper."""
    h = _post({"text": "hello", "voice": "evil-voice-injection"}, client="7.7.7.8")
    routes._handle_tts(h, None)

    assert h.status == 400
    assert "invalid voice" in (h.payload() or {}).get("error", "")


def test_rate_maps_to_piper_length_scale(_isolated):
    """edge-style rate is inverted into piper length_scale; pitch is ignored."""
    h = _post({"text": "Привет", "rate": "+50%", "pitch": "+10Hz"}, client="7.7.7.9")
    assert routes._handle_tts(h, None) is True

    assert h.status == 200
    assert _isolated["calls"][0]["length_scale"] == pytest.approx(1 / 1.5)


def test_zero_rate_sends_no_length_scale(_isolated):
    h = _post({"text": "Привет", "rate": "+0%"}, client="7.7.7.10")
    assert routes._handle_tts(h, None) is True

    assert _isolated["calls"][0]["length_scale"] is None


def test_piper_text_cap_still_applies(_isolated):
    h = _post({"text": "x" * 5001}, client="7.7.7.11")
    routes._handle_tts(h, None)

    assert h.status == 400
    assert _isolated["calls"] == []


def test_loaded_voice_is_cached_across_requests(monkeypatch, _isolated):
    """The model is loaded once per process, not per request (latency)."""
    loads = []

    class _FakeVoice:
        def synthesize_wav(self, text, wav_file, syn_config=None):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 220)

    class _FakePiperVoice:
        @staticmethod
        def load(path):
            loads.append(str(path))
            return _FakeVoice()

    class _FakeSynthesisConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        route_tts, "_piper_import",
        lambda: (_FakePiperVoice, _FakeSynthesisConfig),
    )
    monkeypatch.setattr(route_tts, "_PIPER_VOICE_CACHE", {})
    # Use the real synthesis path for this test.
    monkeypatch.setattr(
        route_tts, "_piper_synthesize_wav",
        lambda text, model_path, *, length_scale=None: _REAL_PIPER_SYNTHESIZE(
            text, model_path, length_scale=length_scale
        ),
    )
    for i in range(2):
        h = _post({"text": "Привет"}, client=f"7.7.8.{i}")
        assert routes._handle_tts(h, None) is True
        assert h.sent_headers.get("Content-Type") == "audio/wav"
        assert h.body().startswith(b"RIFF")

    assert len(loads) == 1, loads
