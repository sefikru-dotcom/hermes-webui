"""Web Push (VAPID) support for the Hermes WebUI.

Self-contained, per-profile:

* VAPID keys are generated on first use and stored in ``STATE_DIR/push_vapid.json``.
* Browser subscriptions are stored in ``STATE_DIR/push_subscriptions.json``
  (the WebUI runs one process per isolated profile with its own STATE_DIR, so
  each space keeps its own devices).
* ``send_push`` delivers a payload to every live subscription and prunes
  subscriptions the push service reports as gone (404/410).

Push is the only channel that reaches an installed PWA while it is closed.
On iOS this requires the PWA to be installed on the Home Screen and the
service worker to have a ``push`` handler.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from api.config import STATE_DIR

logger = logging.getLogger(__name__)

VAPID_FILE = STATE_DIR / "push_vapid.json"
SUBSCRIPTIONS_FILE = STATE_DIR / "push_subscriptions.json"

_LOCK = threading.Lock()
_VAPID_CACHE: dict[str, Any] = {}

# The WebUI re-execs into Hermes' bundled, uv-managed runtime Python, which must
# not be modified. Web Push dependencies therefore live in a vendored directory
# that is injected onto sys.path here — no systemd unit or runtime edits needed.
_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "push"

# Push services that mean "this subscription is dead" -> drop it.
_GONE_STATUS_CODES = {404, 410}


def _ensure_push_backend() -> tuple[bool, str]:
    """Make the vendored Web Push stack importable. Returns (ok, error)."""
    import sys

    if _VENDOR_DIR.is_dir():
        vendor = str(_VENDOR_DIR)
        if vendor not in sys.path:
            sys.path.append(vendor)
    try:
        import pywebpush  # noqa: F401
        from py_vapid import Vapid01  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on install
        return False, str(exc)
    return True, ""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _vapid_subject() -> str:
    """VAPID ``sub`` claim. A mailto: or https: URL; overridable via env."""
    env = os.getenv("HERMES_WEBUI_VAPID_SUBJECT", "").strip()
    if env:
        return env
    return "mailto:mars@kvartalpro.ru"


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _normalise_private_key(stored: str) -> str:
    """Return the VAPID private key as ``base64url(raw 32-byte scalar)``.

    pywebpush hands this value to ``py_vapid.Vapid.from_string``, which only
    understands base64url raw keys: a PEM body makes it decode garbage and call
    ``load_der_private_key``, failing with "ASN.1 parsing error: invalid
    length". Legacy files store PEM, so normalise (and rewrite) once here.
    """
    value = (stored or "").strip()
    if not value:
        raise ValueError("empty VAPID private key")
    if "-----BEGIN" in value:
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_private_key(value.encode("ascii"), password=None)
        raw = key.private_numbers().private_value.to_bytes(32, "big")
        return _b64url(raw)
    raw = _b64url_decode(value)
    if len(raw) != 32:
        raise ValueError("VAPID private key is not a 32-byte scalar")
    return _b64url(raw)


def _public_from_private(private_b64: str) -> str:
    """Derive the uncompressed-Point public key for a raw private key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    raw = _b64url_decode(private_b64)
    key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(public_raw)


def _record_from_private(stored_private: str) -> dict[str, str]:
    private_b64 = _normalise_private_key(stored_private)
    return {"public_key": _public_from_private(private_b64), "private_key": private_b64}


def _write_vapid(record: dict[str, str]) -> None:
    VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = VAPID_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(VAPID_FILE)


def ensure_vapid_keys() -> dict[str, str]:
    """Return ``{'public_key': <base64url>, 'private_key': <PEM>}``.

    Generates and persists a P-256 key pair on first use. VAPID keys must stay
    stable: rotating them silently invalidates every existing subscription.
    """
    with _LOCK:
        if _VAPID_CACHE.get("public_key") and _VAPID_CACHE.get("private_key"):
            return {
                "public_key": _VAPID_CACHE["public_key"],
                "private_key": _VAPID_CACHE["private_key"],
            }

        if VAPID_FILE.exists():
            try:
                stored = json.loads(VAPID_FILE.read_text(encoding="utf-8"))
                if stored.get("private_key"):
                    record = _record_from_private(stored["private_key"])
                    if (
                        stored.get("public_key") != record["public_key"]
                        or stored.get("private_key") != record["private_key"]
                    ):
                        # Legacy PEM storage (or a mismatched public half):
                        # rewrite in the raw format pywebpush can actually use.
                        # Same key material, so existing subscriptions stay valid.
                        _write_vapid(record)
                        logger.info("Migrated VAPID key storage at %s", VAPID_FILE)
                    _VAPID_CACHE.update(record)
                    return {
                        "public_key": record["public_key"],
                        "private_key": record["private_key"],
                    }
            except Exception:
                logger.warning("Failed to read %s; regenerating VAPID keys", VAPID_FILE)

        ok, err = _ensure_push_backend()
        if not ok:
            raise RuntimeError(f"Web Push backend unavailable: {err}")

        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        record = _record_from_private(_b64url(private_key.private_numbers().private_value.to_bytes(32, "big")))
        _write_vapid(record)
        _VAPID_CACHE.update(record)
        logger.info("Generated new VAPID key pair at %s", VAPID_FILE)
        return record


def public_key() -> str:
    return ensure_vapid_keys()["public_key"]


def _load_subscriptions() -> list[dict[str, Any]]:
    try:
        raw = json.loads(SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = raw.get("subscriptions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and item.get("endpoint")]


def _save_subscriptions(items: list[dict[str, Any]]) -> None:
    SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"subscriptions": items, "updated_at": time.time()}
    tmp = SUBSCRIPTIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SUBSCRIPTIONS_FILE)


def add_subscription(payload: dict[str, Any], user_agent: str = "") -> dict[str, Any]:
    """Store (or refresh) a browser push subscription. Deduped by endpoint."""
    endpoint = str(payload.get("endpoint") or "").strip()
    if not endpoint:
        raise ValueError("endpoint is required")
    keys = payload.get("keys") or {}
    p256dh = str((keys or {}).get("p256dh") or "").strip()
    auth = str((keys or {}).get("auth") or "").strip()
    if not p256dh or not auth:
        raise ValueError("subscription keys (p256dh, auth) are required")

    with _LOCK:
        items = _load_subscriptions()
        for item in items:
            if item.get("endpoint") == endpoint:
                item.update(
                    {
                        "keys": {"p256dh": p256dh, "auth": auth},
                        "user_agent": user_agent or item.get("user_agent", ""),
                        "updated_at": time.time(),
                    }
                )
                _save_subscriptions(items)
                return {"ok": True, "added": False, "total": len(items)}

        # Защита от дублей: если тот же самый браузер/устройство регистрирует новый endpoint
        if user_agent:
            for item in items:
                if item.get("user_agent") == user_agent:
                    item.update(
                        {
                            "endpoint": endpoint,
                            "keys": {"p256dh": p256dh, "auth": auth},
                            "updated_at": time.time(),
                        }
                    )
                    _save_subscriptions(items)
                    return {"ok": True, "added": False, "total": len(items), "replaced_stale_endpoint": True}

        items.append(
            {
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
                "user_agent": user_agent,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        )
        _save_subscriptions(items)
        return {"ok": True, "added": True, "total": len(items)}


def remove_subscription(endpoint: str) -> dict[str, Any]:
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        raise ValueError("endpoint is required")
    with _LOCK:
        items = _load_subscriptions()
        kept = [item for item in items if item.get("endpoint") != endpoint]
        removed = len(items) - len(kept)
        if removed:
            _save_subscriptions(kept)
        return {"ok": True, "removed": removed, "total": len(kept)}


def subscription_count() -> int:
    return len(_load_subscriptions())


def send_push(
    title: str,
    body: str = "",
    url: str = "./",
    tag: str = "hermes-webui",
    *,
    urgent: bool = False,
    icon: str = "/static/favicon-192.png",
    badge: str = "/static/badge-monochrome-96.png",
) -> dict[str, Any]:
    """Deliver a notification to every stored subscription.

    Returns ``{'sent': n, 'pruned': m, 'total': k, 'errors': [...]}``. Never
    raises: a dead push service must not break the agent turn that triggered it.
    """
    items = _load_subscriptions()
    result: dict[str, Any] = {
        "sent": 0,
        "pruned": 0,
        "total": len(items),
        "errors": [],
    }
    if not items:
        return result

    ok, backend_err = _ensure_push_backend()
    if not ok:
        result["errors"].append(f"push backend unavailable: {backend_err}")
        logger.warning("push: backend unavailable: %s", backend_err)
        return result

    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:  # pragma: no cover - dependency missing
        result["errors"].append(f"pywebpush unavailable: {exc}")
        logger.warning("push: pywebpush unavailable: %s", exc)
        return result

    payload = json.dumps(
        {
            "title": title or "Марс",
            "body": body or "",
            "url": url or "./",
            "tag": tag or "hermes-webui",
            "icon": icon or "/static/favicon-192.png",
            "badge": badge or "/static/badge-monochrome-96.png",
            "urgent": bool(urgent),
            "ts": time.time(),
        },
        ensure_ascii=False,
    )
    keys = ensure_vapid_keys()
    claims = {"sub": _vapid_subject()}

    dead: list[str] = []
    for item in items:
        try:
            webpush(
                subscription_info={
                    "endpoint": item["endpoint"],
                    "keys": item.get("keys") or {},
                },
                data=payload,
                vapid_private_key=keys["private_key"],
                vapid_claims=dict(claims),
                timeout=15,
                ttl=600 if urgent else 3600,
            )
            result["sent"] += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in _GONE_STATUS_CODES:
                dead.append(item["endpoint"])
                result["pruned"] += 1
            else:
                result["errors"].append(f"{status or 'error'}: {exc}")
                logger.warning("push: delivery failed (%s): %s", status, exc)
        except Exception as exc:  # pragma: no cover - defensive
            result["errors"].append(str(exc))
            logger.warning("push: unexpected delivery error: %s", exc)

    if dead:
        with _LOCK:
            remaining = [
                item for item in _load_subscriptions()
                if item.get("endpoint") not in set(dead)
            ]
            _save_subscriptions(remaining)
        result["total"] = len(remaining)

    return result


def status() -> dict[str, Any]:
    """Small introspection payload for the settings UI / diagnostics."""
    ok, err = _ensure_push_backend()
    return {
        "supported": ok,
        "reason": err,
        "subscriptions": subscription_count(),
        "vapid_public_key": public_key() if ok else "",
        "subject": _vapid_subject(),
    }
