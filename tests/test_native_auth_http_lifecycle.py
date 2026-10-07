"""Real loopback HTTP lifecycle using synthetic, verified browser identity.

This never connects to production and never uses production cookies or tokens.
"""
import http.client
import json
import tempfile
import threading
from pathlib import Path
import os

os.environ["HERMES_WEBUI_COOKIE_NAME"] = "hermes_session"


def test_native_auth_real_loopback_lifecycle(monkeypatch):
    import api.auth as auth
    import api.device_pairings as pairings
    import server

    with tempfile.TemporaryDirectory() as state:
        state_path = Path(state)
        monkeypatch.setattr(pairings, "STATE_DIR", state_path)
        monkeypatch.setattr(pairings, "_DB_PATH", state_path / "native_auth.db")
        monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
        monkeypatch.setattr(auth, "verify_session", lambda value: True)
        httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)

        def request(method, path, payload=None, headers=None):
            body = None if payload is None else json.dumps(payload)
            req_headers = {"Content-Type": "application/json"} if body else {}
            req_headers.update(headers or {})
            conn.request(method, path, body=body, headers=req_headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), (json.loads(raw) if raw else {})

        try:
            status, headers, created = request("POST", "/api/v1/auth/device-pairings", {})
            assert status == 200 and headers["Cache-Control"] == "no-store"
            pair_id, poll_secret = created["pair_id"], created["poll_secret"]
            status, headers, pending = request("GET", f"/api/v1/auth/device-pairings/status?pair_id={pair_id}")
            assert status == 200 and pending["pair_id"] == pair_id and pending["status"] == "pending" and "access_token" not in pending
            status, _, approved = request("POST", "/api/v1/auth/device-pairings/approve", {"code": created["code"]}, {"Cookie": "hermes_session=synthetic-verified", "X-Mars-Internal": "1"})
            assert status == 200 and approved["status"] == "approved"
            status, _, exchanged = request("POST", f"/api/v1/auth/device-pairings/{pair_id}/exchange", {"poll_secret": poll_secret})
            assert status == 200 and exchanged["token_type"] == "Bearer"
            status, _, duplicate = request("POST", f"/api/v1/auth/device-pairings/{pair_id}/exchange", {"poll_secret": poll_secret})
            assert status == 409 and "access_token" not in duplicate
            token = exchanged["access_token"]
            status, _, wrong = request("POST", "/api/v1/auth/devices/dev_wrong/revoke", {}, {"Authorization": f"Bearer {token}"})
            assert status == 401
            status, headers, revoked = request("POST", f"/api/v1/auth/devices/{exchanged['device_id']}/revoke", {}, {"Authorization": f"Bearer {token}"})
            assert status == 200 and revoked["status"] == "revoked" and headers["Cache-Control"] == "no-store"

        finally:
            conn.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
