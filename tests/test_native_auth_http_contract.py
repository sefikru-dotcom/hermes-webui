import io
from types import SimpleNamespace
from urllib.parse import urlsplit


class Headers(dict):
    def get(self, key, default=None):
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default

    def get_all(self, key, default=None):
        value = self.get(key)
        return default if value is None else ([value] if isinstance(value, str) else value)


class Handler:
    def __init__(self, headers=None, ip="127.0.0.1"):
        self.headers = Headers(headers or {})
        self.client_address = (ip, 1)
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status): self.status = status
    def send_header(self, key, value): self.sent_headers.append((key, value))
    def end_headers(self): pass


def test_status_rejects_secret_in_query(monkeypatch):
    from api import routes
    h = Handler()
    routes.handle_get(h, urlsplit("/api/v1/auth/device-pairings/status?pair_id=p&poll_secret=s"))
    assert h.status == 400


def test_status_never_accepts_secret_header(monkeypatch):
    from api import routes
    h = Handler({"X-Hermes-Poll-Secret": "secret"})
    routes.handle_get(h, urlsplit("/api/v1/auth/device-pairings/status?pair_id=p"))
    assert h.status == 400


def test_exchange_requires_exact_body_and_secret(monkeypatch):
    from api import routes
    monkeypatch.setattr(routes, "read_body", lambda _h: {"poll_secret": "s", "extra": 1})
    h = Handler()
    routes.handle_post(h, SimpleNamespace(path="/api/v1/auth/device-pairings/p/exchange", query=""))
    assert h.status == 400


def test_revoke_requires_single_authorization_header(monkeypatch):
    from api import routes
    h = Handler({"Authorization": ["Bearer a", "Bearer b"]})
    monkeypatch.setattr(routes, "read_body", lambda _h: {})
    routes.handle_post(h, SimpleNamespace(path="/api/v1/auth/device-credentials/revoke", query=""))
    assert h.status == 401


def test_versioned_device_revoke_rejects_wrong_device(monkeypatch):
    from api import routes
    monkeypatch.setattr(routes, "read_body", lambda _h: {})
    h = Handler({"Authorization": "Bearer hdev_invalid"})
    routes.handle_post(h, SimpleNamespace(path="/api/v1/auth/devices/dev_wrong/revoke", query=""))
    assert h.status == 401
