"""Real Chromium smoke against the production static shell.

The harness serves the checked-in static/index.html, style.css and JavaScript
unchanged. Only the HTML template tokens and network API responses are mocked;
no replacement UI is injected. This is an offline shell/interaction check, not
live-server or device acceptance.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import pytest

pw_api = pytest.importorskip("playwright.sync_api")
REPO = Path(__file__).parents[1]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            html = (REPO / "static/index.html").read_text(encoding="utf-8")
            html = (html.replace("__WEBUI_VERSION__", "mobile-smoke-v27")
                    .replace("__MAX_UPLOAD_BYTES__", "10485760")
                    .replace("__CSRF_TOKEN_JSON__", '"smoke-token"'))
            self._send(200, "text/html; charset=utf-8", html.encode())
            return
        if self.path.startswith("/static/"):
            path = (REPO / self.path.lstrip("/").split("?", 1)[0]).resolve()
            if REPO.resolve() not in path.parents or not path.is_file():
                self._send(404, "text/plain", b"not found")
                return
            self._send(200, "text/javascript" if path.suffix == ".js" else "text/css", path.read_bytes())
            return
        if self.path.startswith("/music/"):
            self._send(200, "text/html", b"<title>Music mocked data</title>")
            return
        if self.path.startswith("/api/"):
            self._json({})
            return
        self._send(404, "text/plain", b"not found")

    def do_POST(self):
        # Deterministic mocked API: enough for boot/session creation, never prod.
        if self.path.startswith("/api/session/new"):
            self._json({"session": {"session_id": "mobile-smoke-session"}})
        else:
            self._json({})

    def _json(self, value):
        self._send(200, "application/json", json.dumps(value).encode())

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextmanager
def production_shell():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=2)


@contextmanager
def browser():
    with pw_api.sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            yield chromium
        finally:
            chromium.close()


def open_page(chromium, url, width=360, height=640):
    context = chromium.new_context(viewport={"width": width, "height": height}, is_mobile=True, has_touch=True)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("#composerMobileConfigBtn")
    return context, page, errors


@pytest.mark.parametrize("width", [320, 360, 393])
def test_production_shell_mobile_geometry_and_overflow(width):
    with production_shell() as url, browser() as chromium:
        context, page, errors = open_page(chromium, url, width)
        page.locator("#msg").fill("keyboard-like smoke")
        page.locator("#msg").focus()
        send = page.locator("#btnSend").bounding_box()
        assert send and send["width"] >= 44 and send["height"] >= 44
        assert page.locator("body").evaluate("e => e.scrollWidth <= e.clientWidth")
        page.set_viewport_size({"width": width, "height": 420})
        send = page.locator("#btnSend").bounding_box()
        assert send and send["y"] >= 0 and send["y"] + send["height"] <= 420
        assert not errors, errors
        context.close()


def test_production_music_chat_new_session_and_command_center():
    with production_shell() as url, browser() as chromium:
        context, page, errors = open_page(chromium, url)
        # These are production controls and handlers, with only API responses mocked.
        page.locator("#btnHamburger").click()
        sidebar = page.locator(".sidebar")
        assert "mobile-open" in (sidebar.get_attribute("class") or "")
        page.locator(".sidebar [data-panel='music']").click()
        page.locator("#mainMusic").wait_for(state="visible")
        assert page.locator("#musicIframe").count() == 1
        page.evaluate("switchPanel('chat',{fromRailClick:true});closeMobileSidebar()")
        page.locator("#mainChat").wait_for(state="visible")
        page.locator("#btnTitlebarNewChat").click()
        page.wait_for_function("!document.querySelector('#btnNewChat').disabled")
        page.evaluate("document.querySelector('#btnNewChat').click()")
        page.locator("#composerMobileConfigBtn").click()
        panel = page.locator("#composerMobileConfigPanel")
        assert panel.is_visible()
        for control in ("#composerMobileAttachAction", "#composerMobileSavedPromptsAction", "#composerMobileToolsetsAction"):
            target = page.locator(control)
            assert target.is_visible()
            box = target.bounding_box()
            assert box and box["width"] >= 44 and box["height"] >= 44, (control, box)
        page.locator("#msg").press("Escape")
        assert not panel.is_visible()
        assert not errors, errors
        context.close()


def test_production_transcript_scroll_and_stream_mock_boundary():
    with production_shell() as url, browser() as chromium:
        context, page, errors = open_page(chromium, url)
        # Existing production transcript container, populated by mocked empty APIs.
        assert page.locator("#messages").count() == 1
        overflow = page.locator("#messages").evaluate("e => getComputedStyle(e).overflowY")
        assert overflow in ("auto", "scroll")
        assert page.locator(".messages-inner").evaluate("e => getComputedStyle(e).overflowY") == "visible"
        assert not errors, errors
        context.close()
