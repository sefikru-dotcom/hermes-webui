"""Durable native device pairing and bearer credential state."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
import uuid
import re
from pathlib import Path

from api.config import STATE_DIR

PAIRING_TTL_SECONDS = 300
DEVICE_CREDENTIAL_TTL_SECONDS = 3600
_CODE_RETRY_LIMIT = 100
_LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()
_DB_PATH = STATE_DIR / "native_auth.db"
_BEARER_RE = re.compile(r"^Bearer ([A-Za-z0-9_-]{20,200})$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_bearer(values) -> str | None:
    """Strictly parse exactly one RFC bearer header value."""
    if isinstance(values, str):
        values = [values]
    values = list(values or [])
    if len(values) != 1:
        return None
    match = _BEARER_RE.fullmatch(values[0])
    return match.group(1) if match else None


def _connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS device_pairings (
        pair_id TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, poll_secret_hash TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL,
        scope_json TEXT, credential_hash TEXT, device_id TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS device_credentials (
        credential_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL,
        scope_json TEXT NOT NULL, device_id TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
    )""")
    return conn


def _json_load(value: str | None) -> dict:
    import json
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _json_dump(value: dict) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM device_pairings WHERE expires_at <= ?", (now,))
    conn.execute("DELETE FROM device_credentials WHERE expires_at <= ? OR revoked = 1", (now,))


def create_pairing() -> dict:
    with _LOCK, _connect() as conn:
        now = time.time()
        _prune(conn, now)
        for _ in range(_CODE_RETRY_LIMIT):
            code = f"{secrets.randbelow(1_000_000):06d}"
            if conn.execute("SELECT 1 FROM device_pairings WHERE code = ?", (code,)).fetchone() is None:
                break
        else:
            raise RuntimeError("Unable to allocate a pairing code")
        pair_id = uuid.uuid4().hex
        poll_secret = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO device_pairings VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL)",
            (pair_id, code, _digest(poll_secret), now, now + PAIRING_TTL_SECONDS),
        )
        return {"pair_id": pair_id, "code": code, "poll_secret": poll_secret,
                "expires_in": PAIRING_TTL_SECONDS, "status": "pending"}


def _pair(conn: sqlite3.Connection, pair_id: str, poll_secret: str):
    row = conn.execute("SELECT * FROM device_pairings WHERE pair_id = ?", (pair_id,)).fetchone()
    if not row:
        return None
    if poll_secret and not hmac.compare_digest(row["poll_secret_hash"], _digest(poll_secret)):
        return None
    return row


def pairing_status(pair_id: str, poll_secret: str | None = None) -> dict | None:
    """Return status metadata only; never returns or creates a credential."""
    with _LOCK, _connect() as conn:
        now = time.time()
        _prune(conn, now)
        row = _pair(conn, pair_id, poll_secret or "")
        if not row:
            return None
        if row["status"] == "pending":
            return {"pair_id": pair_id, "status": "pending", "expires_in": max(0, int(row["expires_at"] - now))}
        return {"pair_id": pair_id, "status": row["status"]}


def exchange_pairing(pair_id: str, poll_secret: str | None) -> dict | None:
    """Atomically consume an approved pairing and issue one OAuth-like bearer."""
    if not poll_secret:
        return None
    with _LOCK, _connect() as conn:
        now = time.time()
        _prune(conn, now)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _pair(conn, pair_id, poll_secret)
            if not row or row["status"] != "approved":
                conn.rollback()
                return None
            token = "hdev_" + secrets.token_urlsafe(32)
            device_id = row["device_id"] or ("dev_" + uuid.uuid4().hex)
            expires_at = now + DEVICE_CREDENTIAL_TTL_SECONDS
            scope = _json_load(row["scope_json"])
            credential_hash = _digest(token)
            conn.execute("INSERT INTO device_credentials VALUES (?, ?, ?, ?, 0)",
                         (credential_hash, expires_at, _json_dump(scope), device_id))
            updated = conn.execute(
                "UPDATE device_pairings SET status='claimed', credential_hash=?, device_id=? "
                "WHERE pair_id=? AND status='approved'", (credential_hash, device_id, pair_id))
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return {"access_token": token, "token_type": "Bearer",
                    "expires_in": DEVICE_CREDENTIAL_TTL_SECONDS, "device_id": device_id,
                    "scopes": sorted(scope)}
        except Exception:
            conn.rollback()
            raise


def approve_pairing(code: str, session_cookie: str | None = None) -> dict | None:
    """Approve only from a caller already authenticated by the HTTP layer."""
    code = str(code or "").strip()
    if len(code) != 6 or not code.isdigit() or not session_cookie:
        return None
    with _LOCK, _connect() as conn:
        now = time.time()
        _prune(conn, now)
        row = conn.execute("SELECT * FROM device_pairings WHERE code = ?", (code,)).fetchone()
        if not row or row["status"] != "pending":
            return None
        # This slice deliberately grants no privileged native scope. A future
        # browser-principal binding must resolve authorization server-side.
        approved_scope = {}
        conn.execute("UPDATE device_pairings SET status='approved', scope_json=? WHERE code=?",
                     (_json_dump(approved_scope), code))
        return {"pair_id": row["pair_id"], "status": "approved"}


def authenticate_device(credential: str | None) -> dict | None:
    if not credential:
        return None
    with _LOCK, _connect() as conn:
        now = time.time()
        _prune(conn, now)
        row = conn.execute("SELECT * FROM device_credentials WHERE credential_hash=? AND revoked=0 AND expires_at>?",
                           (_digest(str(credential)), now)).fetchone()
        if not row:
            return None
        scope = _json_load(row["scope_json"])
        return {"scope": scope, "scopes": sorted(scope), "device_id": row["device_id"], "expires_at": row["expires_at"]}


def revoke_device(credential: str | None, device_id: str | None = None) -> bool:
    if not credential:
        return False
    with _LOCK, _connect() as conn:
        if device_id:
            result = conn.execute("UPDATE device_credentials SET revoked=1 WHERE credential_hash=? AND device_id=? AND revoked=0",
                                  (_digest(str(credential)), device_id))
        else:
            result = conn.execute("UPDATE device_credentials SET revoked=1 WHERE credential_hash=? AND revoked=0",
                                  (_digest(str(credential)),))
        if result.rowcount:
            _LOGGER.info("Native device credential revoked")
            return True
        return False


def clear_pairings() -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM device_pairings")
        conn.execute("DELETE FROM device_credentials")
