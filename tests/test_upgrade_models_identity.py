"""Model identity: one provider-aware notion of "which model is this".

Covers the identity contract the WebUI needs across its three layers
(``api/config.py`` writers, ``api/routes.py`` request/session state,
``api/streaming.py`` runtime):

* ``@provider:model`` route hints split without eating a colon-bearing model
  (``:free``, ``gpt-5:beta``) or a ``custom:host:port`` endpoint slug;
* ``vendor/model`` and route suffixes are never rewritten — only validated;
* an id that carries no model at all (``''``, ``'   '``,
  ``'@custom:plusvibe-api:'``) is refused at the API boundary, while an id that
  merely *looks* unfamiliar (``':kiro'``, ``'qwen3:free'``) is not — only the
  provider catalog may judge that;
* the same model id on two providers is two identities.
"""

from __future__ import annotations

import pytest

import api.model_identity as mi
import api.routes as routes


# ── route hints ───────────────────────────────────────────────────────────────

def test_route_hint_known_provider_splits_once():
    assert mi.parse_route_hint("@openai:gpt-5", known_providers={"openai"}) == ("gpt-5", "openai")


def test_route_hint_keeps_model_side_suffix_for_known_provider():
    # `gpt-5:beta` is a model id with a tag, not `provider=gpt-5:beta`.
    assert mi.parse_route_hint("@openai:gpt-5:beta", known_providers={"openai"}) == (
        "gpt-5:beta",
        "openai",
    )


def test_route_hint_unknown_provider_splits_once():
    assert mi.parse_route_hint("@some-new-provider:model-x") == ("model-x", "some-new-provider")


def test_route_hint_named_custom_provider_keeps_colons_in_model():
    # `custom:plusvibe-api` is a *named* custom provider (no host:port), so the
    # model keeps everything after the second colon.
    assert mi.parse_route_hint("@custom:plusvibe-api:deepseek-v4") == (
        "deepseek-v4",
        "custom:plusvibe-api",
    )


def test_route_hint_endpoint_slug_is_not_peeled():
    # `custom:10.8.71.41:8080` is an endpoint-style slug: peeling `8080:` off
    # would corrupt the model name.
    assert mi.parse_route_hint("@custom:10.8.71.41:8080:Qwen3") == (
        "Qwen3",
        "custom:10.8.71.41:8080",
    )


def test_route_hint_absent_returns_none():
    assert mi.parse_route_hint("gpt-5") is None
    assert mi.parse_route_hint("vendor/model") is None
    assert mi.parse_route_hint("") is None


# ── validation at the boundary ────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_blank_model_id_is_refused(value):
    with pytest.raises(mi.ModelIdentityError) as exc:
        mi.validate_model_id(value)
    assert exc.value.code == "model_empty"


def test_empty_route_hint_tail_is_refused():
    # Production sessions carried this exact shape and were unroutable.
    with pytest.raises(mi.ModelIdentityError) as exc:
        mi.validate_model_id("@custom:plusvibe-api:")
    assert exc.value.code == "model_empty_tail"


@pytest.mark.parametrize(
    "value",
    [
        ":kiro",                  # leading colon: still a name, not our call
        "gpt-5",
        "openai/gpt-5",           # vendor slash preserved
        "openrouter/anthropic/claude-3",
        "qwen3:free",             # route suffix preserved
        "@openai:gpt-5",
        "@custom:plusvibe-api:deepseek-v4-flash",
        "deepseek.v4-flash",
    ],
)
def test_plausible_ids_pass_through_unchanged(value):
    assert mi.validate_model_id(value) == value


@pytest.mark.parametrize("value", [123, {}, [], 1.5, True])
def test_non_string_model_id_is_refused(value):
    with pytest.raises(mi.ModelIdentityError) as exc:
        mi.validate_model_id(value)
    assert exc.value.code == "model_not_a_string"


def test_control_characters_are_refused():
    with pytest.raises(mi.ModelIdentityError) as exc:
        mi.validate_model_id("gpt\x00-5")
    assert exc.value.code == "model_control_chars"


def test_optional_model_id_allows_blank_for_auto_slots():
    # Auxiliary slots use "" to mean "auto"; that must stay legal.
    assert mi.validate_optional_model_id(None) == ""
    assert mi.validate_optional_model_id("  ") == ""
    assert mi.validate_optional_model_id("gpt-6-luna") == "gpt-6-luna"
    with pytest.raises(mi.ModelIdentityError):
        mi.validate_optional_model_id(123)


# ── identity comparison ──────────────────────────────────────────────────────

def test_same_model_different_provider_is_not_the_same_identity():
    assert mi.identities_match("gpt-5", "openai", "gpt-5", "anthropic") is False


def test_same_model_same_provider_matches():
    assert mi.identities_match("gpt-5", "openai", "gpt-5", "openai") is True


def test_qualified_stored_model_matches_bare_resolved_lane():
    assert mi.identities_match("@openai:gpt-5", None, "gpt-5", "openai") is True


def test_legacy_bare_stored_model_matches_any_provider():
    # Pre-provider sessions stored a bare id; that must not read as a change.
    assert mi.identities_match("gpt-5", None, "gpt-5", "anthropic") is True


def test_legacy_bare_stored_model_with_other_provider_is_a_change():
    assert mi.identities_match("gpt-5", "openai", "gpt-5", "anthropic") is False


def test_slash_prefix_only_when_caller_opts_in():
    # `vendor/model` is indistinguishable from `provider/model`; only callers
    # that already treat the prefix as a provider may split it.
    assert mi.split_model_identity("openai/gpt-5").model == "openai/gpt-5"
    split = mi.split_model_identity("openai/gpt-5", allow_slash_prefix=True)
    assert (split.model, split.provider) == ("gpt-5", "openai")


def test_provider_alias_normalizer_is_honored():
    assert (
        mi.identities_match(
            "gpt-5",
            "codex",
            "gpt-5",
            "openai-codex",
            provider_normalizer=lambda p: "openai-codex" if p == "codex" else p,
        )
        is True
    )


def test_non_provider_sentinels_are_no_provider():
    for sentinel in (None, "", "  ", "auto", "default"):
        assert mi.normalize_provider_id(sentinel) is None


# ── routes boundary helpers ──────────────────────────────────────────────────

def test_session_state_none_means_untouched():
    assert routes._session_model_state_from_request(None, None) == (None, None)


def test_session_state_blank_model_is_refused():
    with pytest.raises(mi.ModelIdentityError):
        routes._session_model_state_from_request("", "openai")


def test_session_state_keeps_vendor_slash():
    model, provider = routes._session_model_state_from_request("openai/gpt-5", "openai")
    assert model == "openai/gpt-5"


def test_session_state_keeps_route_suffix():
    model, _ = routes._session_model_state_from_request("qwen3:free", "openrouter")
    assert model == "qwen3:free"


def test_session_state_route_hint_wins_over_body_provider():
    # The picker shape keeps the qualified string; the provider state must come
    # from the hint, not from a conflicting body field.
    model, provider = routes._session_model_state_from_request("@openai:gpt-5", "anthropic")
    assert provider == "openai"
    assert mi.split_model_identity(model, provider) == mi.ModelIdentity("gpt-5", "openai")


def test_session_state_empty_tail_is_refused():
    with pytest.raises(mi.ModelIdentityError):
        routes._session_model_state_from_request("@custom:plusvibe-api:", "custom:plusvibe-api")


def test_requested_model_allows_blank_for_new_session():
    # A brand-new session has no model yet and inherits the profile default.
    assert routes._require_requested_model("") == ""
    assert routes._require_requested_model(None) is None


@pytest.mark.parametrize("value", [123, {"model": "gpt-5"}])
def test_requested_model_refuses_non_string(value):
    with pytest.raises(mi.ModelIdentityError):
        routes._require_requested_model(value)


def test_bad_model_identity_response_carries_diagnostics(monkeypatch):
    err = mi.ModelIdentityError("model must not be empty", field="model", code="model_empty")
    captured = {}

    def _fake_j(handler, payload, status=200, extra_headers=None, **kwargs):
        captured.update(payload=payload, status=status)

    monkeypatch.setattr(routes, "j", _fake_j)
    routes._bad_model_identity(object(), err)
    assert captured["status"] == 400
    assert captured["payload"]["error"] == "model must not be empty"
    assert captured["payload"]["code"] == "model_empty"
    assert captured["payload"]["field"] == "model"
