
from api import device_pairings


def test_pairing_create_status_and_approve(monkeypatch):
    device_pairings.clear_pairings()
    pair = device_pairings.create_pairing()
    assert len(pair["code"]) == 6 and pair["code"].isdigit()
    assert pair["expires_in"] == 300
    assert device_pairings.pairing_status(pair["pair_id"], "wrong") is None
    assert device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])["status"] == "pending"
    approved = device_pairings.approve_pairing(pair["code"], "session-cookie")
    assert approved == {"pair_id": pair["pair_id"], "status": "approved"}
    assert device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])["status"] == "claimed"


def test_pairing_expires(monkeypatch):
    device_pairings.clear_pairings()
    now = [1000.0]
    monkeypatch.setattr(device_pairings.time, "time", lambda: now[0])
    pair = device_pairings.create_pairing()
    now[0] += device_pairings.PAIRING_TTL_SECONDS + 1
    assert device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"]) is None
    assert device_pairings.approve_pairing(pair["code"], "session-cookie") is None


def test_approved_pairing_reveals_a_scoped_native_credential_once():
    device_pairings.clear_pairings()
    pair = device_pairings.create_pairing()
    assert device_pairings.approve_pairing(pair["code"], scope={"profile": "default"})["status"] == "approved"

    first = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    assert first is not None
    assert first["status"] == "claimed"
    assert first["credential"].startswith("hdev_")
    assert first["scope"] == {"profile": "default"}
    second = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])
    assert second["credential"] is None

    assert device_pairings.authenticate_device(first["credential"])["scope"] == {"profile": "default"}
    assert device_pairings._PAIRINGS[pair["pair_id"]]["credential_hash"]


def test_native_credential_can_be_revoked_and_is_not_logged(monkeypatch, caplog):
    device_pairings.clear_pairings()
    pair = device_pairings.create_pairing()
    device_pairings.approve_pairing(pair["code"], scope={"profile": "default"})
    credential = device_pairings.pairing_status(pair["pair_id"], pair["poll_secret"])["credential"]

    assert device_pairings.revoke_device(credential) is True
    assert device_pairings.authenticate_device(credential) is None
    assert credential not in caplog.text
