"""Native Bearer admission for the read-only Android-screen routes.

Covers the read-only fast-path in api/auth.py for the new native screens:
GET/HEAD on the single ``/api/kanban/`` prefix plus the exact paths
``/api/crons/delivery-options``, ``/api/crons/history``, ``/api/insights``,
``/api/logs``, ``/api/provider/quota``, ``/api/push/fcm/status`` and
``/api/skills/usage``.

  * before admission (read-only sets monkeypatched empty) a valid device
    Bearer gets 401 on these routes — after admission the same request
    authenticates and reaches dispatch;
  * invalid / garbage / missing Bearer fails closed with 401 and never
    falls back to the browser-cookie path;
  * mutating methods (POST/PUT/PATCH/DELETE) never accept a device Bearer
    on these routes — they stay on the cookie-session + CSRF path, so the
    read-only admission cannot be used to bypass CSRF on kanban mutations;
  * prefix neighbours (``/api/kanban`` without slash, ``/api/kanbanx/...``,
    suffixed exact paths) are not covered;
  * the pre-existing NATIVE_BEARER_EXACT_PATHS set is unchanged (scope
    creep guard) and cookie/CSRF machinery is untouched.

Dispatch is stubbed with a sentinel router, so no real kanban/log/quota
handler ever executes.  Credentials are synthetic, loopback-only and
never printed.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path

import pytest

# Routes admitted for the new Android screens (kept in sync with api/auth.py).
EXPECTED_READONLY_EXACT_PATHS = {
    '/api/crons/delivery-options',
    '/api/crons/history',
    '/api/insights',
    '/api/logs',
    '/api/provider/quota',
    '/api/push/fcm/status',
    '/api/skills/usage',
}
EXPECTED_READONLY_PREFIXES = ('/api/kanban/',)
SAFE_METHODS = ('GET', 'HEAD')

# Representative kanban GET contract from api/kanban_bridge.py.
KANBAN_GET_PATHS = (
    '/api/kanban/board',
    '/api/kanban/boards',
    '/api/kanban/config',
    '/api/kanban/stats',
    '/api/kanban/assignees',
    '/api/kanban/events',
    '/api/kanban/events/stream',
    '/api/kanban/tasks',
    '/api/kanban/tasks/2026-10-12-demo-task',
)
ALL_READONLY_PATHS = tuple(sorted(EXPECTED_READONLY_EXACT_PATHS)) + KANBAN_GET_PATHS

# Mutating kanban/insights/logs/quota requests that must NEVER accept the
# device Bearer (cookie + CSRF path only).
MUTATING_TARGETS = (
    ('POST', '/api/kanban/tasks'),
    ('POST', '/api/kanban/tasks/bulk'),
    ('POST', '/api/kanban/boards'),
    ('POST', '/api/kanban/boards/demo/switch'),
    ('POST', '/api/kanban/dispatch'),
    ('POST', '/api/kanban/links'),
    ('POST', '/api/kanban/links/delete'),
    ('PUT', '/api/kanban/tasks/demo'),
    ('PATCH', '/api/kanban/boards/demo'),
    ('PATCH', '/api/kanban/tasks/demo'),
    ('DELETE', '/api/kanban/boards/demo'),
    ('DELETE', '/api/kanban/tasks/demo'),
    ('POST', '/api/insights'),
    ('POST', '/api/logs'),
    ('POST', '/api/provider/quota'),
    # The cron history / delivery-options reads are GET/HEAD-only too.
    ('POST', '/api/crons/history'),
    ('POST', '/api/crons/delivery-options'),
)

# The exact-path allowlist, pinned so it can never silently widen beyond the
# owner-approved set (scope-creep regression guard).  Includes the cron CRUD,
# memory/skills write and tts/share-create/share-revoke paths deliberately
# granted to the device token.
PINNED_EXACT_PATHS = {
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
    '/api/tts',
    '/api/voice/session',
    '/api/workspaces',
}


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
    return exchanged["access_token"], exchanged["device_id"]


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
            yield auth, pairings, httpd.server_address[1], dispatched, monkeypatch
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


def test_readonly_allowlist_shape_and_scope():
    """Static shape: exact, non-public, safe-methods-only, no scope creep."""
    import api.auth as auth

    assert set(auth.NATIVE_BEARER_READONLY_EXACT_PATHS) == EXPECTED_READONLY_EXACT_PATHS
    assert tuple(auth.NATIVE_BEARER_READONLY_PREFIXES) == EXPECTED_READONLY_PREFIXES
    assert set(auth.NATIVE_BEARER_READONLY_METHODS) <= {"GET", "HEAD"}

    for path in auth.NATIVE_BEARER_READONLY_EXACT_PATHS:
        assert path.startswith('/api/')
        assert not path.endswith('/')
        assert '*' not in path
        assert path not in auth.PUBLIC_PATHS
    for prefix in auth.NATIVE_BEARER_READONLY_PREFIXES:
        assert prefix.startswith('/api/') and prefix.endswith('/')
        assert '*' not in prefix
        # A prefix must never be broad enough to cover all of /api/.
        assert prefix != '/api/'
        assert not any(p.startswith(prefix) for p in auth.PUBLIC_PATHS)

    # The read-only admission is disjoint from — and does not widen — the
    # pre-existing exact-path allowlist or the public set.
    assert set(auth.NATIVE_BEARER_EXACT_PATHS) == PINNED_EXACT_PATHS
    assert not (
        set(auth.NATIVE_BEARER_READONLY_EXACT_PATHS)
        & set(auth.NATIVE_BEARER_EXACT_PATHS)
    )
    assert not (set(auth.NATIVE_BEARER_READONLY_EXACT_PATHS) & set(auth.PUBLIC_PATHS))


def test_401_before_admission_and_authorized_after(native_server):
    """Regression core: 401 while the routes are not admitted, 200 after."""
    auth, pairings, port, dispatched, monkeypatch = native_server
    token, _device_id = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}

    # BEFORE admission: empty read-only sets → valid Bearer grants nothing
    # on the new routes; the (dead) cookie path answers 401.
    with monkeypatch.context() as m:
        m.setattr(auth, "NATIVE_BEARER_READONLY_EXACT_PATHS", frozenset())
        m.setattr(auth, "NATIVE_BEARER_READONLY_PREFIXES", ())
        for path in ALL_READONLY_PATHS:
            dispatched.clear()
            status, payload = _request(port, "GET", path, headers)
            assert status == 401, (path, status)
            assert payload == {"error": "Authentication required"}
            assert dispatched == []

    # AFTER admission (monkeypatch context exited → real sets restored):
    # the same requests authenticate and reach dispatch.
    for path in ALL_READONLY_PATHS:
        dispatched.clear()
        status, payload = _request(port, "GET", path, headers)
        assert status == 200, (path, status)
        assert payload == {"dispatched": path}
        assert dispatched == [("GET", path)]


def test_valid_bearer_passes_safe_methods_only(native_server):
    auth, pairings, port, dispatched, _mp = native_server
    token, _device_id = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}
    for path in ALL_READONLY_PATHS:
        for method in SAFE_METHODS:
            dispatched.clear()
            status, payload = _request(port, method, path, headers)
            assert status == 200, (method, path, status)
            # do_HEAD flushes real headers but drops the body (RFC 9110),
            # so only GET carries the sentinel JSON payload.
            if method == "GET":
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
    _auth, _pairings, port, dispatched, _mp = native_server
    for path in ALL_READONLY_PATHS:
        dispatched.clear()
        status, payload = _request(
            port=port,
            method="GET",
            path=path,
            headers={"Authorization": authorization},
        )
        assert status == 401, (path, status)
        assert payload == {"error": "Authentication required"}
        assert dispatched == []


def test_missing_bearer_is_not_cookie_free_pass(native_server):
    _auth, _pairings, port, dispatched, _mp = native_server
    for path in ALL_READONLY_PATHS:
        for method in SAFE_METHODS:
            status, payload = _request(port, method, path)
            assert status == 401, (method, path, status)
            if method == "GET":  # HEAD responses carry no body
                assert payload == {"error": "Authentication required"}
    assert dispatched == []


def test_mutating_methods_never_accept_device_bearer(native_server):
    """CSRF/method guard: unsafe verbs stay on the cookie path (401 here).

    check_auth rejects before handle_post/put/patch/delete (which carry
    _check_csrf) ever runs, so the read-only admission cannot be used to
    reach kanban mutations — or to skip CSRF on them — with a Bearer.
    """
    _auth, pairings, port, dispatched, _mp = native_server
    token, _device_id = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}
    for method, path in MUTATING_TARGETS:
        dispatched.clear()
        status, payload = _request(port, method, path, headers)
        assert status == 401, (method, path, status)
        assert payload == {"error": "Authentication required"}
        assert dispatched == []


def test_prefix_neighbours_not_covered(native_server):
    """Only '/api/kanban/' prefix and the three exact paths are admitted."""
    _auth, pairings, port, dispatched, _mp = native_server
    token, _device_id = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}
    for path in (
        '/api/kanban',            # missing trailing slash
        '/api/kanbanx/board',     # prefix neighbour
        '/api/kanban2/tasks',     # prefix neighbour
        '/api/insights/',         # trailing slash is not an exact match
        '/api/insights/extra',    # suffix is not an exact match
        '/api/logs/live',         # suffix is not an exact match
        '/api/provider',          # parent of the exact quota path
        '/api/provider/',         # parent of the exact quota path
        '/api/provider/quotas',   # neighbour
        '/api/provider/quota/x',  # suffix is not an exact match
        '/api/crons/histories',   # suffix is not an exact match
        '/api/crons/delivery-option',  # missing trailing 's'
    ):
        dispatched.clear()
        status, _payload = _request(port, "GET", path, headers)
        assert status == 401, (path, status)
        assert dispatched == []


def test_revoked_device_rejected_on_readonly_routes(native_server):
    auth, pairings, port, dispatched, _mp = native_server
    token, device_id = _mint_native_token(pairings)
    headers = {"Authorization": f"Bearer {token}"}
    # Sanity: admitted before revocation.
    status, _payload = _request(port, "GET", "/api/kanban/board", headers)
    assert status == 200
    assert pairings.revoke_device(token, device_id)
    for path in ALL_READONLY_PATHS:
        dispatched.clear()
        status, payload = _request(port, "GET", path, headers)
        assert status == 401, (path, status)
        assert payload == {"error": "Authentication required"}
        assert dispatched == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
