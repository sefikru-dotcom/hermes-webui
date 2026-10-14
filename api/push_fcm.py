"""FCM (Firebase Cloud Messaging) HTTP v1 delivery for the Hermes WebUI.

Server-side twin of ``api/push.py`` (Web Push / VAPID) for NATIVE clients
(Android app, TV box) that register a Firebase device token instead of a
browser PushSubscription. Both channels coexist: the turn-complete hook in
``api/streaming.py`` fires Web Push first, then FCM; neither may break the
other or the agent turn.

Design notes:

* Token registry lives in ``STATE_DIR/push_fcm.db`` (sqlite), following the
  exact connection pattern of ``api/device_pairings.py`` — one short-lived
  connection per operation, WAL, commit/rollback in a contextmanager. The
  WebUI runs one process per isolated profile with its own STATE_DIR, so
  every space keeps its own device registry.
* Delivery is FCM HTTP v1: ``POST
  https://fcm.googleapis.com/v1/projects/<project_id>/messages:send`` with an
  OAuth2 access token minted from a service-account key. The key path comes
  from ``HERMES_WEBUI_FCM_SERVICE_ACCOUNT`` (default
  ``/root/.secrets/fcm-mars-6b68f.json``); its CONTENTS are never logged,
  returned by any endpoint, or committed.
* The Google access token is cached in-process until ~5 minutes before
  expiry (``_ACCESS_TOKEN_SKEW_SECONDS``); a 401/403 from FCM forces one
  refresh+retry per send_fcm call.
* Dead tokens (FCM answers 404/UNREGISTERED, or 400/INVALID_ARGUMENT whose
  message points at the registration token) are pruned from the registry so
  uninstalled apps stop generating traffic.
* ``send_fcm`` NEVER raises and is a silent no-op when the service key or
  the ``google``/``requests`` stack is unavailable — same contract as
  ``send_push``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

#: sqlite registry of FCM device tokens (raw tokens are required for sending,
#: so — unlike device-pairing credentials — they are stored as-is, in a
#: 0600-state-dir database next to native_auth.db).
FCM_DB_PATH = STATE_DIR / "push_fcm.db"

FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

#: Default location of the mars-6b68f service-account key (0600, outside git).
#: Override with HERMES_WEBUI_FCM_SERVICE_ACCOUNT.
_DEFAULT_FCM_KEY_PATH = "/root/.secrets/fcm-mars-6b68f.json"

#: Refresh the Google access token when less than this many seconds remain.
_ACCESS_TOKEN_SKEW_SECONDS = 300

_VALID_PLATFORMS = frozenset({"android", "ios", "web"})
_MAX_TOKEN_LEN = 4096
_MAX_DEVICE_NAME_LEN = 120

_LOCK = threading.RLock()
_TOKEN_LOCK = threading.Lock()
#: In-process Google OAuth2 access-token cache: {"token", "expiry", "project_id"}.
_ACCESS_CACHE: dict[str, Any] = {"token": None, "expiry": 0.0, "project_id": None}


# ── sqlite registry (device_pairings.py pattern) ────────────────────────────

def _connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(FCM_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS fcm_tokens (
        token TEXT PRIMARY KEY,
        device_id TEXT NOT NULL DEFAULT '',
        platform TEXT NOT NULL DEFAULT 'android',
        device_name TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_sent_at REAL,
        sent_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fcm_tokens_device ON fcm_tokens(device_id)"
    )
    return conn


@contextmanager
def _connection():
    """Yield a transaction and always close the SQLite file descriptor."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_registry() -> None:
    """Drop every stored token (test helper / manual reset)."""
    with _LOCK, _connection() as conn:
        conn.execute("DELETE FROM fcm_tokens")


def register_token(payload: dict[str, Any], device_id: str = "") -> dict[str, Any]:
    """Store (upsert) one FCM registration token.

    ``payload`` keys: ``token`` (required), ``platform`` (android|ios|web,
    default android), ``device_name`` (optional label). ``device_id`` is the
    native-auth device bound to the request credential — never client-
    supplied. When a known device re-registers with a NEW token (Firebase
    rotates them), its previous tokens are dropped: one device = one live
    token, so a rotation can never leave a zombie entry behind.
    """
    token = str((payload or {}).get("token") or "").strip()
    if not token:
        raise ValueError("token is required")
    if len(token) > _MAX_TOKEN_LEN:
        raise ValueError("token is too long")
    platform = str((payload or {}).get("platform") or "android").strip().lower()
    if platform not in _VALID_PLATFORMS:
        raise ValueError("platform must be one of: android, ios, web")
    device_name = str((payload or {}).get("device_name") or "").strip()[:_MAX_DEVICE_NAME_LEN]
    device_id = str(device_id or "").strip()[:64]
    now = time.time()
    with _LOCK, _connection() as conn:
        if device_id:
            conn.execute(
                "DELETE FROM fcm_tokens WHERE device_id = ? AND token != ?",
                (device_id, token),
            )
        conn.execute(
            """INSERT INTO fcm_tokens
                   (token, device_id, platform, device_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(token) DO UPDATE SET
                   device_id = excluded.device_id,
                   platform = excluded.platform,
                   device_name = excluded.device_name,
                   updated_at = excluded.updated_at""",
            (token, device_id, platform, device_name, now, now),
        )
        total = conn.execute("SELECT COUNT(*) AS n FROM fcm_tokens").fetchone()["n"]
    return {
        "ok": True,
        "registered": True,
        "platform": platform,
        "device_id": device_id,
        "total": int(total),
    }


def unregister_token(payload: dict[str, Any] | str, device_id: str = "") -> dict[str, Any]:
    """Remove a token (or every token of one device) from the registry."""
    token = payload if isinstance(payload, str) else str((payload or {}).get("token") or "")
    token = token.strip()
    device_id = str(device_id or "").strip()
    if not token and not device_id:
        raise ValueError("token is required")
    with _LOCK, _connection() as conn:
        if token:
            cur = conn.execute("DELETE FROM fcm_tokens WHERE token = ?", (token,))
        else:
            cur = conn.execute("DELETE FROM fcm_tokens WHERE device_id = ?", (device_id,))
        removed = cur.rowcount or 0
        total = conn.execute("SELECT COUNT(*) AS n FROM fcm_tokens").fetchone()["n"]
    return {"ok": True, "removed": int(removed), "total": int(total)}


def token_count() -> int:
    try:
        with _connection() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM fcm_tokens").fetchone()["n"])
    except Exception:
        return 0


def _list_tokens() -> list[dict[str, Any]]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT token, device_id, platform, device_name FROM fcm_tokens"
        ).fetchall()
    return [dict(row) for row in rows]


def _mark_sent(token: str) -> None:
    try:
        with _LOCK, _connection() as conn:
            conn.execute(
                "UPDATE fcm_tokens SET last_sent_at = ?, sent_count = sent_count + 1,"
                " last_error = '' WHERE token = ?",
                (time.time(), token),
            )
    except Exception:  # bookkeeping must never break delivery
        logger.debug("fcm: sent-mark failed", exc_info=True)


def _mark_error(token: str, error: str) -> None:
    try:
        with _LOCK, _connection() as conn:
            conn.execute(
                "UPDATE fcm_tokens SET last_error = ? WHERE token = ?",
                (str(error)[:300], token),
            )
    except Exception:
        logger.debug("fcm: error-mark failed", exc_info=True)


def _prune_token(token: str, reason: str) -> None:
    try:
        with _LOCK, _connection() as conn:
            conn.execute("DELETE FROM fcm_tokens WHERE token = ?", (token,))
        logger.info("fcm: pruned dead token (%s)", reason)
    except Exception:
        logger.debug("fcm: prune failed", exc_info=True)


# ── service key + Google OAuth2 access token ────────────────────────────────

def _service_key_path() -> str | None:
    """Resolve the FCM service-account key path, or None when unavailable.

    Never logs or returns the key CONTENTS — only the path.
    """
    path = str(
        os.getenv("HERMES_WEBUI_FCM_SERVICE_ACCOUNT", "")
        or os.getenv("HERMES_WEBUI_FCM_KEY", "")
        or _DEFAULT_FCM_KEY_PATH
    ).strip()
    if path and os.path.isfile(path):
        return path
    return None


def _fetch_access_token() -> tuple[str, float, str]:
    """Mint a fresh Google access token from the service-account key.

    Returns ``(token, expiry_unix_ts, project_id)``. Kept as a separate seam
    so unit tests can exercise the cache without the google-auth stack or
    any network.
    """
    key_path = _service_key_path()
    if not key_path:
        raise RuntimeError(
            "FCM service key not found (set HERMES_WEBUI_FCM_SERVICE_ACCOUNT)"
        )
    from google.oauth2 import service_account  # noqa: PLC0415 — optional dep
    import google.auth.transport.requests  # noqa: PLC0415 — optional dep

    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=[FCM_SCOPE]
    )
    creds.refresh(google.auth.transport.requests.Request())
    token = str(getattr(creds, "token", "") or "")
    if not token:
        raise RuntimeError("FCM credential refresh returned no token")
    expiry = getattr(creds, "expiry", None)
    try:
        expiry_ts = float(expiry.timestamp()) if expiry else time.time() + 3600.0
    except Exception:
        expiry_ts = time.time() + 3600.0
    project_id = str(getattr(creds, "project_id", "") or "")
    if not project_id:
        raise RuntimeError("FCM service key carries no project_id")
    return token, expiry_ts, project_id


def _access_token(force_refresh: bool = False) -> tuple[str, str]:
    """Cached access token + project id; refreshes near expiry.

    Raises RuntimeError when the key/stack is unavailable — callers translate
    that into a silent no-op result.
    """
    with _TOKEN_LOCK:
        now = time.time()
        cached_token = _ACCESS_CACHE.get("token")
        if (
            not force_refresh
            and cached_token
            and float(_ACCESS_CACHE.get("expiry") or 0.0) - now > _ACCESS_TOKEN_SKEW_SECONDS
        ):
            return str(cached_token), str(_ACCESS_CACHE.get("project_id") or "")
        token, expiry_ts, project_id = _fetch_access_token()
        _ACCESS_CACHE.update({"token": token, "expiry": expiry_ts, "project_id": project_id})
        return token, project_id


def _reset_access_cache() -> None:
    """Clear the in-process token cache (test helper)."""
    with _TOKEN_LOCK:
        _ACCESS_CACHE.update({"token": None, "expiry": 0.0, "project_id": None})


def supported() -> tuple[bool, str, str]:
    """(ok, reason, project_id) without performing any network call.

    ``project_id`` is resolved from the key file metadata only when the cache
    already knows it; otherwise it stays empty — status must never trigger a
    token mint (and thus a network call) on its own.
    """
    key_path = _service_key_path()
    if not key_path:
        return False, "FCM service key not found", ""
    try:
        import requests  # noqa: F401
    except Exception as exc:
        return False, f"requests unavailable: {exc}", ""
    return True, "", str(_ACCESS_CACHE.get("project_id") or "")


# ── payload + delivery ──────────────────────────────────────────────────────

def _build_message(
    token: str,
    title: str,
    body: str = "",
    url: str | None = None,
    tag: str | None = None,
    *,
    urgent: bool = False,
) -> dict[str, Any]:
    """Compose one FCM HTTP v1 ``messages:send`` request body.

    ``notification`` renders the system tray entry on Android/iOS even with
    the app process dead; ``data`` carries the deep link for the native
    client's tap handler (FCM requires all data values to be strings).
    """
    data: dict[str, str] = {"ts": str(int(time.time()))}
    if url:
        data["url"] = str(url)
    if tag:
        data["tag"] = str(tag)
    message: dict[str, Any] = {
        "token": token,
        "notification": {"title": title or "Марс", "body": body or ""},
        "data": data,
        "android": {
            "priority": "high" if urgent else "normal",
            "notification": {
                "channel_id": "hermes_default",
                **({"tag": str(tag)} if tag else {}),
            },
        },
    }
    if url:
        # Webpush twin of the deep link: a PWA installed via FCM-web tokens
        # opens this URL on click (same field the Web Push channel uses).
        message["webpush"] = {"fcm_options": {"link": str(url)}}
    return {"message": message}


def _is_dead_token(status_code: int, error_payload: dict[str, Any]) -> bool:
    """True when FCM says this registration token will never work again.

    * 404 / NOT_FOUND with reason UNREGISTERED — app uninstalled or token
      wiped: always prune.
    * 400 / INVALID_ARGUMENT — prune ONLY when the message points at the
      registration token itself ("...not a valid FCM registration token",
      "Requested entity was not found"). A generic INVALID_ARGUMENT (bad
      payload shape) must not wipe the whole registry.
    """
    error = (error_payload or {}).get("error") or {}
    code = str(error.get("code") or "").upper()
    details = error.get("details") or []
    reasons = {
        str(detail.get("reason") or "").upper()
        for detail in details
        if isinstance(detail, dict)
    }
    message = str(error.get("message") or "").lower()
    if status_code == 404 and (
        code in {"NOT_FOUND", "UNREGISTERED"}
        or "UNREGISTERED" in reasons
        or "requested entity was not found" in message
    ):
        return True
    if status_code == 400 and (
        code == "INVALID_ARGUMENT" or "INVALID_ARGUMENT" in reasons
    ) and "token" in message:
        return True
    return False


def _http_post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int):
    """Single POST seam — unit tests replace this, never the real network."""
    import requests  # noqa: PLC0415 — optional dep

    return requests.post(url, json=payload, headers=headers, timeout=timeout)


def send_fcm(
    title: str,
    body: str = "",
    url: str | None = None,
    tag: str | None = None,
    *,
    urgent: bool = False,
) -> dict[str, Any]:
    """Deliver a notification to every registered FCM token.

    Returns ``{'sent': n, 'pruned': m, 'total': k, 'errors': [...]}`` — the
    same shape ``send_push`` returns, so the streaming hook logs both
    channels identically. NEVER raises: a dead FCM backend, a missing
    service key or an unavailable google-auth stack degrade to a silent
    no-op (total=0) exactly like an unconfigured Web Push channel.
    """
    result: dict[str, Any] = {"sent": 0, "pruned": 0, "total": 0, "errors": []}
    try:
        tokens = _list_tokens()
    except Exception as exc:
        logger.debug("fcm: registry unreadable: %s", exc)
        result["errors"].append(f"registry unreadable: {exc}")
        return result
    result["total"] = len(tokens)
    if not tokens:
        return result

    ok, reason, _project = supported()
    if not ok:
        # Silent no-op: debug-level only, the hook must not spam turn logs.
        logger.debug("fcm: unavailable (%s)", reason)
        result["total"] = 0
        result["errors"].append(f"FCM unavailable: {reason}")
        return result

    try:
        access_token, project_id = _access_token()
    except Exception as exc:
        logger.debug("fcm: access token failed: %s", exc)
        result["total"] = 0
        result["errors"].append(f"FCM access token failed: {exc}")
        return result

    endpoint = FCM_SEND_URL.format(project_id=project_id)
    auth_retried = False
    for row in tokens:
        token = str(row.get("token") or "")
        if not token:
            continue
        payload = _build_message(token, title, body, url, tag, urgent=urgent)
        try:
            response = _http_post(
                endpoint,
                payload,
                {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                10,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in (401, 403) and not auth_retried:
                # Cached token may have been revoked/expired early: one
                # forced refresh + retry for THIS message, then continue.
                auth_retried = True
                try:
                    access_token, project_id = _access_token(force_refresh=True)
                    endpoint = FCM_SEND_URL.format(project_id=project_id)
                    response = _http_post(
                        endpoint,
                        payload,
                        {
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json",
                        },
                        10,
                    )
                    status_code = int(getattr(response, "status_code", 0) or 0)
                except Exception as exc:
                    result["errors"].append(f"token refresh failed: {exc}")
            if status_code == 200:
                result["sent"] += 1
                _mark_sent(token)
                continue
            try:
                error_payload = response.json()
            except Exception:
                error_payload = {}
            if _is_dead_token(status_code, error_payload):
                result["pruned"] += 1
                _prune_token(token, f"HTTP {status_code}")
                continue
            detail = str(((error_payload or {}).get("error") or {}).get("message") or "")[:200]
            err = f"HTTP {status_code}: {detail}".strip()
            result["errors"].append(err)
            _mark_error(token, err)
            logger.warning("fcm: delivery failed for one token: %s", err)
        except Exception as exc:
            result["errors"].append(f"transport error: {exc}")
            logger.warning("fcm: transport error: %s", exc)

    result["total"] = token_count()
    result["errors"] = result["errors"][:10]
    return result


def status() -> dict[str, Any]:
    """Small introspection payload for the settings UI / diagnostics.

    Mirrors ``api.push.status``: never exposes secrets — only whether the
    channel is usable, how many tokens are registered and (when already
    cached) the FCM project id.
    """
    ok, reason, project_id = supported()
    by_platform: dict[str, int] = {}
    try:
        for row in _list_tokens():
            platform = str(row.get("platform") or "unknown")
            by_platform[platform] = by_platform.get(platform, 0) + 1
    except Exception:
        pass
    return {
        "supported": ok,
        "reason": reason,
        "project_id": project_id,
        "tokens": token_count(),
        "by_platform": by_platform,
    }
