"""Speech synthesis endpoint with route-owned network and limiter seams."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

# ── Local Piper engine ──────────────────────────────────────────────────
#
# Piper synthesises on CPU from a local ONNX voice, so /api/tts keeps working
# when the Microsoft Edge endpoint rejects our token (the failure mode that
# used to kill voice output silently). The loaded voice is cached per process:
# loading takes ~1.3 s, a short sentence then synthesises in ~0.25 s. Every
# synthesis is serialised behind a lock — an onnxruntime InferenceSession is
# not documented as safe for concurrent Run() calls, and the 2 s per-client
# rate limiter keeps the queue short.
_PIPER_VOICE_CACHE: dict = {}
_PIPER_VOICE_CACHE_LOCK = threading.Lock()
_PIPER_SYNTH_LOCK = threading.Lock()

# Interpreter that owns the `piper` package, used only when the running
# WebUI interpreter cannot import piper (bootstrap re-execs into the Hermes
# venv, but a bare .venv launch would not have it). Probed once, lazily.
_PIPER_PYTHON: list = [None]
_PIPER_PYTHON_PROBED = threading.Event()

# Voice ids are file stems inside voices_dir. The id from the request is only
# ever matched against this pattern and then looked up in the directory
# listing — a request path can never reach the filesystem.
_PIPER_VOICE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_PIPER_DEFAULT_VOICES_DIR = Path.home() / ".hermes" / "cache" / "piper-voices"


def _tts_piper_config() -> dict:
    """Resolve tts.piper.{voice,voices_dir} from config.yaml, then env, then home."""
    voice = os.getenv("HERMES_TTS_PIPER_VOICE", "").strip()
    voices_dir = os.getenv("HERMES_TTS_PIPER_VOICES_DIR", "").strip()
    try:
        from api.config import get_config
        tts_cfg = (get_config() or {}).get("tts", {})
        if isinstance(tts_cfg, dict):
            piper_cfg = tts_cfg.get("piper", {})
            if isinstance(piper_cfg, dict):
                voice = str(piper_cfg.get("voice") or voice or "").strip()
                voices_dir = str(piper_cfg.get("voices_dir") or voices_dir or "").strip()
    except Exception:
        pass  # config is optional: env/home fallbacks below still work
    if not voices_dir:
        try:
            from api.profiles import get_active_hermes_home
            voices_dir = str(get_active_hermes_home() / "cache" / "piper-voices")
        except Exception:
            voices_dir = str(_PIPER_DEFAULT_VOICES_DIR)
    return {"voice": voice, "voices_dir": voices_dir}


def _piper_voice_catalog(voices_dir: str) -> dict:
    """Map voice id -> model path for every *.onnx voice in voices_dir."""
    catalog: dict[str, Path] = {}
    try:
        entries = sorted(Path(voices_dir).glob("*.onnx"))
    except OSError:
        return catalog
    for entry in entries:
        try:
            if entry.is_file() and _PIPER_VOICE_ID_RE.fullmatch(entry.stem):
                catalog[entry.stem] = entry
        except OSError:
            continue
    return catalog


def _piper_import():
    """Return (PiperVoice, SynthesisConfig) when piper is importable, else None."""
    try:
        from piper import PiperVoice, SynthesisConfig
    except Exception:
        return None
    return PiperVoice, SynthesisConfig


def _piper_load_voice(model_path):
    """Load (and cache) a PiperVoice for model_path; None when piper is absent."""
    key = str(model_path)
    with _PIPER_VOICE_CACHE_LOCK:
        cached = _PIPER_VOICE_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = _piper_import()
    if loaded is None:
        return None
    PiperVoice, _SynthesisConfig = loaded
    voice = PiperVoice.load(key)
    with _PIPER_VOICE_CACHE_LOCK:
        _PIPER_VOICE_CACHE[key] = voice
    return voice


def _piper_python_candidates():
    """Interpreters that might have the piper package, best first."""
    env_python = os.getenv("HERMES_TTS_PIPER_PYTHON", "").strip()
    if env_python:
        yield env_python
    yield "/usr/local/lib/hermes-agent/venv/bin/python"
    yield sys.executable


def _piper_subprocess_python():
    """First interpreter that can `import piper`, probed once and cached."""
    if _PIPER_PYTHON_PROBED.is_set():
        return _PIPER_PYTHON[0]
    resolved = None
    for candidate in _piper_python_candidates():
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import piper"],
                capture_output=True, timeout=30, check=False,
            )
        except Exception:
            continue
        if probe.returncode == 0:
            resolved = candidate
            break
    _PIPER_PYTHON[0] = resolved
    _PIPER_PYTHON_PROBED.set()
    return resolved


def _piper_synthesize_wav_subprocess(text: str, model_path, *, length_scale=None, timeout=60):
    """Synthesise through `python -m piper` when piper is not importable here."""
    python_bin = _piper_subprocess_python()
    if not python_bin:
        return None
    with tempfile.TemporaryDirectory(prefix="hermes-piper-") as tmpdir:
        out_path = Path(tmpdir) / "speech.wav"
        cmd = [python_bin, "-m", "piper", "-m", str(model_path), "-f", str(out_path)]
        if length_scale:
            cmd += ["--length-scale", f"{float(length_scale):.3f}"]
        try:
            proc = subprocess.run(
                cmd, input=text, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except Exception:
            return None
        if proc.returncode != 0 or not out_path.is_file():
            return None
        data = out_path.read_bytes()
    return data or None


def _piper_synthesize_wav(text: str, model_path, *, length_scale=None):
    """Synthesise text to WAV bytes with a local voice; None if piper is unavailable."""
    loaded = _piper_import()
    if loaded is None:
        return _piper_synthesize_wav_subprocess(text, model_path, length_scale=length_scale)
    _PiperVoice, SynthesisConfig = loaded
    voice = _piper_load_voice(model_path)
    if voice is None:
        return _piper_synthesize_wav_subprocess(text, model_path, length_scale=length_scale)
    syn_config = None
    if length_scale:
        try:
            syn_config = SynthesisConfig(length_scale=float(length_scale))
        except Exception:
            syn_config = None
    buf = io.BytesIO()
    with _PIPER_SYNTH_LOCK:
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file, syn_config=syn_config)
    return buf.getvalue() or None


def _tts_rate_to_length_scale(rate_str: str):
    """Map an edge-style rate ("+20%") onto piper's length_scale (inverse speed).

    piper has no rate/pitch parameters: rate is inverted into length_scale and
    clamped to 0.5..2.0 so a hostile value cannot produce minute-long audio.
    pitch is ignored (no equivalent in the piper model).
    """
    if not rate_str:
        return None
    try:
        pct = float(str(rate_str).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if pct == 0:
        return None
    return min(2.0, max(0.5, 1.0 / (1.0 + pct / 100.0)))


def handle_tts(handler, parsed, *, limiter_owner, deps):
    """Generate TTS audio via supported server TTS engines. POST JSON body only.

    Engine routing: an explicit `engine` wins ("piper", "edge", "elevenlabs",
    "openai"). Without one, an edge voice id from the allowlist keeps the
    Microsoft path and anything else (no voice, or a local piper voice id from
    tts.piper.voices_dir) goes to the local piper engine. Piper failures fall
    back to edge-tts so a missing local model never means silence.

    Design note addressing deep review blocker #4 (synchronous I/O):
    The server uses ThreadingHTTPServer (see server.py:173), so each request
    already runs in its own dedicated thread. A TTS request therefore occupies
    only its own thread during Microsoft network I/O + streaming; other clients
    are unaffected. Combined with early auth, a strict per-client 2 s rate
    limit, 5000-char cap, and voice allowlist, the blocking cost is bounded and
    intentional. All audio chunks are buffered before sending so that a
    Content-Length header can be included. The 5000-char cap bounds audio to
    roughly 1-5 MB, making full buffering safe. Without Content-Length the
    HTTP/1.0 server leaves the response open until a ~31 s timeout fires, and
    the browser cannot play the blob mid-stream.
    If the HTTP layer ever moves to asyncio we can adopt edge_tts's native
    async API at that time.
    """
    read_body = deps["read_body"]
    _normalize_tts_prosody = deps["_normalize_tts_prosody"]
    logger = deps["logger"]
    Request = deps["Request"]
    build_opener = deps["build_opener"]
    ProxyHandler = deps["ProxyHandler"]
    _NoRedirectTtsHandler = deps["_NoRedirectTtsHandler"]
    _PinnedHTTPSHandler = deps["_PinnedHTTPSHandler"]
    _tts_open = deps["_tts_open"]
    _buffer_tts_audio_response = deps["_buffer_tts_audio_response"]
    _normalized_openai_tts_base_url = deps["_normalized_openai_tts_base_url"]

    text = ""
    voice = "ru-RU-SvetlanaNeural"
    requested_voice = ""
    rate_str = ""
    pitch_str = ""
    engine = ""  # "" (auto) | "edge" | "piper" | "elevenlabs" | "openai"

    if handler.command != "POST":
        from api.helpers import bad as _bad
        return _bad(handler, "POST required for /api/tts", 405)

    try:
        data = read_body(handler)
        text = (data.get("text") or "").strip()
        requested_voice = str(data.get("voice") or "").strip()
        voice = requested_voice or voice
        rate_str = _normalize_tts_prosody(data.get("rate"), unit="%")
        pitch_str = _normalize_tts_prosody(data.get("pitch"), unit="Hz")
        engine = str(data.get("engine") or "").strip().lower()
    except Exception:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid request body", 400)

    if rate_str is None:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid rate", 400)
    if pitch_str is None:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid pitch", 400)

    if not text:
        from api.helpers import bad as _bad
        return _bad(handler, "text is required", 400)
    if len(text) > 5000:
        from api.helpers import bad as _bad
        return _bad(handler, "text too long (max 5000 characters)", 400)

    from api.auth import is_auth_enabled, parse_cookie, verify_session
    cv = None
    if is_auth_enabled():
        # check_auth (server.py) admitted this request before dispatch: either
        # a browser cookie session or a verified native device Bearer —
        # /api/tts is in NATIVE_BEARER_EXACT_PATHS and a successful Bearer
        # verification sets handler._native_auth_device.  Honour that flag;
        # the former cookie-only re-check 401'd every device-Bearer client
        # (the Android app had to fall back to on-device synthesis).
        if getattr(handler, "_native_auth_device", None) is None:
            cv = parse_cookie(handler)
            if not (cv and verify_session(cv)):
                from api.helpers import bad as _bad
                return _bad(handler, "unauthorized", 401)

    # High-quality per-client rate limiting for TTS.
    if not hasattr(limiter_owner, "_tts_limiter"):
        import time as _time, threading as _threading
        class _TtsRateLimiter:
            def __init__(self, window_seconds=2.0, prune_interval=50):
                self.window = window_seconds
                self.prune_interval = prune_interval
                self._hits = {}
                self._lock = _threading.Lock()
                self._checks = 0

            def _get_client_key(self, h):
                trust_proxy = os.getenv("HERMES_WEBUI_TRUST_FORWARDED_FOR", "").strip().lower()
                if trust_proxy in ("1", "true", "yes", "on"):
                    for hdr in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
                        val = h.headers.get(hdr)
                        if val:
                            ip = val.split(",")[0].strip().split(";")[0].strip()
                            if ip:
                                return ip
                return getattr(h, "client_address", ("unknown",))[0]

            def check(self, handler, session_cookie=None):
                key = self._get_client_key(handler)
                if session_cookie and "." in str(session_cookie):
                    key = str(session_cookie).split(".", 1)[0]
                now = _time.time()
                with self._lock:
                    self._checks += 1
                    if self._checks % self.prune_interval == 0:
                        cutoff = now - (self.window * 10)
                        self._hits = {k: v for k, v in self._hits.items() if v > cutoff}
                    last = self._hits.get(key, 0)
                    if now - last < self.window:
                        return False
                    self._hits[key] = now
                    return True

        limiter_owner._tts_limiter = _TtsRateLimiter(window_seconds=2.0)

    limiter = limiter_owner._tts_limiter
    if not limiter.check(handler, cv):
        logger.warning("TTS rate limit hit for client=%s", limiter._get_client_key(handler))
        from api.helpers import bad as _bad
        return _bad(handler, "rate limit exceeded — please wait", 429)

    # ── ElevenLabs TTS ──────────────────────────────────────────────────
    if engine == "elevenlabs":
        api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            # Fall back to reading from Hermes .env file
            try:
                from api.onboarding import _load_env_file
                from api.profiles import get_active_hermes_home
                api_key = _load_env_file(get_active_hermes_home() / ".env").get("ELEVENLABS_API_KEY", "")
            except Exception:
                pass
        if not api_key:
            from api.helpers import bad as _bad
            return _bad(handler, "ELEVENLABS_API_KEY not configured", 503)

        # Resolve voice_id from Hermes config.yaml → env fallback
        voice_id = "pNInz6obpgDQGcFmaJgB"  # Adam (same default as hermes-agent config.yaml)
        model_id = "eleven_multilingual_v2"
        try:
            from api.config import get_config
            tts_cfg = (get_config() or {}).get("tts", {})
            if isinstance(tts_cfg, dict):
                el_cfg = tts_cfg.get("elevenlabs", {})
                if isinstance(el_cfg, dict):
                    voice_id = el_cfg.get("voice_id", voice_id)
                    model_id = el_cfg.get("model", model_id) or el_cfg.get("model_id", model_id)
                    # ^ treat empty string as "not set" — fall through to default
        except Exception:
            pass  # fall back to defaults

        # Validate voice_id is a safe path segment (no traversal)
        # fullmatch (not match) so a trailing newline can't slip past the `$`
        # anchor — defense-in-depth on the config-derived voice_id before it
        # goes into the request URL (#3510 review).
        if not re.fullmatch(r'[A-Za-z0-9_-]+', voice_id):
            from api.helpers import bad as _bad
            return _bad(handler, "invalid voice_id in config", 400)

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=mp3_44100_128"
        req_body = json.dumps({
            "text": text,
            "model_id": model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }).encode("utf-8")

        req = Request(url, data=req_body, headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        })

        # Buffer the full response before sending first byte.
        # The streaming endpoint is designed for chunked delivery, but urllib's
        # chunked-read path adds per-chunk overhead that dominates short TTS
        # payloads. A hard cap keeps the buffered path bounded even if the
        # upstream misbehaves.
        try:
            with _tts_open(req, timeout=30, opener_factory=lambda: build_opener(ProxyHandler({}), _NoRedirectTtsHandler())) as resp:
                audio_data = _buffer_tts_audio_response(resp)
        except ValueError:
            logger.warning("ElevenLabs TTS rejected an invalid upstream response", exc_info=True)
            from api.helpers import bad as _bad
            return _bad(handler, "ElevenLabs TTS generation failed", 502)
        except Exception:
            logger.exception("ElevenLabs TTS generation failed")
            from api.helpers import bad as _bad
            return _bad(handler, "ElevenLabs TTS generation failed", 500)

        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(audio_data)))
        handler.end_headers()
        try:
            handler.wfile.write(audio_data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    # ── OpenAI-compatible TTS ──────────────────────────────────────────
    if engine == "openai":
        api_key = os.getenv("VOICE_TOOLS_OPENAI_KEY", "").strip()
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            try:
                from api.onboarding import _load_env_file
                from api.profiles import get_active_hermes_home
                env_cfg = _load_env_file(get_active_hermes_home() / ".env")
                api_key = env_cfg.get("VOICE_TOOLS_OPENAI_KEY", "") or env_cfg.get("OPENAI_API_KEY", "")
            except Exception:
                pass
        if not api_key:
            from api.helpers import bad as _bad
            return _bad(handler, "OpenAI API key not configured", 503)

        from urllib.parse import urlunsplit as _urlunsplit

        base_url = _urlunsplit(("https", "api.openai.com", "/v1", "", ""))
        model = "gpt-4o-mini-tts"
        oai_voice = "alloy"
        try:
            from api.config import get_config
            tts_cfg = (get_config() or {}).get("tts", {})
            if isinstance(tts_cfg, dict):
                oai_cfg = tts_cfg.get("openai", {})
                if isinstance(oai_cfg, dict):
                    base_url = _normalized_openai_tts_base_url(oai_cfg.get("base_url") or base_url)
                    model = oai_cfg.get("model") or model
                    oai_voice = oai_cfg.get("voice") or oai_voice
                else:
                    base_url = _normalized_openai_tts_base_url(base_url)
            else:
                base_url = _normalized_openai_tts_base_url(base_url)
        except ValueError:
            from api.helpers import bad as _bad
            return _bad(handler, "invalid OpenAI base_url in config", 400)
        except Exception:
            pass

        url = f"{base_url}/audio/speech"
        req_body = json.dumps({
            "model": model,
            "input": text,
            "voice": oai_voice,
        }).encode("utf-8")

        req = Request(url, data=req_body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        })

        # Use a pinned HTTPS opener so the resolved address is the one that gets
        # dialed. Keep the no-redirect handler in the same chain to block
        # bearer leaks and SSRF bounce redirects after hostname validation.
        try:
            with _tts_open(req, timeout=30, opener_factory=lambda: build_opener(ProxyHandler({}), _NoRedirectTtsHandler(), _PinnedHTTPSHandler())) as resp:
                audio_data = _buffer_tts_audio_response(resp)
        except ValueError:
            logger.warning("OpenAI TTS rejected an invalid upstream response", exc_info=True)
            from api.helpers import bad as _bad
            return _bad(handler, "OpenAI TTS generation failed", 502)
        except Exception:
            logger.exception("OpenAI TTS generation failed")
            from api.helpers import bad as _bad
            return _bad(handler, "OpenAI TTS generation failed", 500)

        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(audio_data)))
        handler.end_headers()
        try:
            handler.wfile.write(audio_data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    # ── Engine selection ────────────────────────────────────────────────
    # An explicit edge voice keeps the old Microsoft path (browser WebUI
    # compatibility); a missing voice or a local piper voice id goes to piper.
    # Unknown ids are still rejected instead of silently synthesising with a
    # different voice.
    allowed = {
        "ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural",
        "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural",
        "zh-CN-YunjianNeural", "zh-CN-YunyangNeural",
        "en-US-AriaNeural", "en-US-GuyNeural",
        "fr-CA-AntoineNeural", "fr-CA-JeanNeural",
        "fr-CA-SylvieNeural", "fr-CA-ThierryNeural",
        "fr-FR-DeniseNeural", "fr-FR-EloiseNeural", "fr-FR-HenriNeural",
        "id-ID-GadisNeural",
    }

    piper_cfg = _tts_piper_config()
    piper_catalog = _piper_voice_catalog(piper_cfg["voices_dir"])
    piper_voice = ""
    use_piper = False

    if engine == "piper":
        use_piper = True
        piper_voice = requested_voice if requested_voice in piper_catalog else piper_cfg["voice"]
    elif engine in ("", "auto") and requested_voice not in allowed:
        if requested_voice and requested_voice not in piper_catalog:
            from api.helpers import bad as _bad
            return _bad(handler, "invalid voice", 400)
        use_piper = True
        piper_voice = requested_voice or piper_cfg["voice"]

    if use_piper:
        model_path = piper_catalog.get(piper_voice) if piper_voice else None
        audio_data = None
        if model_path is None:
            logger.warning(
                "Piper voice %r not found in %s; falling back to edge-tts",
                piper_voice, piper_cfg["voices_dir"],
            )
        else:
            try:
                audio_data = _piper_synthesize_wav(
                    text, model_path, length_scale=_tts_rate_to_length_scale(rate_str),
                )
            except Exception:
                logger.exception("Piper synthesis failed; falling back to edge-tts")
                audio_data = None
        if audio_data:
            handler.send_response(200)
            handler.send_header("Content-Type", "audio/wav")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", str(len(audio_data)))
            handler.end_headers()
            try:
                handler.wfile.write(audio_data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return True
        # Local synthesis unavailable: keep the request alive on edge-tts, with
        # the historical default voice when the request carried a piper id.
        voice = requested_voice if requested_voice in allowed else "ru-RU-SvetlanaNeural"
        logger.warning("Local TTS unavailable; serving edge-tts voice %s", voice)

    # ── Edge TTS ────────────────────────────────────────────────────────
    if voice not in allowed:
        from api.helpers import bad as _bad
        return _bad(handler, "invalid voice", 400)

    try:
        try:
            import edge_tts
        except ImportError:
            from api.helpers import bad as _bad
            return _bad(handler, "Edge TTS engine not installed on the server. Install it with: pip install edge-tts", 503)

        kwargs = {}
        if rate_str:
            kwargs["rate"] = rate_str
        if pitch_str:
            kwargs["pitch"] = pitch_str

        comm = edge_tts.Communicate(text, voice, **kwargs)

        # Buffer all audio chunks before responding so Content-Length is known.
        # Without it the HTTP/1.0 server holds the connection open until a ~31 s
        # timeout fires and the browser cannot play the resulting blob.
        audio_buf = bytearray()
        for chunk in comm.stream_sync():
            if chunk.get("type") == "audio" and chunk.get("data"):
                audio_buf.extend(chunk["data"])

        if not audio_buf:
            from api.helpers import bad as _bad
            return _bad(handler, "TTS produced no audio", 500)

        handler.send_response(200)
        handler.send_header("Content-Type", "audio/mpeg")
        handler.send_header("Content-Length", str(len(audio_buf)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        try:
            handler.wfile.write(audio_buf)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    except BrokenPipeError:
        return True
    except Exception:
        logger.exception("Edge TTS generation failed")
        from api.helpers import bad as _bad
        return _bad(handler, "TTS generation failed", 500)
