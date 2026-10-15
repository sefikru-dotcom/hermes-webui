"""Speech synthesis endpoint with route-owned network and limiter seams."""

import json
import os
import re


def handle_tts(handler, parsed, *, limiter_owner, deps):
    """Generate TTS audio via supported server TTS engines. POST JSON body only.

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
    rate_str = ""
    pitch_str = ""
    engine = "edge"  # "edge" | "elevenlabs" | "openai" | "browser" (browser is client-side only)

    if handler.command != "POST":
        from api.helpers import bad as _bad
        return _bad(handler, "POST required for /api/tts", 405)

    try:
        data = read_body(handler)
        text = (data.get("text") or "").strip()
        voice = data.get("voice") or voice
        rate_str = _normalize_tts_prosody(data.get("rate"), unit="%")
        pitch_str = _normalize_tts_prosody(data.get("pitch"), unit="Hz")
        engine = (data.get("engine") or "edge").strip().lower()
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

    # ── Edge TTS ────────────────────────────────────────────────────────
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
