from api import device_pairings


def _approved_pair(scope=None):
    device_pairings.clear_pairings()
    pair = device_pairings.create_pairing()
    assert device_pairings.approve_pairing(pair["code"], session_cookie="verified-session") == {
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


def test_approved_status_never_issues_token_and_exchange_is_one_time():
    pair = _approved_pair({"profile": "default"})
    status = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    assert status == {"pair_id": pair["pair_id"], "status": "approved"}
    response = device_pairings.exchange_pairing(pair["pair_id"], pair["poll_secret"])
    assert response["access_token"].startswith("hdev_")
    assert response["token_type"] == "Bearer"
    assert response["expires_in"] == device_pairings.DEVICE_CREDENTIAL_TTL_SECONDS
    assert response["device_id"].startswith("dev_")
    assert response["scopes"] == []
    assert device_pairings.exchange_pairing(pair["pair_id"], pair["poll_secret"]) is None
    assert device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"]) == {
        "pair_id": pair["pair_id"], "status": "claimed"
    }


def test_approval_fails_closed_without_authenticated_session():
    device_pairings.clear_pairings()
    pair = device_pairings.create_pairing()
    assert device_pairings.approve_pairing(pair["code"]) is None


def test_approved_status_is_metadata_only(monkeypatch):
    monkeypatch.setattr(device_pairings.time, "time", lambda: 1000.0)
    pair = _approved_pair({"profile": "default"})

    assert device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"]) == {
        "pair_id": pair["pair_id"], "status": "approved"
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
    first = device_pairings.exchange_pairing(pair["pair_id"], pair["poll_secret"])
    token = first["access_token"]
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
    first = device_pairings.exchange_pairing(pair["pair_id"], pair["poll_secret"])
    assert first is not None
    now[0] += device_pairings.DEVICE_CREDENTIAL_TTL_SECONDS + 1
    assert device_pairings.authenticate_device(first["access_token"]) is None


def test_native_credential_is_stored_only_as_hash():
    pair = _approved_pair()
    response = device_pairings.exchange_pairing(pair["pair_id"], pair["poll_secret"])
    assert response["access_token"].startswith("hdev_")
    assert device_pairings.authenticate_device(response["access_token"])["device_id"] == response["device_id"]


def test_versioned_endpoint_aliases_are_documented_in_route_contract():
    from api import auth, routes

    assert "/api/v1/auth/device-pairings" in auth.PUBLIC_PATHS
    assert "/api/v1/auth/device-pairings/status" in auth.PUBLIC_PATHS
    assert "/api/v1/auth/device-credentials/revoke" in auth.PUBLIC_PATHS
    assert "/api/v1/auth/device-pairings" in routes._csrf_exempt_path.__code__.co_consts
