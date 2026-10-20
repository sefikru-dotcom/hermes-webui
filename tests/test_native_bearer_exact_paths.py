"""Request-level tests for the native Bearer exact-path allow-list.

Covers NATIVE_BEARER_EXACT_PATHS in api/auth.py over a real loopback HTTP
server:
  * a valid native device credential authenticates every exact allow-listed
    path (GET and POST) and reaches dispatch;
  * garbage / non-Bearer / malformed Authorization fails closed with 401 and
    never falls back to the browser-cookie path;
  * a request with no Authorization header is NOT waved through as
    cookie-free — it gets the standard 401;
  * mutating sibling routes outside the exact set stay on the cookie path
    even when a valid native credential is presented.

Dispatch is stubbed, so no real route handler (cron runs, memory writes,
approval mutations) ever executes — except the two end-to-end tests at the
bottom (/api/tts and POST /api/transcribe), which travel through the real
check_auth + real dispatch with the network-facing dependency faked.
Credentials are synthetic, loopback-only and never printed.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path

import pytest


def _sentinel_router(dispatched):
    """Replacement for server.handle_* — records the dispatch and answers 200."""

    def route(handler, parsed):
        dispatched.append((handler.command, parsed.path))
        body = json.dumps({"dispatched": parsed.path}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True

    return route


def _request(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
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


def _mint_native_token(pairings):
    created = pairings.create_pairing()
    assert created["status"] == "pending"
    assert pairings.approve_pairing(created["code"], session_cookie="verified")
    exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
    assert exchanged and exchanged["token_type"] == "Bearer"
    return exchanged["access_token"]


def _multipart_transcribe_body(filename="voice.wav", data=b"RIFFfakewave", boundary="voiceboundary"):
    """Minimal multipart/form-data body with the ``file`` field /api/transcribe wants."""
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


@pytest.fixture()
def native_server(monkeypatch):
    import api.auth as auth
    import api.device_pairings as pairings
    import server

    dispatched = []
    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        # Cookie auth is dead for the whole test: anything that reaches the
        # sentinel dispatch got through check_auth via the Bearer fast-path.
        monkeypatch.setattr(auth, "verify_session", lambda value: False)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)
        for name in (
            "handle_get",
            "handle_post",
            "handle_put",
            "handle_patch",
            "handle_delete",
        ):
            monkeypatch.setattr(server, name, _sentinel_router(dispatched))

        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield auth, pairings, httpd.server_address[1], dispatched
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def test_valid_native_credential_passes_every_exact_path(native_server):
    auth, pairings, port, dispatched = native_server
    token = _mint_native_token(pairings)
    assert pairings.verify_native_credential(token)
    headers = {"Authorization": f"Bearer {token}"}
    for path in sorted(auth.NATIVE_BEARER_EXACT_PATHS):
        for method in ("GET", "POST"):
            dispatched.clear()
            status, payload = _request(port, method, path, headers)
            assert status == 200, (method, path, status)
            assert payload == {"dispatched": path}
            assert dispatched == [(method, path)]


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer hdev_invalidcredential",
        "Bearer ",
        "Bearer",
        "Basic Zm9vOmJhcg==",
        "hdev_invalidcredential",
    ],
)
def test_garbage_authorization_fails_closed(native_server, authorization):
    auth, _pairings, port, dispatched = native_server
    for path in sorted(auth.NATIVE_BEARER_EXACT_PATHS):
        dispatched.clear()
        status, payload = _request(
            path=path,
            port=port,
            method="POST",
            headers={"Authorization": authorization},
        )
        assert status == 401, (path, status)
        assert payload == {"error": "Authentication required"}
        assert dispatched == []


def test_missing_bearer_is_not_cookie_free_pass(native_server):
    auth, _pairings, port, dispatched = native_server
    for path in sorted(auth.NATIVE_BEARER_EXACT_PATHS):
        for method in ("GET", "POST"):
            status, payload = _request(port, method, path)
            assert status == 401, (method, path, status)
            assert payload == {"error": "Authentication required"}
    assert dispatched == []


def test_valid_credential_not_honoured_outside_exact_allowlist(native_server):
    """Mutating siblings and prefix-neighbours stay on the cookie path."""
    _auth, pairings, port, dispatched = native_server
    token = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}
    for path in (
        "/api/workspaces/add",
        "/api/workspaces/rename",
        "/api/projects/whatever",
        "/api/approval/respond/all",
        # Prefix neighbours of the newly allowlisted POST /api/transcribe:
        # the trailing slash and the browser-only capability probe stay off
        # the device-token path.
        "/api/transcribe/",
        "/api/transcribe/capability",
        # /api/settings is a read-only admission: the write verb stays off the
        # device-token path (it rewrites the server-global settings.json).
        "/api/settings",
    ):
        status, _payload = _request(port, "POST", path, headers)
        assert status == 401, (path, status)
    assert dispatched == []


def test_settings_read_admitted_write_denied(native_server):
    """GET /api/settings rides the read-only fast-path, POST /api/settings does not.

    The native client syncs the display mode with a read, so only the read is
    admitted (NATIVE_BEARER_READONLY_EXACT_PATHS, GET/HEAD only).  The POST
    twin rewrites the server-global settings.json — bot name, password hash,
    max_tokens, auth on/off — for every client, so a device token must never
    reach it: 401, no dispatch, no CSRF bypass.
    """
    auth, pairings, port, dispatched = native_server
    token = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}

    # Membership, not 404-sniffing: an unknown /api path also answers 401.
    assert "/api/settings" in auth.NATIVE_BEARER_READONLY_EXACT_PATHS
    assert "/api/settings" not in auth.NATIVE_BEARER_EXACT_PATHS

    for method in ("GET", "HEAD"):
        dispatched.clear()
        status, _payload = _request(port, method, "/api/settings", headers)
        assert status == 200, (method, status)
        assert dispatched == [(method, "/api/settings")]

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        dispatched.clear()
        status, payload = _request(port, method, "/api/settings", headers)
        assert status == 401, (method, status)
        assert payload == {"error": "Authentication required"}
        assert dispatched == []

    # No token at all: the read route is still gated.
    for method in ("GET", "POST"):
        dispatched.clear()
        status, _payload = _request(port, method, "/api/settings")
        assert status == 401, (method, status)
        assert dispatched == []


def test_post_transcribe_with_native_bearer_reaches_real_handler(monkeypatch):
    """End-to-end: POST /api/transcribe is admitted for the device Bearer.

    Dispatch is real: the multipart POST goes through check_auth (the
    allowlist barrier this wave opened) and then through the real
    handle_transcribe in api/upload.py, which has no auth gate of its own.
    tools.transcription_tools is faked in-process, so no STT provider is
    contacted and no network call happens; the 200 + transcript proves the
    request travelled the whole route.  Cookie auth is dead for the test, so
    only the device Bearer can admit it — and the same path with no
    Authorization header (or a garbage Bearer) must still 401.
    """
    import sys
    import types

    import api.auth as auth
    import api.device_pairings as pairings
    import server

    fake_stt = types.ModuleType("tools.transcription_tools")
    fake_stt.transcribe_audio = lambda path: {
        "success": True,
        "transcript": "voice turned into text",
    }
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", fake_stt)
    tools_pkg = sys.modules.get("tools")
    if tools_pkg is not None:
        monkeypatch.setattr(tools_pkg, "transcription_tools", fake_stt, raising=False)

    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(auth, "verify_session", lambda value: False)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)
        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            token = _mint_native_token(pairings)
            body, content_type = _multipart_transcribe_body()

            # A valid device Bearer is admitted and the real handler answers.
            status, payload = _request(
                port,
                "POST",
                "/api/transcribe",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                },
                body=body,
            )
            assert status == 200, (status, payload)
            assert payload == {"ok": True, "transcript": "voice turned into text"}

            # No Authorization header: the cookie path is poisoned, so the
            # route must stay closed (no cookie-free pass).
            status, payload = _request(
                port,
                "POST",
                "/api/transcribe",
                headers={"Content-Type": content_type},
                body=body,
            )
            assert status == 401, (status, payload)
            assert payload == {"error": "Authentication required"}

            # Garbage / non-Bearer credentials fail closed as well.
            for authorization in ("Bearer hdev_invalidcredential", "Bearer ", "Basic Zm9vOmJhcg=="):
                status, payload = _request(
                    port,
                    "POST",
                    "/api/transcribe",
                    headers={"Authorization": authorization, "Content-Type": content_type},
                    body=body,
                )
                assert status == 401, (authorization, status, payload)
                assert payload == {"error": "Authentication required"}
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def test_post_tts_with_native_bearer_reaches_real_handler(monkeypatch):
    """End-to-end: a device Bearer clears BOTH /api/tts barriers.

    Unlike the sentinel tests above, dispatch is real here: the request goes
    through check_auth (whitelist barrier) and then through the
    handler-internal auth gate in api/route_tts.py (second barrier).
    edge_tts is faked in-process, so no network call happens; a 200 audio
    response proves the request travelled the whole route.  Cookie auth is
    dead for the test, so only the device Bearer can admit it.
    """
    import sys
    from types import SimpleNamespace

    import api.auth as auth
    import api.device_pairings as pairings
    import api.routes as routes
    import server

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            pass

        def stream_sync(self):
            yield {"type": "audio", "data": b"RIFFfakeaudio"}

    def _drop_limiter():
        try:
            delattr(routes._handle_tts, "_tts_limiter")
        except AttributeError:
            pass

    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=FakeCommunicate))
    _drop_limiter()
    try:
        with tempfile.TemporaryDirectory() as state:
            state_path = Path(state)
            monkeypatch.setattr(pairings, "STATE_DIR", state_path)
            monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
            monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
            monkeypatch.setattr(auth, "verify_session", lambda value: False)
            monkeypatch.setattr(server, "check_auth", auth.check_auth)
            httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                token = _mint_native_token(pairings)
                body = json.dumps({"text": "hello", "voice": "en-US-AriaNeural"}).encode("utf-8")
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=15)
                try:
                    conn.request(
                        "POST",
                        "/api/tts",
                        body=body,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                    )
                    response = conn.getresponse()
                    response.read()
                    assert response.status == 200, response.status
                finally:
                    conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
    finally:
        _drop_limiter()
