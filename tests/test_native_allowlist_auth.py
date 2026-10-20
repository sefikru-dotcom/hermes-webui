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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
