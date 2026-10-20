"""Native Bearer authentication for the exact-path client API allowlist.

Request-level coverage for ``api.auth.NATIVE_BEARER_EXACT_PATHS``: a valid
device Bearer credential passes authentication on every allowlisted path,
while an invalid or missing credential is rejected (fail-closed, no cookie
fallback).  Mutating sibling routes outside the allowlist must NOT accept
the native Bearer credential.  Tokens are never printed.
"""
import http.client
import tempfile
import threading
from pathlib import Path

import pytest

# The exact dispatch paths the native client is allowed to answer with a
# device Bearer credential (kept in sync with api/auth.py; asserted against
# the live frozenset so an accidental removal fails the suite).
EXPECTED_EXACT_PATHS = {
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
    '/api/goal',
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
    # Voice gateway upstream channel (api/voice_session.py).
    '/api/voice/close',
    '/api/voice/interrupt',
    '/api/voice/session',
    '/api/workspaces',
}

# Not allowlisted: mutating/sibling routes must stay on the cookie path and
# therefore reject a native Bearer credential.  (cron CRUD + memory/skills
# writes were deliberately added to the exact set by owner decision; see
# tests/test_native_bearer_write_paths.py.)
NON_ALLOWLISTED_SIBLINGS = (
    '/api/workspaces/add',
    '/api/workspaces/remove',
    '/api/projects/create',
    '/api/crons/',  # trailing slash is not an exact match
    '/api/reasoning/extra',  # suffix is not an exact match
    '/api/transcribe/',  # trailing slash is not an exact match
    '/api/transcribe/capability',  # browser-only probe, never device-token
)

# Mutating routes that must stay on the browser-cookie path even though their
# GET (or a same-name read route) may be device-token readable.  Checked with
# the verb that matters, so a read-only admission can never be mistaken for a
# write admission.
NON_ALLOWLISTED_MUTATIONS = (
    ('POST', '/api/settings'),  # rewrites server-global settings.json
    ('POST', '/api/projects/create'),
    ('POST', '/api/workspaces/add'),
    ('POST', '/api/kanban/tasks'),
)


def test_allowlist_matches_expected_exact_paths():
    import api.auth as auth

    assert set(auth.NATIVE_BEARER_EXACT_PATHS) == EXPECTED_EXACT_PATHS
    # Allowlist entries must never leak into the unauthenticated set.
    assert not (set(auth.NATIVE_BEARER_EXACT_PATHS) & set(auth.PUBLIC_PATHS))


def test_native_bearer_exact_paths_pass_and_reject(monkeypatch):
    import api.auth as auth
    import api.device_pairings as pairings
    import server

    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(auth, "verify_session", lambda value: False)
        monkeypatch.setattr(auth, "is_trusted_auth_enabled", lambda: False)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)
        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=15
        )

        def get(path, headers=None):
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            response.read()
            return response.status

        try:
            created = pairings.create_pairing()
            assert created["status"] == "pending"
            assert pairings.approve_pairing(created["code"], session_cookie="verified")
            exchanged = pairings.exchange_pairing(
                created["pair_id"], created["poll_secret"]
            )
            assert exchanged and exchanged["token_type"] == "Bearer"
            token = exchanged["access_token"]
            assert pairings.verify_native_credential(token)
            valid_headers = {"Authorization": f"Bearer {token}"}
            invalid_headers = {"Authorization": "Bearer hdev_invalidcredential"}

            for path in sorted(EXPECTED_EXACT_PATHS):
                # Valid Bearer passes authentication (dispatch may still 404/405
                # for method-mismatched routes, but must never 401/403).
                status = get(path, valid_headers)
                assert status not in (401, 403), f"valid bearer rejected on {path}"
                # Invalid Bearer fails closed — no cookie fallback.
                status = get(path, invalid_headers)
                assert status in (400, 401), f"invalid bearer not rejected on {path}"
                # Missing Authorization falls to the cookie path, which is
                # patched to reject → 401.
                status = get(path)
                assert status == 401, f"missing bearer not rejected on {path}"

            # Revocation propagates to every allowlisted path.
            assert pairings.revoke_device(token, exchanged["device_id"])
            for path in sorted(EXPECTED_EXACT_PATHS):
                status = get(path, valid_headers)
                assert status == 401, f"revoked bearer accepted on {path}"
        finally:
            conn.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def test_native_bearer_not_accepted_outside_allowlist(monkeypatch):
    """Exact matches only: siblings/prefix extensions reject the Bearer."""
    import api.auth as auth
    import api.device_pairings as pairings
    import server

    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(auth, "verify_session", lambda value: False)
        monkeypatch.setattr(auth, "is_trusted_auth_enabled", lambda: False)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)
        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=15
        )

        def get(path, headers=None):
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            response.read()
            return response.status

        try:
            created = pairings.create_pairing()
            assert pairings.approve_pairing(created["code"], session_cookie="verified")
            exchanged = pairings.exchange_pairing(
                created["pair_id"], created["poll_secret"]
            )
            assert exchanged and exchanged["token_type"] == "Bearer"
            token = exchanged["access_token"]
            valid_headers = {"Authorization": f"Bearer {token}"}

            for path in NON_ALLOWLISTED_SIBLINGS:
                assert path not in auth.NATIVE_BEARER_EXACT_PATHS, path
                # A valid native Bearer grants nothing off the allowlist; with
                # the cookie path patched to reject, the response must be 401.
                status = get(path, valid_headers)
                assert status == 401, f"native bearer leaked onto {path}"
        finally:
            conn.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def test_goal_write_and_settings_read_admission(monkeypatch):
    """POST /api/goal is device-token writable; /api/settings is read-only.

    Both routes are pinned here on their own because the generic loops above
    only exercise GET: the native client needs POST /api/goal (goal control)
    and GET /api/settings (display-mode sync), while POST /api/settings keeps
    rewriting the server-global settings.json and must stay cookie-only.
    Dispatch is stubbed with a sentinel so no real goal/settings handler runs
    (POST /api/settings would otherwise write the live settings file).
    """
    import api.auth as auth
    import api.device_pairings as pairings
    import server

    dispatched = []

    def sentinel(handler, parsed):
        dispatched.append((handler.command, parsed.path))
        payload = b'{"dispatched": true}'
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(auth, "verify_session", lambda value: False)
        monkeypatch.setattr(auth, "is_trusted_auth_enabled", lambda: False)
        monkeypatch.setattr(server, "check_auth", auth.check_auth)
        monkeypatch.setattr(server, "handle_get", sentinel)
        monkeypatch.setattr(server, "handle_post", sentinel)
        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]

        def request(method, path, headers=None):
            # One connection per request: the stubbed dispatch never reads the
            # request body, so a kept-alive socket would leak it into the next
            # request line.
            body = b"{}" if method in ("POST", "PUT", "PATCH") else None
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            try:
                conn.request(method, path, body=body, headers=headers or {})
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()

        try:
            created = pairings.create_pairing()
            assert pairings.approve_pairing(created["code"], session_cookie="verified")
            exchanged = pairings.exchange_pairing(
                created["pair_id"], created["poll_secret"]
            )
            assert exchanged and exchanged["token_type"] == "Bearer"
            valid = {"Authorization": f"Bearer {exchanged['access_token']}"}

            # The write route really is in the exact set, and the read route
            # in the read-only set — asserted by membership, never by 404.
            assert "/api/goal" in auth.NATIVE_BEARER_EXACT_PATHS
            assert "/api/settings" in auth.NATIVE_BEARER_READONLY_EXACT_PATHS
            assert "/api/settings" not in auth.NATIVE_BEARER_EXACT_PATHS

            # POST /api/goal: 401 without a token, admitted with a device token.
            dispatched.clear()
            assert request("POST", "/api/goal") == 401
            assert dispatched == []
            dispatched.clear()
            status = request("POST", "/api/goal", valid)
            assert status == 200, status
            assert dispatched == [("POST", "/api/goal")]

            # GET /api/settings: 401 without a token, admitted with a device token.
            dispatched.clear()
            assert request("GET", "/api/settings") == 401
            assert dispatched == []
            dispatched.clear()
            status = request("GET", "/api/settings", valid)
            assert status == 200, status
            assert dispatched == [("GET", "/api/settings")]

            # POST /api/settings stays cookie-only: the device token grants
            # nothing, and dispatch is never reached.
            dispatched.clear()
            assert request("POST", "/api/settings", valid) == 401
            assert request("POST", "/api/settings") == 401
            assert request(
                "POST", "/api/settings", {"Authorization": "Bearer hdev_invalidcredential"}
            ) == 401
            assert dispatched == []

            # POST-only admission: other verbs on /api/settings are not covered.
            for method in ("PUT", "PATCH", "DELETE"):
                assert request(method, "/api/settings", valid) == 401, method
            assert dispatched == []

            # The mutating siblings stay cookie-only even with a valid device
            # token — no dispatch, so no CSRF bypass.
            for method, path in NON_ALLOWLISTED_MUTATIONS:
                dispatched.clear()
                assert request(method, path, valid) == 401, (method, path)
                assert dispatched == [], (method, path)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
