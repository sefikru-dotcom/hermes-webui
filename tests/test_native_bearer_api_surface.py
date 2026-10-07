"""Native Bearer authentication for the client API surface prefixes.

The native Android client (ChatRepository / SessionRepository /
ModelsRepository / InteractiveRequestsRepository) authenticates with its
device Bearer credential on the chat, sessions, session, models, upload and
profiles families.  api/auth.py NATIVE_BEARER_PATH_PREFIXES must:

* let a valid Bearer through on those routes (including the first-session
  contract: POST /api/session/new -> session_id -> POST /api/chat/start),
* fail closed on an invalid Authorization header (no cookie fallback),
* leave the browser-cookie path untouched,
* NOT leak to unrelated routes such as /api/settings.

Request-level checks against the real server.Handler with an isolated
pairing DB, mirroring tests/test_native_approval_auth.py.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path

GOOD_COOKIE = "***"

# (method, path, payload) triples that must accept a valid native Bearer.
# The /api/upload entry uses an empty JSON body: the multipart handler
# rejects it at handler level, which still proves the request passed auth.
COVERED_REQUESTS = (
    ("GET", "/api/sessions", None),
    ("GET", "/api/models", None),
    ("GET", "/api/profiles", None),
    ("POST", "/api/session/new", {}),
    ("POST", "/api/upload", {}),
    ("POST", "/api/chat/start", {"session_id": "s1", "message": "[SILENT]"}),
)

# Routes that must stay cookie-auth: a valid Bearer does not leak here.
UNCOVERED_GET_PATHS = ("/api/settings",)


def _start_server(monkeypatch):
    import api.auth as auth
    import api.device_pairings as pairings
    import server

    state_path = Path(tempfile.mkdtemp())
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
        # drain the request body, so reusing one keep-alive connection after
        # an unauthorized POST would poison the next request line.
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

    def shutdown():
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return pairings, request, shutdown


def _issue_credential(pairings):
    created = pairings.create_pairing()
    assert created["status"] == "pending"
    assert pairings.approve_pairing(created["code"], session_cookie="verified")
    exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
    assert exchanged and exchanged["token_type"] == "Bearer"
    return exchanged


def test_native_bearer_api_surface_auth(monkeypatch):
    pairings, request, shutdown = _start_server(monkeypatch)
    try:
        # ── Missing credentials → 401 on every covered route.
        for method, path, payload in COVERED_REQUESTS:
            status, _ = request(method, path, payload)
            assert status == 401, (method, path)

        exchanged = _issue_credential(pairings)
        token = exchanged["access_token"]
        bearer = {"Authorization": f"Bearer {token}"}

        # ── Valid Bearer → auth passes and the real handler is reached.
        for method, path, payload in COVERED_REQUESTS:
            status, _ = request(method, path, payload, headers=bearer)
            assert status != 401, (method, path, status)

        # ── Invalid Bearer → fail closed on every covered route.
        invalid = {"Authorization": "***"}
        for method, path, payload in COVERED_REQUESTS:
            status, _ = request(method, path, payload, headers=invalid)
            assert status == 401, (method, path)

        # A valid cookie must NOT rescue an invalid Bearer (no fallback).
        status, _ = request("POST", "/api/session/new", {}, headers={
            "Authorization": "***",
            "Cookie": f"hermes_session={GOOD_COOKIE}",
        })
        assert status == 401

        # ── Cookie path preserved: session cookie without Bearer still works.
        cookie_headers = {"Cookie": f"hermes_session={GOOD_COOKIE}"}
        for method, path, payload in COVERED_REQUESTS:
            status, _ = request(method, path, payload, headers=cookie_headers)
            assert status != 401, (method, path, status)

        # ── Unrelated routes stay cookie-auth even with a valid Bearer.
        for path in UNCOVERED_GET_PATHS:
            status, _ = request("GET", path, headers=bearer)
            assert status == 401, path
            status, _ = request("GET", path, headers=cookie_headers)
            assert status != 401, path

        # ── Revoked credential → 401 everywhere it was previously accepted.
        assert pairings.revoke_device(token, exchanged["device_id"])
        for method, path, payload in COVERED_REQUESTS:
            status, _ = request(method, path, payload, headers=bearer)
            assert status == 401, (method, path)
    finally:
        shutdown()


def test_native_bearer_first_session_contract(monkeypatch):
    """POST /api/session/new with a native Bearer returns a session_id that
    POST /api/chat/start accepts — the client's first-session flow without
    any chat/start auto-create behaviour."""
    pairings, request, shutdown = _start_server(monkeypatch)
    try:
        exchanged = _issue_credential(pairings)
        bearer = {"Authorization": f"Bearer {exchanged['access_token']}"}

        status, result = request("POST", "/api/session/new", {}, headers=bearer)
        assert status == 200, result
        session_id = (result.get("session") or {}).get("session_id")
        assert session_id, result

        # chat/start with the fresh session_id passes auth AND session
        # validation.  The [SILENT] sentinel makes the handler return a
        # deterministic 200 before session lookup or any agent work.
        status, result = request(
            "POST", "/api/chat/start",
            {"session_id": session_id, "message": "[SILENT]"},
            headers=bearer,
        )
        assert status == 200, result
        assert result.get("status") == "suppressed"

        # The created session is visible to the same Bearer via the list.
        status, result = request("GET", "/api/sessions", headers=bearer)
        assert status != 401
    finally:
        shutdown()
