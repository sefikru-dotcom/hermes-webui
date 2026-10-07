"""Native Bearer authentication for chat endpoints."""
import http.client
import json
import tempfile
import threading
from pathlib import Path


def test_native_bearer_chat_start_and_revocation(monkeypatch):
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

        def request(method, path, payload=None, headers=None):
            body = None if payload is None else json.dumps(payload)
            request_headers = {"Content-Type": "application/json"} if body else {}
            request_headers.update(headers or {})
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            raw = response.read()
            try:
                result = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                result = {}
            return response.status, result

        try:
            created = pairings.create_pairing()
            assert created["status"] == "pending"
            assert pairings.approve_pairing(created["code"], session_cookie="verified")
            exchanged = pairings.exchange_pairing(created["pair_id"], created["poll_secret"])
            assert exchanged and exchanged["token_type"] == "Bearer"
            token = exchanged["access_token"]

            status, _ = request("POST", "/api/chat/start", {"message": "native auth test"},
                                {"Authorization": f"Bearer {token}"})
            # Chat start must pass auth; agent runtime may be unavailable in unit-test env.
            assert status != 401
            assert pairings.verify_native_credential(token)

            assert pairings.revoke_device(token, exchanged["device_id"])
            status, _ = request("POST", "/api/chat/start", {"message": "revoked"},
                                {"Authorization": f"Bearer {token}"})
            assert status == 401
            # The route parser rejects malformed credentials before dispatch.
            status, _ = request("POST", "/api/chat/start", {"message": "invalid"},
                                {"Authorization": "Bearer hdev_invalidcredential"})
            assert status in (400, 401)
        finally:
            conn.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
