#!/usr/bin/env python3
"""Mock HTTP + SSE server for the WebUI browser regression harness.

WHY THIS EXISTS
  The upgrade wave changes chat rendering (activity display modes), appearance
  settings, scroll behaviour and composer geometry. Those are only observable in
  a real browser executing the REAL static application. This server makes that
  possible without auth, without a live agent and without production data:

    * serves the real ``static/`` tree from an app root (worktree today,
      integration tree after merge — ``--app-root`` / ``UPGRADE_APP_ROOT``),
    * substitutes the three request-time tokens the real server injects into
      ``static/index.html`` (``__WEBUI_VERSION__``, ``__MAX_UPLOAD_BYTES__``,
      ``__CSRF_TOKEN_JSON__``) — nothing else is rewritten,
    * answers ``/api/*`` from JSON fixtures in ``tests/fixtures/upgrade_chat/``
      and replays a scripted SSE turn,
    * exposes ``/__mock/*`` introspection so the harness can assert on server
      side facts (how many SSE connections were opened, what a settings POST
      actually contained) instead of trusting the UI.

  It contains NO application logic: no markdown rendering, no activity-mode
  decisions, no scroll policy. Fixtures are pure data; the only code here is
  HTTP plumbing and a queue of ``{event, data, delay_ms}`` records.

ISOLATION
  Binds 127.0.0.1 on an ephemeral port, reads only the app root and the fixture
  directory, writes nothing outside a temp dir, never talks to the network and
  never touches a live config/state dir.

USAGE
  python3 scripts/upgrade_browser_server.py --app-root . --fixture-dir tests/fixtures/upgrade_chat
  (prints ``PORT <n>`` on stdout once listening; the harness spawns it)
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION_TOKEN = "upgrade-browser-harness"
MAX_UPLOAD_BYTES = 104857600  # 100 MiB, same order as production default
CSRF_TOKEN = "mock-csrf-token"

# Only the tokens api/routes.py substitutes at request time. Globals such as
# window.__HERMES_CONFIG__ also look like tokens but must be left alone.
TEMPLATE_TOKENS = ("__WEBUI_VERSION__", "__MAX_UPLOAD_BYTES__", "__CSRF_TOKEN_JSON__")


def _now() -> float:
    return round(time.time(), 3)


class State:
    """Mutable mock backend state shared by request handler threads."""

    def __init__(self, fixture_dir: str, seed: bool = True):
        self.lock = threading.Lock()
        self.fixture_dir = fixture_dir
        self.started_at = _now()
        self.settings = self._fixture("settings_default.json", {}) or {}
        self.stream_fixture = "stream_basic.json"
        self.sessions = {}
        self.streams = {}
        self.stream_seq = 0
        self.session_seq = 0
        self.requests = []          # [{t, method, path, status}]
        self.sse_connections = []   # [{t, path, stream_id}]
        self.settings_posts = []    # [payload]
        self.chat_starts = []       # [payload]
        self.unknown = []           # [path] first-seen unknown /api paths
        self.errors = []            # [str]
        if seed:
            self._seed_session()

    # -- fixtures ---------------------------------------------------------
    def _fixture(self, name: str, default=None):
        path = os.path.join(self.fixture_dir, name)
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _seed_session(self) -> None:
        data = self._fixture("session_basic.json", None)
        if not data:
            return
        sid = data["session"]["session_id"]
        self.sessions[sid] = {
            "session": dict(data["session"]),
            "messages": list(data.get("messages") or []),
        }
        self.settings.setdefault("last_session_id", sid)

    # -- helpers ----------------------------------------------------------
    def record(self, method: str, path: str, status: int) -> None:
        with self.lock:
            self.requests.append({"t": _now(), "method": method, "path": path, "status": status})

    def note_unknown(self, path: str) -> None:
        with self.lock:
            if path not in self.unknown:
                self.unknown.append(path)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "started_at": self.started_at,
                "settings": json.loads(json.dumps(self.settings)),
                "stream_fixture": self.stream_fixture,
                "sessions": {sid: len(v["messages"]) for sid, v in self.sessions.items()},
                "requests": list(self.requests),
                "sse_connections": list(self.sse_connections),
                "settings_posts": json.loads(json.dumps(self.settings_posts)),
                "chat_starts": json.loads(json.dumps(self.chat_starts)),
                "unknown_paths": list(self.unknown),
                "errors": list(self.errors),
            }

    def reset_counters(self) -> None:
        with self.lock:
            self.requests = []
            self.sse_connections = []
            self.settings_posts = []
            self.chat_starts = []
            self.unknown = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # no chunking needed; SSE streams until close
    server_version = "UpgradeBrowserMock/1.0"

    # -- wiring -----------------------------------------------------------
    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    @property
    def app_root(self) -> str:
        return self.server.app_root  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002 - silence stderr spam
        pass

    # -- primitives -------------------------------------------------------
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _text(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}
        return parsed if isinstance(parsed, dict) else {"_value": parsed}

    # -- index.html -------------------------------------------------------
    def _index_html(self) -> str:
        path = os.path.join(self.app_root, "static", "index.html")
        with open(path, "r", encoding="utf-8") as fh:
            html = fh.read()
        # Mirror the three substitutions api/routes.py performs at request time.
        html = html.replace("__WEBUI_VERSION__", VERSION_TOKEN)
        html = html.replace("__MAX_UPLOAD_BYTES__", str(MAX_UPLOAD_BYTES))
        html = html.replace("__CSRF_TOKEN_JSON__", json.dumps(CSRF_TOKEN))
        leftover = [tok for tok in TEMPLATE_TOKENS if tok in html]
        if leftover:
            # A new template token means the harness would serve a broken shell.
            raise RuntimeError(f"unsubstituted index.html tokens: {leftover}")
        return html

    # -- static -----------------------------------------------------------
    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        target = os.path.normpath(os.path.join(self.app_root, "static", rel))
        static_root = os.path.normpath(os.path.join(self.app_root, "static"))
        if not target.startswith(static_root + os.sep) and target != static_root:
            self._text("forbidden", 403)
            return
        if not os.path.isfile(target):
            self._text("not found", 404, "text/plain")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif target.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif target.endswith(".svg"):
            ctype = "image/svg+xml"
        elif target.endswith(".json"):
            ctype = "application/json; charset=utf-8"
        with open(target, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- SSE --------------------------------------------------------------
    def _sse_start(self, path: str, stream_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with self.state.lock:
            self.state.sse_connections.append({"t": _now(), "path": path, "stream_id": stream_id})

    def _sse_send(self, event: str, data) -> bool:
        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        chunk = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _stream_script(self) -> list:
        with self.state.lock:
            name = self.state.stream_fixture
        script = self.state._fixture(name, []) or []
        if isinstance(script, dict):
            script = script.get("events") or []
        return script

    def _handle_chat_stream(self, qs: dict) -> None:
        stream_id = (qs.get("stream_id") or [""])[0]
        with self.state.lock:
            stream = self.state.streams.get(stream_id)
        self._sse_start("/api/chat/stream", stream_id)
        if not stream:
            self._sse_send("error", {"error": "unknown stream"})
            return
        sid = stream["session_id"]
        script = self._stream_script()
        cancelled = False
        for step in script:
            delay = float(step.get("delay_ms", 60)) / 1000.0
            if delay:
                time.sleep(delay)
            with self.state.lock:
                cancelled = bool(self.state.streams.get(stream_id, {}).get("cancelled"))
            if cancelled:
                break
            event = step["event"]
            data = step.get("data")
            if event == "done":
                data = self._build_done(sid, data or {})
            if not self._sse_send(event, data):
                return
        with self.state.lock:
            if stream_id in self.state.streams:
                self.state.streams[stream_id]["finished"] = True
        if cancelled:
            self._sse_send("stream_end", {"reason": "cancelled"})

    def _build_done(self, sid: str, extra: dict) -> dict:
        with self.state.lock:
            entry = self.state.sessions.get(sid)
            # Fixture bookkeeping only: the scripted turn streams tokens but the
            # persisted transcript is what the real backend would store, so the
            # final assistant text comes from the fixture step, not from any
            # rendering logic here.
            final_text = extra.get("final_text")
            if final_text is not None and entry is not None:
                for msg in reversed(entry["messages"]):
                    if msg.get("role") == "assistant":
                        msg["content"] = final_text
                        break
            session = dict(entry["session"]) if entry else {"session_id": sid}
            messages = list(entry["messages"]) if entry else []
            session["messages"] = messages
            session["message_count"] = len(messages)
            session["updated_at"] = _now()
        payload = {"status": "completed", "session": session, "usage": {"input_tokens": 1200, "output_tokens": 340}}
        payload.update({k: v for k, v in extra.items() if k != "session"})
        return payload

    # -- routing ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path.startswith("/__mock/"):
                return self._mock_get(path, qs)
            if path in ("/", "/index.html") or path.startswith("/session/"):
                self._text(self._index_html())
                return self.state.record("GET", path, 200)
            if path == "/sw.js":
                # Keep the service worker out of the picture: a stub that never
                # caches anything, so reloads always hit this server.
                self._text(
                    "self.addEventListener('install',()=>self.skipWaiting());"
                    "self.addEventListener('activate',(e)=>e.waitUntil(self.registration.unregister()));",
                    200,
                    "application/javascript; charset=utf-8",
                )
                return self.state.record("GET", path, 200)
            if path == "/health":
                return self._json({"ok": True})
            if path.startswith("/static/"):
                self._serve_static(path)
                return self.state.record("GET", path, 200)
            if path.startswith("/api/"):
                return self._api_get(path, qs)
            if path == "/favicon.ico":
                self._text("", 204)
                return
            self._text("not found", 404, "text/plain")
            return self.state.record("GET", path, 404)
        except Exception:  # pragma: no cover - defensive
            with self.state.lock:
                self.state.errors.append(traceback.format_exc()[-2000:])
            try:
                self._json({"error": "mock server error"}, 500)
            except Exception:
                pass

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/__mock/"):
                return self._mock_post(path)
            body = self._read_json()
            if path == "/api/settings":
                return self._api_settings_post(body)
            if path == "/api/chat/start":
                return self._api_chat_start(body)
            if path == "/api/session/new":
                return self._json({"session": self._new_session(body=body)})
            if path == "/api/session/update":
                sid = str(body.get("session_id") or "")
                with self.state.lock:
                    entry = self.state.sessions.get(sid)
                    if entry is not None:
                        for key, value in body.items():
                            if key in ("session_id", "messages"):
                                continue
                            entry["session"][key] = value
                        entry["session"]["updated_at"] = _now()
                return self._json({"ok": True, "session": (entry or {}).get("session", {})})
            if path in ("/api/session/delete", "/api/session/draft", "/api/session/archive"):
                sid = str(body.get("session_id") or "")
                if path == "/api/session/delete":
                    with self.state.lock:
                        self.state.sessions.pop(sid, None)
                return self._json({"ok": True})
            if path == "/api/chat/cancel":
                sid = (body.get("stream_id") or "")
                with self.state.lock:
                    for entry in self.state.streams.values():
                        if not sid or entry.get("stream_id") == sid:
                            entry["cancelled"] = True
                return self._json({"cancelled": True})
            if path.startswith("/api/"):
                self.state.note_unknown(path)
                return self._json({})
            self._text("not found", 404, "text/plain")
            return self.state.record("POST", path, 404)
        except Exception:  # pragma: no cover - defensive
            with self.state.lock:
                self.state.errors.append(traceback.format_exc()[-2000:])
            try:
                self._json({"error": "mock server error"}, 500)
            except Exception:
                pass

    # -- /__mock ----------------------------------------------------------
    def _mock_get(self, path: str, qs: dict) -> None:
        if path == "/__mock/state":
            return self._json(self.state.snapshot())
        if path == "/__mock/health":
            return self._json({"ok": True, "app_root": self.app_root})
        if path == "/__mock/fixture":
            name = (qs.get("name") or [""])[0]
            return self._json(self.state._fixture(name, None))
        self._json({"error": "unknown mock endpoint"}, 404)

    def _mock_post(self, path: str) -> None:
        body = self._read_json()
        if path == "/__mock/reset":
            self.state.reset_counters()
            return self._json({"ok": True})
        if path == "/__mock/config":
            with self.state.lock:
                if "stream_fixture" in body:
                    self.state.stream_fixture = str(body["stream_fixture"])
                if isinstance(body.get("settings"), dict):
                    self.state.settings.update(body["settings"])
                if body.get("reset_session"):
                    self.state.sessions = {}
                    self.state._seed_session()
                # ``seed: false`` gives a scenario a completely empty backend
                # (fresh chat), ``seed: true`` restores the settled transcript.
                if "seed" in body:
                    self.state.sessions = {}
                    self.state.streams = {}
                    if body["seed"]:
                        self.state._seed_session()
            return self._json({"ok": True, "stream_fixture": self.state.stream_fixture})
        if path == "/__mock/session":
            # Install/replace a session (used to load the long-artifacts transcript).
            sid = body.get("session_id")
            if not sid:
                return self._json({"error": "session_id required"}, 400)
            with self.state.lock:
                self.state.sessions[sid] = {
                    "session": body.get("session") or {"session_id": sid},
                    "messages": body.get("messages") or [],
                }
            return self._json({"ok": True})
        self._json({"error": "unknown mock endpoint"}, 404)

    # -- API GET ----------------------------------------------------------
    def _api_get(self, path: str, qs: dict) -> None:
        st = self.state
        if path in ("/api/auth/status", "/api/v1/auth/status"):
            return self._json({
                "authenticated": True, "auth_required": False, "password_set": False,
                "login_required": False, "mode": "open",
            })
        if path == "/api/settings":
            with st.lock:
                payload = json.loads(json.dumps(st.settings))
            return self._json(payload)
        if path == "/api/sessions":
            with st.lock:
                sessions = []
                for entry in st.sessions.values():
                    meta = {k: v for k, v in entry["session"].items() if k != "messages"}
                    meta.setdefault("message_count", len(entry["messages"]))
                    sessions.append(meta)
            return self._json({"sessions": sessions})
        if path == "/api/session":
            sid = (qs.get("session_id") or [""])[0]
            with st.lock:
                entry = st.sessions.get(sid)
                if not entry:
                    return self._json({"error": "not found"}, 404)
                session = json.loads(json.dumps(entry["session"]))
                session["messages"] = json.loads(json.dumps(entry["messages"]))
                session["message_count"] = len(entry["messages"])
                # A live turn must stay visible to a mid-stream reload, exactly
                # like the real server: the client derives busy/reattach state
                # from active_stream_id.
                active = [s["stream_id"] for s in st.streams.values()
                          if s.get("session_id") == sid and not s.get("finished") and not s.get("cancelled")]
                if active:
                    session["active_stream_id"] = active[-1]
            return self._json({"session": session})
        if path == "/api/models":
            with st.lock:
                model = st.settings.get("model") or "mock-model"
                provider = st.settings.get("model_provider") or "mock"
            return self._json({
                "groups": [{
                    "provider": provider, "provider_id": provider,
                    "models": [{"id": model, "label": model, "provider": provider}],
                    "extra_models": [],
                }],
                "models": [{"id": model, "label": model, "provider": provider}],
                "default_model": model, "default_provider": provider,
            })
        if path == "/api/profiles":
            return self._json({"profiles": [{"name": "default", "label": "default", "active": True}]})
        if path == "/api/profile/active":
            return self._json({"name": "default", "profile": "default", "label": "default"})
        if path == "/api/workspaces":
            return self._json({"workspaces": [{"path": "/tmp/mock-workspace", "name": "mock-workspace"}]})
        if path == "/api/chat/stream/status":
            stream_id = (qs.get("stream_id") or [""])[0]
            with st.lock:
                entry = st.streams.get(stream_id) or {}
                active = bool(entry) and not entry.get("finished") and not entry.get("cancelled")
            return self._json({"active": active, "stream_id": stream_id, "status": "running" if active else "done"})
        if path == "/api/chat/stream":
            return self._handle_chat_stream(qs)
        if path == "/api/chat/cancel":
            stream_id = (qs.get("stream_id") or [""])[0]
            with st.lock:
                for entry in st.streams.values():
                    if not stream_id or entry.get("stream_id") == stream_id:
                        entry["cancelled"] = True
            return self._json({"cancelled": True})
        if path in ("/api/gateway/events", "/api/session/stream", "/api/sessions/events",
                    "/api/sessions/gateway/stream"):
            # Long-lived "nothing happened" channel: keeps the client's watcher
            # quiet without inventing events.
            self._sse_start(path, "")
            for _ in range(120):
                time.sleep(0.5)
            return
        if path == "/api/onboarding/status":
            with st.lock:
                settings = json.loads(json.dumps(st.settings))
            return self._json({
                "completed": True, "needs_onboarding": False, "status": "complete",
                "setup": {},
                "workspaces": {"last": settings.get("default_workspace") or "/tmp/mock-workspace"},
                "settings": settings,
            })
        if path == "/api/projects":
            return self._json({"projects": []})
        if path == "/api/reasoning":
            return self._json({"display": "auto", "effort": None})
        if path in ("/api/dashboard/config", "/api/dashboard/status", "/api/updates/check"):
            return self._json({})
        if path == "/api/transcribe/capability":
            return self._json({"available": False, "server_stt": False})
        if path == "/api/list":
            return self._json({"entries": [], "path": (qs.get("path") or ["."])[0]})
        if path == "/api/git-info":
            return self._json({"is_repo": False, "branch": None})
        if path == "/api/session/compress/status":
            return self._json({"active": False, "phase": None})
        if path in ("/api/approval/pending", "/api/clarify/pending"):
            return self._json({"pending": None})
        if path == "/api/session/yolo":
            return self._json({"enabled": False})
        if path == "/api/version":
            return self._json({"version": VERSION_TOKEN, "webui_version": VERSION_TOKEN})
        st.note_unknown(path)
        return self._json({})

    # -- API POST ---------------------------------------------------------
    def _new_session(self, sid: str = "", body: dict | None = None) -> dict:
        """Create an empty session the way POST /api/session/new does."""
        st = self.state
        body = body or {}
        with st.lock:
            st.session_seq += 1
            sid = sid or f"mock-session-{st.session_seq}"
            session = {
                "session_id": sid,
                "title": body.get("title") or "New chat",
                "model": body.get("model") or st.settings.get("model", "mock-model"),
                "model_provider": body.get("model_provider") or st.settings.get("model_provider", "mock"),
                "workspace": body.get("workspace") or "/tmp/mock-workspace",
                "profile": body.get("profile") or "default",
                "created_at": _now(),
                "updated_at": _now(),
                "message_count": 0,
                "messages": [],
            }
            st.sessions[sid] = {"session": session, "messages": []}
            return json.loads(json.dumps(session))

    def _api_settings_post(self, body: dict) -> None:
        with self.state.lock:
            self.state.settings_posts.append(body)
            self.state.settings.update(body)
            payload = json.loads(json.dumps(self.state.settings))
        return self._json(payload)

    def _api_chat_start(self, body: dict) -> None:
        st = self.state
        sid = body.get("session_id") or ""
        with st.lock:
            entry = st.sessions.get(sid)
            if entry is None:
                entry = {"session": {"session_id": sid, "title": "Mock chat", "model": st.settings.get("model", "mock-model"),
                                     "model_provider": st.settings.get("model_provider", "mock"),
                                     "workspace": "/tmp/mock-workspace", "profile": "default",
                                     "created_at": _now(), "updated_at": _now()},
                         "messages": []}
                st.sessions[sid] = entry
            message = body.get("message") or ""
            if message:
                entry["messages"].append({"id": f"u-{len(entry['messages'])}", "role": "user",
                                          "content": message, "created_at": _now()})
            st.stream_seq += 1
            stream_id = f"mock-stream-{st.stream_seq}"
            st.streams[stream_id] = {"stream_id": stream_id, "session_id": sid,
                                     "finished": False, "cancelled": False}
            st.chat_starts.append({"t": _now(), "stream_id": stream_id, "session_id": sid,
                                   "message": message})
            assistant = {"id": f"a-{len(entry['messages'])}", "role": "assistant", "content": "",
                         "created_at": _now()}
            entry["messages"].append(assistant)
        return self._json({"stream_id": stream_id, "session_id": sid, "status": "started",
                           "pending_started_at": _now()})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--app-root", default=os.environ.get("UPGRADE_APP_ROOT", os.getcwd()))
    ap.add_argument("--fixture-dir", default=os.environ.get(
        "UPGRADE_FIXTURE_DIR",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "upgrade_chat"),
    ))
    ap.add_argument("--port", type=int, default=int(os.environ.get("UPGRADE_PORT", "0")))
    ap.add_argument("--state-out", default=os.environ.get("UPGRADE_STATE_OUT", ""))
    ap.add_argument("--no-seed", action="store_true",
                    help="start with no sessions at all (fresh-chat scenarios)")
    args = ap.parse_args(argv)

    app_root = os.path.abspath(args.app_root)
    if not os.path.isfile(os.path.join(app_root, "static", "index.html")):
        print(f"SETUP FAIL: no static/index.html under {app_root}", file=sys.stderr)
        return 2
    if not os.path.isdir(args.fixture_dir):
        print(f"SETUP FAIL: fixture dir missing: {args.fixture_dir}", file=sys.stderr)
        return 2

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    httpd.state = State(args.fixture_dir, seed=not args.no_seed)          # type: ignore[attr-defined]
    httpd.app_root = app_root                      # type: ignore[attr-defined]
    port = httpd.server_address[1]
    if args.state_out:
        with open(args.state_out, "w", encoding="utf-8") as fh:
            json.dump({"port": port, "app_root": app_root, "pid": os.getpid()}, fh)
    print(f"PORT {port}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
