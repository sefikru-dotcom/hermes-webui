"""Unit tests for api/push_fcm.py — FCM HTTP v1 delivery + token registry.

No network, no google-auth stack, no real service key:
  * the sqlite registry runs against a per-test tmp STATE_DIR;
  * delivery goes through the ``_http_post`` seam replaced by fakes;
  * OAuth is replaced at the ``_fetch_access_token`` / ``_access_token``
    seams so the token CACHE logic is exercised without credentials.

Route-level coverage of the Bearer admission for /api/push/fcm/* lives in
tests/test_native_allowlist_auth.py and
tests/test_native_bearer_readonly_allowlist.py (pinned allowlists).
"""
import io
import json
import time
from types import SimpleNamespace

import pytest

from api import push_fcm


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Point the registry at a throwaway dir and clear the OAuth cache."""
    monkeypatch.setattr(push_fcm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(push_fcm, "FCM_DB_PATH", tmp_path / "push_fcm.db")
    push_fcm._reset_access_cache()
    yield
    push_fcm._reset_access_cache()


def _fake_response(status_code, payload=None):
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: (payload if payload is not None else {}),
    )


def _fcm_error(code, reason=None, message=""):
    details = (
        [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
          "reason": reason}]
        if reason else []
    )
    return {"error": {"code": code, "message": message, "details": details}}


# ── registry ────────────────────────────────────────────────────────────────

def test_register_and_unregister_roundtrip():
    result = push_fcm.register_token(
        {"token": "tok-a", "platform": "android", "device_name": "Pixel"},
        device_id="dev_1",
    )
    assert result["registered"] is True
    assert result["platform"] == "android"
    assert result["device_id"] == "dev_1"
    assert result["total"] == 1
    assert push_fcm.token_count() == 1

    removed = push_fcm.unregister_token({"token": "tok-a"})
    assert removed == {"ok": True, "removed": 1, "total": 0}
    assert push_fcm.token_count() == 0
    # Unregistering an unknown token is a clean no-op, not an error.
    assert push_fcm.unregister_token("tok-a")["removed"] == 0


def test_register_rejects_bad_input():
    with pytest.raises(ValueError):
        push_fcm.register_token({"token": "   "})
    with pytest.raises(ValueError):
        push_fcm.register_token({"token": "tok", "platform": "symbian"})
    with pytest.raises(ValueError):
        push_fcm.register_token({"token": "x" * 5000})
    with pytest.raises(ValueError):
        push_fcm.unregister_token({"token": ""})  # no token, no device_id


def test_register_rotates_one_live_token_per_device():
    push_fcm.register_token({"token": "old-token"}, device_id="dev_9")
    push_fcm.register_token({"token": "new-token"}, device_id="dev_9")
    tokens = {row["token"] for row in push_fcm._list_tokens()}
    assert tokens == {"new-token"}  # Firebase rotation leaves no zombie
    # A different device keeps its own token.
    push_fcm.register_token({"token": "other-device"}, device_id="dev_10")
    assert push_fcm.token_count() == 2
    # Re-registering the SAME token is an idempotent upsert.
    push_fcm.register_token({"token": "new-token", "device_name": "renamed"},
                            device_id="dev_9")
    assert push_fcm.token_count() == 2
    names = {row["token"]: row["device_name"] for row in push_fcm._list_tokens()}
    assert names["new-token"] == "renamed"


def test_unregister_by_device_id_removes_all_its_tokens():
    push_fcm.register_token({"token": "t1"}, device_id="dev_x")
    push_fcm.register_token({"token": "t2"}, device_id="dev_x")
    push_fcm.register_token({"token": "t3"}, device_id="dev_y")
    # t2 replaced t1 for dev_x via rotation, but a manual insert proves the
    # device-wide unregister path regardless.
    result = push_fcm.unregister_token({"token": ""}, device_id="dev_x")
    assert result["removed"] >= 1
    remaining = {row["token"] for row in push_fcm._list_tokens()}
    assert "t3" in remaining
    assert not any(row["device_id"] == "dev_x" for row in push_fcm._list_tokens())


# ── payload shape ───────────────────────────────────────────────────────────

def test_build_message_payload_shape():
    msg = push_fcm._build_message(
        "tok", "Марс: ответ готов", "Превелик ответ", "session/abc",
        "hermes-abc", urgent=True,
    )
    message = msg["message"]
    assert msg.keys() == {"message"}
    assert message["token"] == "tok"
    assert message["notification"] == {
        "title": "Марс: ответ готов", "body": "Превелик ответ",
    }
    # FCM data values must all be strings.
    assert message["data"]["url"] == "session/abc"
    assert message["data"]["tag"] == "hermes-abc"
    assert all(isinstance(v, str) for v in message["data"].values())
    assert message["android"]["priority"] == "high"
    # Без kind — общий канал; id обязан совпадать с каналом нативного клиента,
    # иначе Android 8+ отбрасывает системный notification-payload.
    assert message["android"]["notification"]["channel_id"] == "mars_push_general"
    assert message["android"]["notification"]["tag"] == "hermes-abc"
    assert message["webpush"] == {"fcm_options": {"link": "session/abc"}}


def test_build_message_minimal_without_url_or_tag():
    message = push_fcm._build_message("tok", "Заголовок")["message"]
    assert "webpush" not in message
    assert "tag" not in message["android"]["notification"]
    assert message["android"]["priority"] == "normal"
    assert set(message["data"]) == {"ts"}


# ── delivery ────────────────────────────────────────────────────────────────

def _arm_delivery(monkeypatch, responses_by_token, captured):
    """Fake supported()+access token+HTTP; record every POST."""
    monkeypatch.setattr(push_fcm, "supported", lambda: (True, "", "mars-test"))
    monkeypatch.setattr(push_fcm, "_access_token",
                        lambda force_refresh=False: ("fake-access", "mars-test"))

    def fake_post(url, payload, headers, timeout):
        captured.append({"url": url, "payload": payload,
                         "headers": dict(headers), "timeout": timeout})
        token = payload["message"]["token"]
        entry = responses_by_token.get(token)
        if isinstance(entry, Exception):
            raise entry
        return entry

    monkeypatch.setattr(push_fcm, "_http_post", fake_post)


def test_send_fcm_delivers_every_token_and_marks_sent(monkeypatch):
    push_fcm.register_token({"token": "tok-1"})
    push_fcm.register_token({"token": "tok-2"})
    captured = []
    _arm_delivery(monkeypatch, {
        "tok-1": _fake_response(200, {"name": "projects/mars-test/messages/1"}),
        "tok-2": _fake_response(200, {"name": "projects/mars-test/messages/2"}),
    }, captured)

    result = push_fcm.send_fcm("Марс: ответ готов", "Готово",
                               url="session/s1", tag="hermes-s1")
    assert result == {"sent": 2, "pruned": 0, "total": 2, "errors": []}
    assert len(captured) == 2
    for call in captured:
        assert call["url"] == (
            "https://fcm.googleapis.com/v1/projects/mars-test/messages:send"
        )
        assert call["headers"]["Authorization"] == "Bearer fake-access"
        assert call["headers"]["Content-Type"] == "application/json"
        assert call["timeout"] == 10
        assert call["payload"]["message"]["notification"]["title"] == "Марс: ответ готов"
    # Delivery bookkeeping landed in the registry.
    with push_fcm._connection() as conn:
        rows = conn.execute(
            "SELECT token, sent_count, last_sent_at FROM fcm_tokens"
        ).fetchall()
    assert {row["token"]: row["sent_count"] for row in rows} == {"tok-1": 1, "tok-2": 1}
    assert all(row["last_sent_at"] for row in rows)


def test_send_fcm_prunes_dead_tokens_keeps_transient(monkeypatch):
    push_fcm.register_token({"token": "gone"})       # 404 UNREGISTERED
    push_fcm.register_token({"token": "invalid"})    # 400 INVALID_ARGUMENT(token)
    push_fcm.register_token({"token": "busy"})        # 503 — transient, keep
    push_fcm.register_token({"token": "bad-payload"}) # 400 generic — keep
    captured = []
    _arm_delivery(monkeypatch, {
        "gone": _fake_response(404, _fcm_error(
            "NOT_FOUND", "UNREGISTERED", "Requested entity was not found.")),
        "invalid": _fake_response(400, _fcm_error(
            "INVALID_ARGUMENT", "INVALID_ARGUMENT",
            "The registration token is not a valid FCM registration token")),
        "busy": _fake_response(503, _fcm_error(
            "UNAVAILABLE", None, "Service unavailable")),
        "bad-payload": _fake_response(400, _fcm_error(
            "INVALID_ARGUMENT", None, "Invalid JSON payload received")),
    }, captured)

    result = push_fcm.send_fcm("t", "b")
    assert result["sent"] == 0
    assert result["pruned"] == 2
    assert len(result["errors"]) == 2
    remaining = {row["token"] for row in push_fcm._list_tokens()}
    assert remaining == {"busy", "bad-payload"}
    # Transient failures are recorded on the surviving tokens, not pruned.
    with push_fcm._connection() as conn:
        errs = {row["token"]: row["last_error"] for row in conn.execute(
            "SELECT token, last_error FROM fcm_tokens").fetchall()}
    assert "503" in errs["busy"]
    assert "400" in errs["bad-payload"]


def test_send_fcm_refreshes_access_token_once_on_401(monkeypatch):
    push_fcm.register_token({"token": "tok-1"})
    captured = []
    monkeypatch.setattr(push_fcm, "supported", lambda: (True, "", "mars-test"))

    def access_token(force_refresh=False):
        return ("second-access" if force_refresh else "first-access", "mars-test")

    monkeypatch.setattr(push_fcm, "_access_token", access_token)

    def fake_post(url, payload, headers, timeout):
        captured.append(dict(headers))
        if headers["Authorization"] == "Bearer first-access":
            return _fake_response(401, _fcm_error("UNAUTHENTICATED", None, "expired"))
        return _fake_response(200, {"name": "ok"})

    monkeypatch.setattr(push_fcm, "_http_post", fake_post)
    result = push_fcm.send_fcm("t", "b")
    assert result["sent"] == 1
    assert result["errors"] == []
    assert len(captured) == 2
    assert captured[1]["Authorization"] == "Bearer second-access"


def test_send_fcm_survives_transport_errors(monkeypatch):
    push_fcm.register_token({"token": "tok-1"})
    captured = []
    _arm_delivery(monkeypatch, {"tok-1": ConnectionError("boom")}, captured)
    result = push_fcm.send_fcm("t", "b")
    assert result["sent"] == 0
    assert result["total"] == 1
    assert any("transport error" in e for e in result["errors"])
    # The token survives a transport failure.
    assert push_fcm.token_count() == 1


# ── access-token cache ──────────────────────────────────────────────────────

def test_access_token_cached_until_near_expiry(monkeypatch):
    calls = {"n": 0}
    now = time.time()

    def fake_fetch():
        calls["n"] += 1
        return (f"tok-{calls['n']}", now + 3600.0, "mars-test")

    monkeypatch.setattr(push_fcm, "_fetch_access_token", fake_fetch)
    first, project = push_fcm._access_token()
    second, _ = push_fcm._access_token()
    assert first == second == "tok-1"
    assert project == "mars-test"
    assert calls["n"] == 1  # served from cache

    token, _ = push_fcm._access_token(force_refresh=True)
    assert token == "tok-2"
    assert calls["n"] == 2

    # Inside the skew window (expiry - now <= 300 s) the cache is bypassed.
    push_fcm._ACCESS_CACHE.update({"token": "stale", "expiry": now + 100.0,
                                   "project_id": "mars-test"})
    token, _ = push_fcm._access_token()
    assert token == "tok-3"
    assert calls["n"] == 3


def test_send_fcm_silent_noop_without_service_key(monkeypatch):
    push_fcm.register_token({"token": "tok-1"})
    monkeypatch.setattr(push_fcm, "_service_key_path", lambda: None)

    def explode(*_a, **_k):
        raise AssertionError("network/HTTP must never be reached without a key")

    monkeypatch.setattr(push_fcm, "_http_post", explode)
    monkeypatch.setattr(push_fcm, "_fetch_access_token", explode)

    result = push_fcm.send_fcm("t", "b")  # must not raise
    assert result["sent"] == 0
    assert result["pruned"] == 0
    assert result["total"] == 0  # reported as no-op, streaming hook stays quiet
    assert result["errors"] and "unavailable" in result["errors"][0].lower()
    assert push_fcm.token_count() == 1  # registry untouched


def test_send_fcm_empty_registry_short_circuits(monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("no token mint when the registry is empty")

    monkeypatch.setattr(push_fcm, "_fetch_access_token", explode)
    result = push_fcm.send_fcm("t", "b")
    assert result == {"sent": 0, "pruned": 0, "total": 0, "errors": []}


def test_is_dead_token_matrix():
    assert push_fcm._is_dead_token(404, _fcm_error(
        "NOT_FOUND", "UNREGISTERED", "Requested entity was not found."))
    assert push_fcm._is_dead_token(400, _fcm_error(
        "INVALID_ARGUMENT", "INVALID_ARGUMENT",
        "The registration token is not a valid FCM registration token"))
    # Generic INVALID_ARGUMENT (no 'token' in the message) must NOT prune.
    assert not push_fcm._is_dead_token(400, _fcm_error(
        "INVALID_ARGUMENT", None, "Invalid JSON payload received"))
    assert not push_fcm._is_dead_token(503, _fcm_error(
        "UNAVAILABLE", None, "Service unavailable"))
    assert not push_fcm._is_dead_token(401, _fcm_error(
        "UNAUTHENTICATED", None, "Request had invalid authentication"))
    assert not push_fcm._is_dead_token(0, {})


# ── status ──────────────────────────────────────────────────────────────────

def test_status_reports_registry_without_secrets(monkeypatch):
    monkeypatch.setattr(push_fcm, "_service_key_path", lambda: None)
    status = push_fcm.status()
    assert status["supported"] is False
    assert status["tokens"] == 0
    assert status["by_platform"] == {}
    assert status["project_id"] == ""
    # No key material can ever leak into the payload.
    assert "private_key" not in json.dumps(status)

    push_fcm.register_token({"token": "t-and", "platform": "android"})
    push_fcm.register_token({"token": "t-web", "platform": "web"})
    status = push_fcm.status()
    assert status["tokens"] == 2
    assert status["by_platform"] == {"android": 1, "web": 1}


# ── route handlers (dispatch level, stubbed body) ──────────────────────────

def _stub_handler(monkeypatch, body):
    from api import routes

    monkeypatch.setattr(routes, "read_body", lambda _h: body)
    responses = []
    handler = SimpleNamespace(
        client_address=("127.0.0.1", 1),
        headers={},
        command="POST",
        send_response=lambda status: responses.append(status),
        send_header=lambda *_a: None,
        end_headers=lambda: None,
        wfile=io.BytesIO(),
    )
    handler._native_auth_device = {"device_id": "dev_42", "scopes": []}
    return routes, handler, responses


def test_route_register_binds_device_from_credential(monkeypatch, tmp_path):
    routes, handler, responses = _stub_handler(
        monkeypatch, {"token": "route-tok", "platform": "android",
                      "device_name": "TV"})
    routes.handle_post(
        handler, SimpleNamespace(path="/api/push/fcm/register", query=""))
    assert responses and responses[0] == 200
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert payload["registered"] is True
    assert payload["device_id"] == "dev_42"  # from the credential, not the body
    rows = push_fcm._list_tokens()
    assert len(rows) == 1
    assert rows[0]["token"] == "route-tok"
    assert rows[0]["device_id"] == "dev_42"
    assert rows[0]["device_name"] == "TV"


def test_route_register_rejects_missing_token_with_400(monkeypatch):
    routes, handler, responses = _stub_handler(monkeypatch, {"platform": "android"})
    routes.handle_post(
        handler, SimpleNamespace(path="/api/push/fcm/register", query=""))
    assert responses and responses[0] == 400
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert "token" in payload["error"]


def test_route_unregister_removes_token(monkeypatch):
    push_fcm.register_token({"token": "route-tok"}, device_id="dev_42")
    routes, handler, responses = _stub_handler(monkeypatch, {"token": "route-tok"})
    routes.handle_post(
        handler, SimpleNamespace(path="/api/push/fcm/unregister", query=""))
    assert responses and responses[0] == 200
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert payload["removed"] == 1
    assert push_fcm.token_count() == 0


# ── нативные каналы и маршрутизация (клиент Марса) ─────────────────────────

def test_channel_for_kind_mirrors_the_client_policy():
    # app-mobile PushMessagePolicy: chat/approval/clarify/turn/cron — high.
    for kind in ("chat", "approval", "clarify", "turn", "cron", "APPROVAL"):
        assert push_fcm.channel_for_kind(kind) == "mars_push_chat", kind
    for kind in ("system", "music", "", None, "unknown"):
        assert push_fcm.channel_for_kind(kind) == "mars_push_general", kind


def test_build_message_carries_routing_contract():
    message = push_fcm._build_message(
        "tok", "Требуется подтверждение", "rm -rf /tmp/x",
        kind="approval", session_id="s-1", notification_id=42,
    )["message"]
    assert message["data"]["kind"] == "approval"
    assert message["data"]["session_id"] == "s-1"
    assert message["data"]["notification_id"] == "42"
    assert message["android"]["notification"]["channel_id"] == "mars_push_chat"
    # approval — событие, требующее внимания: heads-up даже без urgent=True.
    assert message["android"]["priority"] == "high"
    assert all(isinstance(v, str) for v in message["data"].values())


def test_build_message_kind_is_normalized_and_omitted_when_blank():
    message = push_fcm._build_message("tok", "t", kind="  ", session_id="")["message"]
    assert "kind" not in message["data"]
    assert "session_id" not in message["data"]
    assert message["android"]["priority"] == "normal"


def test_send_fcm_passes_routing_fields_into_every_message(monkeypatch):
    push_fcm.register_token({"token": "tok-1"})
    captured = []
    _arm_delivery(monkeypatch, {"tok-1": _fake_response(200, {"name": "x"})}, captured)

    push_fcm.send_fcm(
        "Марс спрашивает", "Какой вариант?", kind="clarify",
        session_id="s-9", notification_id=7,
    )
    data = captured[0]["payload"]["message"]["data"]
    assert data["kind"] == "clarify"
    assert data["session_id"] == "s-9"
    assert data["notification_id"] == "7"
    assert captured[0]["payload"]["message"]["android"]["notification"]["channel_id"] == "mars_push_chat"


# ── тихий пуш ──────────────────────────────────────────────────────────────

def test_send_fcm_silent_is_data_only(monkeypatch):
    push_fcm.register_token({"token": "tok-1"})
    captured = []
    _arm_delivery(monkeypatch, {"tok-1": _fake_response(200, {"name": "x"})}, captured)

    result = push_fcm.send_fcm_silent(
        {"action": "refresh_pending", "session_id": "s-1"}, ttl_seconds=600
    )

    assert result == {"sent": 1, "pruned": 0, "total": 1, "errors": []}
    message = captured[0]["payload"]["message"]
    # Нет notification-блока — Android не рисует уведомление вообще.
    assert "notification" not in message
    assert "notification" not in message["android"]
    assert message["data"] == {
        "action": "refresh_pending",
        "session_id": "s-1",
        "silent": "1",
        "kind": "sync",
        "ts": message["data"]["ts"],
    }
    assert message["android"]["priority"] == "normal"
    assert message["android"]["ttl"] == "600s"


def test_send_fcm_silent_keeps_explicit_ts_and_drops_none_values(monkeypatch):
    push_fcm.register_token({"token": "tok-1"})
    captured = []
    _arm_delivery(monkeypatch, {"tok-1": _fake_response(200, {"name": "x"})}, captured)

    push_fcm.send_fcm_silent({"ts": "111", "session_id": None, "action": "token_resync"})
    data = captured[0]["payload"]["message"]["data"]
    assert data["ts"] == "111"
    assert data["session_id"] == ""
    assert data["action"] == "token_resync"


def test_send_fcm_silent_prunes_dead_tokens(monkeypatch):
    push_fcm.register_token({"token": "gone"})
    captured = []
    _arm_delivery(monkeypatch, {
        "gone": _fake_response(404, _fcm_error("NOT_FOUND", "UNREGISTERED", "Requested entity was not found.")),
    }, captured)

    result = push_fcm.send_fcm_silent({"action": "refresh_sessions"})
    assert result["pruned"] == 1
    assert push_fcm.token_count() == 0


# ── approval/clarify триггер ───────────────────────────────────────────────

def test_notify_attention_async_sends_a_decision_push(monkeypatch):
    import threading

    calls = []
    done = threading.Event()

    def fake_send_fcm(title, body="", url=None, tag=None, **kwargs):
        calls.append({"title": title, "body": body, "tag": tag, **kwargs})
        done.set()
        return {"sent": 1, "pruned": 0, "total": 1, "errors": []}

    monkeypatch.setattr(push_fcm, "send_fcm", fake_send_fcm)

    push_fcm.notify_attention_async("approval", "s-1", "Требуется подтверждение", "rm -rf /tmp")
    assert done.wait(5), "push must be delivered from the background thread"
    assert calls[0]["kind"] == "approval"
    assert calls[0]["session_id"] == "s-1"
    assert calls[0]["urgent"] is True
    assert calls[0]["tag"] == "s-1"
    assert isinstance(calls[0]["notification_id"], int)


def test_notify_attention_async_ignores_non_decision_kinds(monkeypatch):
    calls = []
    monkeypatch.setattr(push_fcm, "send_fcm", lambda *a, **k: calls.append(a))

    push_fcm.notify_attention_async("chat", "s-1", "t")
    push_fcm.notify_attention_async("", "s-1", "t")
    assert calls == []


def test_notify_attention_async_never_raises(monkeypatch):
    import threading

    done = threading.Event()

    def boom(*args, **kwargs):
        done.set()
        raise RuntimeError("FCM exploded")

    monkeypatch.setattr(push_fcm, "send_fcm", boom)

    # Не бросает в вызывающем потоке (иначе approval-хук уронил бы ход).
    push_fcm.notify_attention_async("clarify", "s-2", "Марс спрашивает", "?")
    assert done.wait(5)


def test_stable_notification_id_is_deterministic_and_31_bit():
    first = push_fcm._stable_notification_id("approval:abc")
    assert first == push_fcm._stable_notification_id("approval:abc")
    assert first != push_fcm._stable_notification_id("approval:abd")
    assert 0 <= first <= 0x7FFFFFFF
