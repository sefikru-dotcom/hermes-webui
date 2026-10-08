"""Native Bearer authentication for GET /api/workspaces.

The native client renders the workspace list from GET /api/workspaces using
its device Bearer credential, so api/auth.py NATIVE_BEARER_EXACT_PATHS must
admit exactly that route: valid Bearer passes, missing/invalid/revoked
credentials 401 fail-closed (no cookie fallback for an invalid Bearer), the
browser-cookie path is preserved, and the exact entry does NOT extend to the
mutating siblings (/api/workspaces/{add,remove,rename,reorder}),
/api/workspaces/suggest, or unrelated routes such as /api/settings.

Request-level checks against the real server.Handler with an isolated
pairing DB and stubbed workspace storage (no real workspace data is read or
written), mirroring tests/test_native_approval_auth.py.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path

GOOD_COOKIE = "***"
FAKE_WORKSPACES = [{"path": "/tmp/native-auth-test-ws", "label": "test-ws"}]
FAKE_LAST = "/tmp/native-auth-test-ws"


def test_native_bearer_workspaces_auth(monkeypatch):
    import api.auth as auth
    import api.device_pairings as pairings
    import api.routes as routes
    import server

    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(auth, "verify_session", lambda value: value == GOOD_COOKIE)
        monkeypatch.setattr(
            auth,
            "get_session_info",
            lambda value: (
                {"auth_type": "password", "username": "tester", "bound_profile": None}
                if value == GOOD_COOKIE
                else None
            ),
        )
        # Stub workspace storage so the handler never touches real state.
        monkeypatch.setattr(routes, "load_workspaces", lambda: list(FAKE_WORKSPACES))
        monkeypatch.setattr(routes, "get_last_workspace", lambda: FAKE_LAST)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)
        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def request(method, path, payload=None, headers=None):
            # Fresh connection per request: a check_auth 401 on POST does not
            # drain the request body, so reusing one keep-alive connection
            # after an unauthorized POST would poison the next request line.
            local = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            try:
                body = None if payload is None else json.dumps(payload)
                request_headers = {"Content-Type": "application/json"} if body else {}
                request_headers.update(headers or {})
                local.request(method, path, body=body, headers=request_headers)
                response = local.getresponse()
                raw = response.read()
                try:
                    result = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    result = {}
                return response.status, result
            finally:
                local.close()

        try:
            # ── Missing credentials → 401.
            status, _ = request("GET", "/api/workspaces")
            assert status == 401

            # Issue a real native credential through the pairing flow.
            created = pairings.create_pairing()
            assert created["status"] == "pending"
            assert pairings.approve_pairing(created["code"], session_cookie="verified")
            exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
            assert exchanged and exchanged["token_type"] == "Bearer"
            token = exchanged["access_token"]
            assert pairings.verify_native_credential(token)
            bearer = {"Authorization": f"Bearer {token}"}

            # ── Valid Bearer → 200 and the real workspace payload.
            status, result = request("GET", "/api/workspaces", headers=bearer)
            assert status == 200
            assert result.get("workspaces") == FAKE_WORKSPACES
            assert result.get("last") == FAKE_LAST

            # ── Invalid Bearer → fail closed, no cookie fallback.
            invalid = {"Authorization": "***"}
            status, _ = request("GET", "/api/workspaces", headers=invalid)
            assert status == 401
            status, _ = request("GET", "/api/workspaces", headers={
                "Authorization": "***",
                "Cookie": f"hermes_session={GOOD_COOKIE}",
            })
            assert status == 401

            # ── Cookie path preserved: session cookie without Bearer works.
            cookie_headers = {"Cookie": f"hermes_session={GOOD_COOKIE}"}
            status, result = request("GET", "/api/workspaces", headers=cookie_headers)
            assert status == 200
            assert result.get("workspaces") == FAKE_WORKSPACES

            # ── Unrelated-path regression: the exact /api/workspaces entry is
            # not a prefix.  Mutating siblings, suggest, and cookie-auth routes
            # still 401 for a valid Bearer without a cookie.
            for path in ("/api/workspaces/suggest",
                         "/api/settings"):
                status, _ = request("GET", path, headers=bearer)
                assert status == 401, path
            for path in ("/api/workspaces/add",
                         "/api/workspaces/remove",
                         "/api/workspaces/rename",
                         "/api/workspaces/reorder"):
                status, _ = request("POST", path, {}, headers=bearer)
                assert status == 401, path

            # ── Revoked credential → 401 again.
            assert pairings.revoke_device(token, exchanged["device_id"])
            status, _ = request("GET", "/api/workspaces", headers=bearer)
            assert status == 401
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
