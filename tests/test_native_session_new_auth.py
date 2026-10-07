"""Native Bearer auth for POST /api/session/new — response-contract level.

Complements tests/test_native_bearer_api_surface.py (surface-wide coverage of
the NATIVE_BEARER_PATH_PREFIXES fast-path) with assertions pinned to the
route's exact contract, mirroring the request-level style of
tests/test_native_approval_auth.py: real server.Handler, isolated pairing DB
(conftest isolates HERMES_HOME/state/workspace).

Route contract (api/routes.py ``handle_post``, ``/api/session/new``):
POST with a JSON body whose fields are all optional (``prev_session_id``,
``workspace``, ``worktree``, ``model``, ``model_provider``,
``enabled_toolsets``, ``profile``, ``project_id``); success is 200
``{"session": {...}}`` where the projection carries ``session_id``
(new_session is in-memory until the first message, so this has no durable
side effects).  An empty JSON body is the native first-session call.

Auth contract: a valid device Bearer credential is accepted; an invalid one
fails closed with 401 (a valid cookie must NOT rescue it); missing
credentials 401; the browser cookie path keeps working; a revoked credential
401s.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path

NEW_SESSION_PATH = "/api/session/new"
GOOD_COOKIE = "test-cookie-token"


def _session_id_from(result):
    session = result.get("session")
    assert isinstance(session, dict), f"response has no session object: {result}"
    sid = session.get("session_id")
    assert isinstance(sid, str) and sid.strip(), f"session_id missing: {session}"
    return sid


def test_native_bearer_session_new_auth(monkeypatch):
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
            local = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
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
            status, _ = request("POST", NEW_SESSION_PATH, {})
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

            # ── Invalid Bearer → fail closed, even with a valid cookie.
            status, _ = request(
                "POST", NEW_SESSION_PATH, {},
                headers={"Authorization": "Bearer hdev_invalidcredential"},
            )
            assert status == 401
            status, _ = request(
                "POST", NEW_SESSION_PATH, {},
                headers={
                    "Authorization": "Bearer hdev_invalidcredential",
                    "Cookie": f"hermes_session={GOOD_COOKIE}",
                },
            )
            assert status == 401

            # ── Valid Bearer → 200 and the response carries session_id.
            status, result = request("POST", NEW_SESSION_PATH, {}, headers=bearer)
            assert status == 200, result
            bearer_sid = _session_id_from(result)
            assert set(result) <= {"session", "worktree_skipped"}, result

            # Explicit-body shape of the same contract (native first session
            # that opts out of worktrees and hands no previous session).
            status, result = request(
                "POST", NEW_SESSION_PATH, {"worktree": False}, headers=bearer
            )
            assert status == 200, result
            assert _session_id_from(result) != bearer_sid

            # ── Cookie path preserved: session cookie without Bearer still works.
            status, result = request(
                "POST", NEW_SESSION_PATH, {},
                headers={"Cookie": f"hermes_session={GOOD_COOKIE}"},
            )
            assert status == 200, result
            _session_id_from(result)

            # ── Unrelated-path regression: a valid Bearer does not leak to
            # cookie-auth routes outside the covered native surface.
            status, _ = request("GET", "/api/settings", headers=bearer)
            assert status == 401

            # ── Revoked credential → 401.
            assert pairings.revoke_device(token, exchanged["device_id"])
            status, _ = request("POST", NEW_SESSION_PATH, {}, headers=bearer)
            assert status == 401
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
