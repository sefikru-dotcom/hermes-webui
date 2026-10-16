"""Regression: an early 401 must not poison the keep-alive connection.

Before the fix, ``check_auth`` answered an unauthenticated POST without
reading the request body. Under HTTP/1.1 keep-alive the leftover body bytes
were parsed as the next request line, so a client that reused the connection
saw ``401`` followed by ``501 Unsupported method ('{"x":1}POST')``.

This test drives a real loopback server with auth enabled and reuses one
socket for consecutive requests; every response must be a clean 401.
Never touches production state: the server runs on its own ephemeral port.
"""
import http.client
import json
import threading
import os

os.environ.setdefault("HERMES_WEBUI_COOKIE_NAME", "hermes_session")


def _serve():
    import api.auth as auth
    import server

    original = auth.is_auth_enabled
    auth.is_auth_enabled = lambda: True
    httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, original


def test_early_deny_drains_body_and_keeps_connection():
    httpd, thread, original = _serve()
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    try:
        statuses = []
        for i in range(3):
            payload = json.dumps({"x": i})
            conn.request(
                "POST",
                "/api/sessions",
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            statuses.append(resp.status)
        # A 501 in position 2/3 means the previous body leaked into the next
        # request line; every request is unauthenticated, so all must be 401.
        assert statuses == [401, 401, 401], statuses
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        import api.auth as auth

        auth.is_auth_enabled = original


def test_early_deny_drains_for_all_write_methods():
    httpd, thread, original = _serve()
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    try:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            statuses = []
            for _ in range(2):
                conn.request(
                    method,
                    "/api/sessions",
                    body=json.dumps({"x": 1}),
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                resp.read()
                statuses.append(resp.status)
            assert statuses == [401, 401], (method, statuses)
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        import api.auth as auth

        auth.is_auth_enabled = original
