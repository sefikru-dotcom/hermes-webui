"""Versioned native chat transport boundary.

This module deliberately does not reproduce the legacy model/stream machinery.
Until a principal/profile-aware delegation contract exists, authenticated native
requests receive an explicit 501 rather than a fabricated run.
"""
from __future__ import annotations

import json
from urllib.parse import unquote

from api.device_pairings import extract_bearer, verify_native_credential

_PREFIX = "/api/v1/chat/runs"
_REQUIRED_SCOPES = {"chat:read", "chat:write"}


def _headers(handler, name: str) -> list[str]:
    getter = getattr(handler.headers, "get_all", None)
    if getter:
        return list(getter(name, []))
    value = handler.headers.get(name)
    return [value] if value else []


def _json(handler, payload: dict, status: int) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else {}


def _run_identifier(path: str, method: str = "GET") -> tuple[str, str] | None:
    if method == "POST" and path == _PREFIX:
        return ("", "create")
    suffix = path[len(_PREFIX):]
    if not suffix.startswith("/"):
        return None
    parts = [unquote(p) for p in suffix.split("/") if p]
    allowed = {"status", "events", "cancel", "steer"}
    if len(parts) == 1:
        return (parts[0], "status")
    if len(parts) == 2 and parts[1] in allowed - {"status"}:
        return (parts[0], parts[1])
    return None


def _authorized(handler, method: str) -> bool:
    token = extract_bearer(_headers(handler, "Authorization"))
    principal = verify_native_credential(token)
    if not principal:
        _json(handler, {"error": "Authentication required"}, 401)
        return False
    scopes = set(principal.get("scopes") or principal.get("scope") or ())
    needed = {"chat:read"} if method == "GET" else {"chat:write"}
    if not needed.issubset(scopes):
        _json(handler, {"error": "Native chat scope required"}, 403)
        return False
    handler._native_chat_principal = principal
    return True


def dispatch(handler, parsed, method: str) -> bool:
    """Handle only the versioned native chat paths; return False otherwise."""
    path = parsed.path
    if path != _PREFIX and not path.startswith(_PREFIX + "/"):
        return False
    method = method.upper()
    if method not in {"GET", "POST"} or not _authorized(handler, method):
        if method not in {"GET", "POST"}:
            _json(handler, {"error": "Method not allowed"}, 405)
        return True
    if method == "POST" and path == _PREFIX:
        try:
            body = _read_body(handler)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            _json(handler, {"error": "Invalid JSON"}, 400)
            return True
        if set(body) != {"session_id", "message", "client_request_id"} or not all(
            isinstance(body.get(key), str) and body[key].strip()
            for key in ("session_id", "message", "client_request_id")
        ):
            _json(handler, {"error": "session_id, message, and client_request_id are required"}, 400)
            return True
    elif method == "POST":
        try:
            _read_body(handler)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            _json(handler, {"error": "Invalid JSON"}, 400)
            return True
    if _run_identifier(path, method) is None:
        _json(handler, {"error": "Not found"}, 404)
        return True
    _json(handler, {
        "error": "native_chat_unavailable",
        "detail": "Legacy chat integration is not enabled for the versioned native adapter",
    }, 501)
    return True
