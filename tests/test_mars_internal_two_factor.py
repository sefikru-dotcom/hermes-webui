"""Regression: the X-Mars-Internal loopback bypass needs a second factor.

Before the fix a bare ``X-Mars-Internal: 1`` from a loopback peer returned
True from ``check_auth`` -- and because the reverse proxy forwards *every*
public request to 127.0.0.1:8787, any remote caller could authenticate with
that one header.  The gate now requires a loopback peer AND a shared secret
read from an owner-only (0600) file.

These checks exercise the gate function directly with a stub handler so they
stay hermetic and never touch production state.
"""
import os
import tempfile
from pathlib import Path


class _StubHandler:
    def __init__(self, peer, header=None):
        self.client_address = (peer, 12345)
        self.headers = {} if header is None else {"X-Mars-Internal": header}


def _gate(monkeypatch, tmp_path, peer, header):
    import api.auth as auth

    secret = tmp_path / "internal_secret"
    secret.write_text("s3cret-value", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setenv("HERMES_WEBUI_INTERNAL_SECRET_FILE", str(secret))
    auth._mars_internal_secret_cache.update({"path": None, "mtime": None, "value": None})
    return auth._mars_internal_authorized(_StubHandler(peer, header))


def test_bare_flag_is_rejected(monkeypatch, tmp_path):
    # The retired public bypass must never authenticate again.
    assert _gate(monkeypatch, tmp_path, "127.0.0.1", "1") is False


def test_correct_secret_from_loopback_is_accepted(monkeypatch, tmp_path):
    assert _gate(monkeypatch, tmp_path, "127.0.0.1", "s3cret-value") is True


def test_wrong_secret_is_rejected(monkeypatch, tmp_path):
    assert _gate(monkeypatch, tmp_path, "127.0.0.1", "nope") is False


def test_non_loopback_peer_is_rejected_even_with_secret(monkeypatch, tmp_path):
    assert _gate(monkeypatch, tmp_path, "203.0.113.7", "s3cret-value") is False


def test_world_readable_secret_is_refused(monkeypatch, tmp_path):
    import api.auth as auth

    secret = tmp_path / "leaky_secret"
    secret.write_text("s3cret-value", encoding="utf-8")
    secret.chmod(0o644)
    monkeypatch.setenv("HERMES_WEBUI_INTERNAL_SECRET_FILE", str(secret))
    auth._mars_internal_secret_cache.update({"path": None, "mtime": None, "value": None})
    assert auth._mars_internal_authorized(_StubHandler("127.0.0.1", "s3cret-value")) is False


def test_missing_secret_file_is_refused(monkeypatch, tmp_path):
    import api.auth as auth

    monkeypatch.setenv("HERMES_WEBUI_INTERNAL_SECRET_FILE", str(tmp_path / "absent"))
    auth._mars_internal_secret_cache.update({"path": None, "mtime": None, "value": None})
    assert auth._mars_internal_authorized(_StubHandler("127.0.0.1", "s3cret-value")) is False
