"""Native Bearer authentication for approval/clarify interactive endpoints.

Mirrors tests/test_native_chat_auth.py: request-level checks against the real
server.Handler with an isolated pairing DB.  The Bearer fast-path in
api/auth.py must cover exactly the pending/respond routes (GET
/api/{approval,clarify}/pending, POST /api/{approval,clarify}/respond),
fail closed on an invalid Authorization header (no cookie fallback), and
leave both the cookie path and unrelated routes untouched.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path

PENDING_GET_PATHS = ("/api/approval/pending", "/api/clarify/pending")
RESPOND_POST_PATHS = ("/api/approval/respond", "/api/clarify/respond")
GOOD_COOKIE = "test-cookie-token"


def test_native_bearer_approval_clarify_auth(monkeypatch):
    import api.auth as auth
    import api.device_pairings as pairings
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
        monkeypatch.setattr(server, "check_auth", auth.check_auth)
        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def request(method, path, payload=None, headers=None):
            # Fresh connection per request: a check_auth 401 on POST does not
            # drain the request body, so reusing one keep-alive connection
            # after an unauthorized POST would poison the next request line.
            # (Pre-existing server behaviour, identical for the chat fast-path.)
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
            # ── Missing credentials → 401 on every covered interactive route.
            for path in PENDING_GET_PATHS:
                status, _ = request("GET", path)
                assert status == 401, path
            for path in RESPOND_POST_PATHS:
                status, _ = request("POST", path, {})
                assert status == 401, path

            # Issue a real native credential through the pairing flow.
            created = pairings.create_pairing()
            assert created["status"] == "pending"
            assert pairings.approve_pairing(created["code"], session_cookie="verified")
            exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
            assert exchanged and exchanged["token_type"] == "Bearer"
            token = exchanged["access_token"]
            assert pairings.verify_native_credential(token)
            bearer = {"Authorization": f"Bearer {token}"}

            # ── Valid Bearer → auth passes and the real handler is reached.
            for path in PENDING_GET_PATHS:
                status, _ = request("GET", path, headers=bearer)
                assert status == 200, path
            for path in RESPOND_POST_PATHS:
                # Empty body → handler-level 400 ("session_id is required"),
                # which proves the request got past auth to the route.
                status, result = request("POST", path, {}, headers=bearer)
                assert status == 400, path
                assert "session_id" in result.get("error", "")

            # ── Invalid Bearer → fail closed on covered routes.
            invalid = {"Authorization": "Bearer hdev_invalidcredential"}
            for path in PENDING_GET_PATHS:
                status, _ = request("GET", path, headers=invalid)
                assert status == 401, path
            for path in RESPOND_POST_PATHS:
                status, _ = request("POST", path, {}, headers=invalid)
                assert status == 401, path
            # A valid cookie must NOT rescue an invalid Bearer (no fallback).
            status, _ = request("GET", "/api/approval/pending", headers={
                "Authorization": "Bearer hdev_invalidcredential",
                "Cookie": f"hermes_session={GOOD_COOKIE}",
            })
            assert status == 401
            status, _ = request("POST", "/api/clarify/respond", {}, headers={
                "Authorization": "Bearer hdev_invalidcredential",
                "Cookie": f"hermes_session={GOOD_COOKIE}",
            })
            assert status == 401

            # ── Cookie path preserved: session cookie without Bearer still works.
            cookie_headers = {"Cookie": f"hermes_session={GOOD_COOKIE}"}
            for path in PENDING_GET_PATHS:
                status, _ = request("GET", path, headers=cookie_headers)
                assert status == 200, path
            for path in RESPOND_POST_PATHS:
                status, _ = request("POST", path, {}, headers=cookie_headers)
                assert status == 400, path

            # ── Unrelated-path regression: a valid Bearer does not leak beyond
            # the exact covered routes.  /api/sessions and /api/settings stay
            # cookie-auth, and the SSE stream + loopback inject_test routes are
            # deliberately NOT in NATIVE_BEARER_EXACT_PATHS.
            for path in ("/api/sessions", "/api/settings",
                         "/api/approval/stream", "/api/clarify/stream"):
                status, _ = request("GET", path, headers=bearer)
                assert status == 401, path

            # ── Revoked credential → 401 everywhere it was previously accepted.
            assert pairings.revoke_device(token, exchanged["device_id"])
            for path in PENDING_GET_PATHS:
                status, _ = request("GET", path, headers=bearer)
                assert status == 401, path
            for path in RESPOND_POST_PATHS:
                status, _ = request("POST", path, {}, headers=bearer)
                assert status == 401, path
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
