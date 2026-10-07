import io
import json
from types import SimpleNamespace

from api import native_chat


class Handler:
    def __init__(self, body=b"", authorization="Bearer " + "n" * 24):
        self.headers = {"Authorization": authorization, "Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def body(self):
        return json.loads(self.wfile.getvalue())


def parsed(path, query=""):
    return SimpleNamespace(path=path, query=query)


def allow_native(monkeypatch):
    monkeypatch.setattr(
        native_chat,
        "verify_native_credential",
        lambda token: {"device_id": "device-1", "scopes": ["chat:read", "chat:write"]},
    )


def test_create_run_has_versioned_contract_and_does_not_fake_success(monkeypatch):
    allow_native(monkeypatch)
    handler = Handler(json.dumps({
        "session_id": "session-1",
        "message": "hello",
        "client_request_id": "req-1",
    }).encode())

    handled = native_chat.dispatch(handler, parsed("/api/v1/chat/runs"), "POST")

    assert handled is True
    assert handler.status == 501
    assert handler.body() == {
        "error": "native_chat_unavailable",
        "detail": "Legacy chat integration is not enabled for the versioned native adapter",
    }


def test_events_cancel_steer_and_status_are_explicit_contract_responses(monkeypatch):
    allow_native(monkeypatch)
    cases = [
        ("GET", "/api/v1/chat/runs/run-1/events", ""),
        ("GET", "/api/v1/chat/runs/run-1", ""),
        ("POST", "/api/v1/chat/runs/run-1/cancel", "{}"),
        ("POST", "/api/v1/chat/runs/run-1/steer", '{"message":"next"}'),
    ]

    for method, path, body in cases:
        handler = Handler(body.encode())
        assert native_chat.dispatch(handler, parsed(path), method) is True
        assert handler.status == 501
        assert handler.body()["error"] == "native_chat_unavailable"


def test_native_bearer_is_required_before_contract_response(monkeypatch):
    monkeypatch.setattr(native_chat, "verify_native_credential", lambda token: None)
    handler = Handler(b"{}", authorization="Bearer invalid")

    native_chat.dispatch(handler, parsed("/api/v1/chat/runs/run-1"), "GET")

    assert handler.status == 401
    assert handler.body() == {"error": "Authentication required"}
