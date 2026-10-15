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
approval mutations) ever executes. Credentials are synthetic, loopback-only
and never printed.
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


def _request(port, method, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
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
    ):
        status, _payload = _request(port, "POST", path, headers)
        assert status == 401, (path, status)
    assert dispatched == []


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
