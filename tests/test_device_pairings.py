from api import device_pairings


def _approved_pair(scope=None):
    device_pairings.clear_pairings()
    pair = device_pairings.create_pairing()
    assert device_pairings.approve_pairing(pair["code"], scope=scope or {"profile": "default"}) == {
        "pair_id": pair["pair_id"],
        "status": "approved",
    }
    return pair


def test_pairing_create_status_and_approve():
    pair = _approved_pair()
    assert len(pair["code"]) == 6 and pair["code"].isdigit()
    assert pair["expires_in"] == 300
    assert device_pairings.pairing_status(pair["pair_id"], "wrong") is None
    pending = device_pairings.create_pairing()
    assert device_pairings.pairing_status(pending["pair_id"], pending["poll_secret"]) == {
        "pair_id": pending["pair_id"], "status": "pending", "expires_in": 300
    }


def test_approved_status_is_exact_native_response_and_device_id_is_stable(monkeypatch):
    monkeypatch.setattr(device_pairings.time, "time", lambda: 1000.0)
    pair = _approved_pair({"profile": "default"})

    first = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    assert first is not None
    assert set(first) == {"pair_id", "status", "device_token", "device_id", "scopes", "expires_in"}
    assert first["status"] == "approved"
    assert first["device_token"].startswith("hdev_")
    assert first["device_id"] == device_pairings._PAIRINGS[pair["pair_id"]]["device_id"]
    assert first["scopes"] == ["profile"]
    assert first["expires_in"] == device_pairings.DEVICE_CREDENTIAL_TTL_SECONDS

    second = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    assert second == {
        "pair_id": pair["pair_id"],
        "status": "approved",
        "device_id": first["device_id"],
        "scopes": ["profile"],
        "expires_in": device_pairings.DEVICE_CREDENTIAL_TTL_SECONDS,
    }


def test_pairing_expires_and_wrong_secret_does_not_reveal_status(monkeypatch):
    device_pairings.clear_pairings()
    now = [1000.0]
    monkeypatch.setattr(device_pairings.time, "time", lambda: now[0])
    pair = device_pairings.create_pairing()
    assert device_pairings.pairing_status(pair["pair_id"], "wrong") is None
    now[0] += device_pairings.PAIRING_TTL_SECONDS + 1
    assert device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"]) is None
    assert device_pairings.approve_pairing(pair["code"], "session-cookie") is None


def test_native_credential_expires_and_can_be_revoked_without_secret_logs(monkeypatch, caplog):
    device_pairings.clear_pairings()
    now = [1000.0]
    monkeypatch.setattr(device_pairings.time, "time", lambda: now[0])
    pair = _approved_pair()
    first = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    token = first["device_token"]
    auth = device_pairings.authenticate_device(token)
    assert auth is not None
    assert auth["device_id"] == first["device_id"]

    assert device_pairings.revoke_device(token) is True
    assert device_pairings.authenticate_device(token) is None
    assert token not in caplog.text


def test_native_credential_expiry_is_enforced(monkeypatch):
    device_pairings.clear_pairings()
    now = [1000.0]
    monkeypatch.setattr(device_pairings.time, "time", lambda: now[0])
    pair = _approved_pair()
    first = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    assert first is not None
    now[0] += device_pairings.DEVICE_CREDENTIAL_TTL_SECONDS + 1
    assert device_pairings.authenticate_device(first["device_token"]) is None


def test_native_credential_is_stored_only_as_hash():
    pair = _approved_pair()
    first = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    token = first["device_token"]
    record = device_pairings._CREDENTIALS[device_pairings._digest(token)]
    assert token not in record.values()
    assert "device_token" not in record


def test_versioned_endpoint_aliases_are_documented_in_route_contract():
    from api import auth, routes

    assert "/api/v1/auth/device-pairings" in auth.PUBLIC_PATHS
    assert "/api/v1/auth/device-pairings/status" in auth.PUBLIC_PATHS
    assert "/api/v1/auth/device-credentials/revoke" in auth.PUBLIC_PATHS
    assert "/api/v1/auth/device-pairings" in routes._csrf_exempt_path.__code__.co_consts
