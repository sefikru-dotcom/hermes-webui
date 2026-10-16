"""Exact native Bearer allow-list for client API routes (api/auth.py)."""
import http.client
import tempfile
import threading
from pathlib import Path


EXPECTED_EXACT_PATHS = frozenset({
    '/api/approval/pending',
    '/api/approval/respond',
    '/api/clarify/pending',
    '/api/clarify/respond',
    '/api/crons',
    '/api/crons/create',
    '/api/crons/delete',
    '/api/crons/pause',
    '/api/crons/resume',
    '/api/crons/run',
    '/api/crons/update',
    '/api/memory',
    '/api/memory/write',
    '/api/projects',
    '/api/push/fcm/register',
    '/api/push/fcm/unregister',
    '/api/reasoning',
    '/api/share/create',
    '/api/share/revoke',
    '/api/skills',
    '/api/skills/content',
    '/api/skills/delete',
    '/api/skills/save',
    '/api/skills/toggle',
    '/api/transcribe',
    '/api/tts',
    '/api/workspaces',
})


def test_allowlist_is_exact_and_not_public():
    import api.auth as auth

    assert isinstance(auth.NATIVE_BEARER_EXACT_PATHS, frozenset)
    assert auth.NATIVE_BEARER_EXACT_PATHS == EXPECTED_EXACT_PATHS
    # Exact matches only: no trailing slashes, no wildcards, never public.
    for path in auth.NATIVE_BEARER_EXACT_PATHS:
        assert path.startswith('/api/')
        assert not path.endswith('/')
        assert '*' not in path
        assert path not in auth.PUBLIC_PATHS


def test_native_bearer_allowlist_gate(monkeypatch):
    import api.auth as auth
    import api.device_pairings as pairings
    import server

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
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)

        def request(method, path, headers=None):
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            response.read()
            return response.status

        try:
            created = pairings.create_pairing()
            assert created["status"] == "pending"
            assert pairings.approve_pairing(created["code"], session_cookie="verified")
            exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
            assert exchanged and exchanged["token_type"] == "Bearer"
            bearer = {"Authorization": f"Bearer {exchanged['access_token']}"}

            # A valid Bearer passes auth on an exact allow-listed route
            # (dispatch may still fail in the unit-test env; auth must not 401).
            status = request("GET", "/api/crons", bearer)
            assert status != 401

            # An invalid Bearer fails closed on an allow-listed route: no
            # cookie fallback masks the bad native token.
            status = request("GET", "/api/crons", {"Authorization": "Bearer hdev_invalidcredential"})
            assert status in (400, 401)

            # Mutating siblings OUTSIDE the exact set are NOT allow-listed:
            # a valid Bearer does not authenticate them and, without a
            # cookie, they stay 401.  (cron CRUD and memory/skills writes
            # ARE allow-listed by owner decision — see
            # tests/test_native_bearer_write_paths.py.)  GET keeps this
            # check on the auth gate without tripping the POST CSRF check.
            status = request("GET", "/api/workspaces/add", bearer)
            assert status == 401

            # Prefix-adjacent paths must not match the exact allow-list.
            status = request("GET", "/api/cronsx", bearer)
            assert status == 401
        finally:
            conn.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
