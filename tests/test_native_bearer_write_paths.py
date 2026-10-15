"""Native Bearer admission for the owner-approved mutating paths.

The cron CRUD, memory-write, skills-save/delete/toggle, tts-synthesis and
share-create/revoke routes are deliberately granted to the device token so
the native Android client can manage them without a browser session (owner
decision).  This module pins that contract:

  * a valid device Bearer authenticates every one of these POST routes and
    reaches dispatch (200);
  * an invalid / garbage / missing Bearer fails closed with 401 and never
    falls back to the cookie path;
  * the same paths as GET also authenticate (read-back is harmless) — this
    keeps the check on the auth gate and avoids tripping the POST CSRF
    gate in the stubbed server;
  * prefix/suffix neighbours still reject the Bearer (exact-match only).

Dispatch is stubbed with a sentinel router, so no real cron/memory/skills
handler ever executes.  Credentials are synthetic, loopback-only and never
printed.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path

import pytest


# The mutating routes deliberately admitted to the device token by owner
# decision (kept in sync with api/auth.py NATIVE_BEARER_EXACT_PATHS).
WRITE_PATHS = (
    '/api/crons/create',
    '/api/crons/update',
    '/api/crons/delete',
    '/api/memory/write',
    '/api/skills/save',
    '/api/skills/delete',
    '/api/skills/toggle',
    '/api/tts',
    '/api/share/create',
    '/api/share/revoke',
)


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


def test_write_paths_are_in_exact_allowlist():
    """Static pin: every owner-approved write path lives in EXACT_PATHS."""
    import api.auth as auth

    for path in WRITE_PATHS:
        assert path in auth.NATIVE_BEARER_EXACT_PATHS, f"{path} missing from allowlist"


def test_valid_bearer_passes_every_write_path(native_server):
    auth, pairings, port, dispatched = native_server
    token = _mint_native_token(pairings)
    assert pairings.verify_native_credential(token)
    headers = {"Authorization": f"Bearer {token}"}
    for path in WRITE_PATHS:
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
def test_garbage_authorization_fails_closed_on_write_paths(native_server, authorization):
    _auth, _pairings, port, dispatched = native_server
    for path in WRITE_PATHS:
        dispatched.clear()
        status, payload = _request(
            port=port,
            method="POST",
            path=path,
            headers={"Authorization": authorization},
        )
        assert status == 401, (path, status)
        assert payload == {"error": "Authentication required"}
        assert dispatched == []


def test_missing_bearer_is_rejected_on_write_paths(native_server):
    _auth, _pairings, port, dispatched = native_server
    for path in WRITE_PATHS:
        for method in ("GET", "POST"):
            status, payload = _request(port, method, path)
            assert status == 401, (method, path, status)
            assert payload == {"error": "Authentication required"}
    assert dispatched == []


def test_prefix_suffix_neighbours_still_reject_bearer(native_server):
    """Exact-match semantics: trailing slash / extra suffix stay 401."""
    _auth, pairings, port, dispatched = native_server
    token = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}
    for path in (
        "/api/crons/creates",      # suffix neighbour
        "/api/crons/create/",     # trailing slash
        "/api/memory/writes",     # suffix neighbour
        "/api/memory/write/",     # trailing slash
        "/api/skills/saves",      # suffix neighbour
        "/api/skills/save/",      # trailing slash
        "/api/skills/deletes",    # suffix neighbour
        "/api/skills/toggles",    # suffix neighbour
        "/api/ttsx",              # suffix neighbour
        "/api/tts/",              # trailing slash
        # NB: no '/api/share/…' neighbours here — the /api/share/ prefix is
        # public in check_auth (token viewer), so a neighbour such as
        # /api/share/creates passes the gate and only 404s in real dispatch;
        # exact-match semantics of the mutations are pinned by the
        # EXPECTED_EXACT_PATHS equality tests instead.
    ):
        dispatched.clear()
        status, _payload = _request(port, "POST", path, headers)
        assert status == 401, (path, status)
    assert dispatched == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
