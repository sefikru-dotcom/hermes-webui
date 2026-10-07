"""Short-lived native pairing credentials.

The legacy ``/api/auth/device-pairings`` routes and their versioned
``/api/v1/auth/device-pairings`` aliases share this contract.  An approved
poll returns the bearer token only on its first authenticated poll; subsequent
polls retain the approved metadata but never return the token.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid

PAIRING_TTL_SECONDS = 300
DEVICE_CREDENTIAL_TTL_SECONDS = 3600
_CODE_RETRY_LIMIT = 100
_LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()
_PAIRINGS: dict[str, dict] = {}
_CREDENTIALS: dict[str, dict] = {}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prune(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for pair_id, pair in list(_PAIRINGS.items()):
        if now >= pair["expires_at"]:
            _PAIRINGS.pop(pair_id, None)
    for credential_hash, credential in list(_CREDENTIALS.items()):
        if now >= credential["expires_at"] or credential["revoked"]:
            _CREDENTIALS.pop(credential_hash, None)


def create_pairing() -> dict:
    """Create a pairing record and return only TV-safe bootstrap values."""
    with _LOCK:
        _prune()
        for _ in range(_CODE_RETRY_LIMIT):
            code = f"{secrets.randbelow(1_000_000):06d}"
            if not any(p["code"] == code for p in _PAIRINGS.values()):
                break
        else:
            raise RuntimeError("Unable to allocate a pairing code")
        pair_id = uuid.uuid4().hex
        poll_secret = secrets.token_urlsafe(32)
        now = time.time()
        _PAIRINGS[pair_id] = {
            "code": code,
            "poll_secret": poll_secret,
            "created_at": now,
            "expires_at": now + PAIRING_TTL_SECONDS,
            "status": "pending",
            "scope": None,
            "credential_hash": None,
            "device_id": None,
        }
        return {
            "pair_id": pair_id,
            "code": code,
            "poll_secret": poll_secret,
            "expires_in": PAIRING_TTL_SECONDS,
            "status": "pending",
        }


def pairing_status(pair_id: str, poll_secret: str | None) -> dict | None:
    """Authenticate polling and reveal the native token at most once."""
    with _LOCK:
        _prune()
        pair = _PAIRINGS.get(pair_id)
        if not pair or not poll_secret or not hmac.compare_digest(pair["poll_secret"], poll_secret):
            return None
        device_token = None
        if pair["status"] == "approved" and pair["credential_hash"] is None:
            device_token = "hdev_" + secrets.token_urlsafe(32)
            credential_hash = _digest(device_token)
            pair["device_id"] = "dev_" + uuid.uuid4().hex
            pair["credential_hash"] = credential_hash
            _CREDENTIALS[credential_hash] = {
                "expires_at": time.time() + DEVICE_CREDENTIAL_TTL_SECONDS,
                "scope": dict(pair["scope"] or {}),
                "device_id": pair["device_id"],
                "revoked": False,
            }
        if pair["status"] == "pending":
            return {"pair_id": pair_id, "status": "pending", "expires_in": max(0, int(pair["expires_at"] - time.time()))}
        response = {
            "pair_id": pair_id,
            "status": "approved",
            "device_id": pair["device_id"],
            "scopes": sorted(pair["scope"] or {}),
            "expires_in": max(0, int((_CREDENTIALS[pair["credential_hash"]]["expires_at"] - time.time()))),
        }
        if device_token is not None:
            response["device_token"] = device_token
        return response


def approve_pairing(code: str, session_cookie: str | None = None, *, scope: dict | None = None) -> dict | None:
    """Approve from an authenticated browser without retaining its cookie."""
    code = str(code or "").strip()
    if len(code) != 6 or not code.isdigit() or (session_cookie is None and scope is None):
        return None
    with _LOCK:
        _prune()
        for pair_id, pair in _PAIRINGS.items():
            if hmac.compare_digest(pair["code"], code):
                if pair["status"] != "pending":
                    return {"pair_id": pair_id, "status": pair["status"]}
                pair["status"] = "approved"
                pair["scope"] = dict(scope or {})
                return {"pair_id": pair_id, "status": "approved"}
    return None


def authenticate_device(credential: str | None) -> dict | None:
    """Return credential scope when a bearer credential is valid."""
    if not credential:
        return None
    with _LOCK:
        _prune()
        record = _CREDENTIALS.get(_digest(str(credential)))
        if not record or record["revoked"]:
            return None
        return {
            "scope": dict(record["scope"]),
            "scopes": sorted(record["scope"]),
            "device_id": record["device_id"],
            "expires_at": record["expires_at"],
        }


def revoke_device(credential: str | None) -> bool:
    """Revoke a native credential without logging or returning its value."""
    if not credential:
        return False
    with _LOCK:
        record = _CREDENTIALS.get(_digest(str(credential)))
        if not record:
            return False
        record["revoked"] = True
        _LOGGER.info("Native device credential revoked")
        return True


def clear_pairings() -> None:
    """Test helper; not exposed as an HTTP operation."""
    with _LOCK:
        _PAIRINGS.clear()
        _CREDENTIALS.clear()
