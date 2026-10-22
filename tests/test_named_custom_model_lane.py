"""Named custom providers (``custom:<slug>``) keep their lane end to end.

A named custom provider is a first-class lane: the session stores
``custom:plusvibe-api``, the reasoning pin is keyed ``@custom:plusvibe-api:<model>``,
and the context guard compares the session's lane with the turn's. The SDK
transport, however, is built with bare ``custom`` — ``_resolve_custom_provider_
runtime_overrides`` resolves the slug to its endpoint and rewrites the provider
id. Passing that transport id back into identity decisions made a named-custom
session compare unequal to its OWN turn, which silently dropped the reasoning
pin and every context-metadata write.

These tests drive the real helpers in the order the runtime does
(request -> session state -> turn resolution -> pin lookup -> context guard ->
error attribution) with the provider ids this deployment actually uses, so a
future rewrite of the transport value cannot regress the identity path.
"""

from __future__ import annotations

import api.model_identity as mi
import api.streaming as streaming
from api import config as cfg_mod
from api import routes as routes_mod


NAMED = "custom:plusvibe-api"
NAMED_OTHER = "custom:vertex-gemini"
MODEL = "gpt-6.1-sol"


# ── turn identity: canonical requested provider wins over the transport ─────

def test_turn_provider_identity_keeps_named_custom_slug():
    """The SDK-rewritten transport id must not replace the canonical lane."""
    assert streaming._turn_provider_identity(NAMED, "custom") == NAMED
    assert streaming._turn_provider_identity(NAMED_OTHER, "custom") == NAMED_OTHER


def test_turn_provider_identity_degrades_to_transport_when_request_unknown():
    assert streaming._turn_provider_identity(None, "custom") == "custom"
    assert streaming._turn_provider_identity("", "openai") == "openai"


# ── context-metadata guard on a named-custom session ────────────────────────

def test_guard_allows_named_custom_session_against_its_own_turn():
    """Regression: the rewrite used to make this False and skip every write."""
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model=MODEL,
        session_provider=NAMED,
        turn_model=MODEL,
        turn_provider=streaming._turn_provider_identity(NAMED, "custom"),
    )
    assert allowed is True, reason


def test_guard_blocks_a_different_named_custom_lane():
    """Two named custom providers are two lanes, even on the same model id."""
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model=MODEL,
        session_provider=NAMED_OTHER,
        turn_model=MODEL,
        turn_provider=streaming._turn_provider_identity(NAMED, "custom"),
    )
    assert allowed is False
    assert NAMED_OTHER in reason and NAMED in reason


def test_guard_allows_vendor_slash_model_on_named_custom_lane():
    """``google/...`` is a model id on a custom lane, not a provider hint."""
    slash_model = "google/gemini-3-flash-preview"
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model=slash_model,
        session_provider=NAMED,
        turn_model=slash_model,
        turn_provider=streaming._turn_provider_identity(NAMED, "custom"),
    )
    assert allowed is True, reason


def test_guard_still_matches_legacy_vendor_slash_under_matching_provider():
    """The historical tolerance survives: prefix == provider still matches."""
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model="deepseek/deepseek-v4-1m",
        session_provider="deepseek",
        turn_model="deepseek-v4-1m",
        turn_provider="deepseek",
    )
    assert allowed is True, reason


def test_guard_still_blocks_a_real_mid_turn_switch():
    """The guard must keep doing its job for a genuine lane change."""
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model="gemini-3.8-flash",
        session_provider="gemini",
        turn_model=MODEL,
        turn_provider=streaming._turn_provider_identity(NAMED, "custom"),
    )
    assert allowed is False
    assert "gemini" in reason


# ── request -> session state ────────────────────────────────────────────────

def test_session_state_keeps_named_custom_provider():
    model, provider = routes_mod._session_model_state_from_request(MODEL, NAMED)
    assert model == MODEL
    assert provider == NAMED


def test_session_state_keeps_named_custom_from_qualified_hint():
    """A qualified hint is stored verbatim; the provider is extracted from it."""
    model, provider = routes_mod._session_model_state_from_request(
        f"@{NAMED}:{MODEL}", None
    )
    assert model == f"@{NAMED}:{MODEL}"
    assert provider == NAMED


def test_qualified_hint_session_matches_the_bare_turn_model():
    """The stored hint and the resolved bare id are the same lane."""
    stored_model, stored_provider = routes_mod._session_model_state_from_request(
        f"@{NAMED}:{MODEL}", None
    )
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model=stored_model,
        session_provider=stored_provider,
        turn_model=MODEL,
        turn_provider=streaming._turn_provider_identity(NAMED, "custom"),
    )
    assert allowed is True, reason


def test_blank_model_update_keeps_provider_and_does_not_raise():
    """Legacy ``model: ""`` from a workspace/profile switch stays accepted."""
    model, provider = routes_mod._session_model_state_from_request("", NAMED)
    assert model == ""
    assert provider == NAMED


def test_blank_model_update_still_rejects_a_broken_qualified_hint():
    """Relaxing the blank shape must not relax identity validation itself."""
    import pytest

    with pytest.raises(mi.ModelIdentityError):
        routes_mod._session_model_state_from_request(f"@{NAMED}:", NAMED)


# ── reasoning pin written for the named lane is found by the runtime ────────

def _pin_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent: {}\n", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "_get_config_path", lambda: config_path)
    return config_path


def test_pin_written_for_named_custom_is_resolved_at_runtime(tmp_path, monkeypatch):
    """The picker's inline effort must survive into the turn that runs it."""
    _pin_config(tmp_path, monkeypatch)
    cfg_mod.set_model_reasoning_override("high", model_id=MODEL, provider_id=NAMED)
    cfg = cfg_mod.get_model_reasoning_overrides()
    assert cfg, "writer persisted no pin"

    resolution = streaming._resolve_turn_reasoning_effort(
        {"agent": {"reasoning_overrides": cfg, "reasoning_effort": "low"}},
        None,
        MODEL,
        streaming._turn_provider_identity(NAMED, "custom"),
    )
    assert resolution["effort"] == "high"
    assert resolution["source"] == "model"
    assert resolution["pin_key"] == f"@{NAMED}:{MODEL}"


def test_pin_lookup_uses_the_canonical_key_not_the_transport_one():
    """Documents WHY the canonical provider has to reach the pin lookup."""
    overrides = {mi.reasoning_pin_key(MODEL, NAMED): "high"}
    canonical = streaming._resolve_turn_reasoning_effort(
        {"agent": {"reasoning_overrides": overrides, "reasoning_effort": "low"}},
        None,
        MODEL,
        NAMED,
    )
    transport = streaming._resolve_turn_reasoning_effort(
        {"agent": {"reasoning_overrides": overrides, "reasoning_effort": "low"}},
        None,
        MODEL,
        "custom",
    )
    assert canonical["effort"] == "high"
    # Bare ``custom`` is a different key: this is the degradation the runtime
    # must never hand to the resolver.
    assert transport["effort"] == "low"
    assert transport["pin_key"] is None


# ── error attribution ──────────────────────────────────────────────────────

def test_error_diagnostics_do_not_call_the_same_named_lane_substituted():
    diag = streaming._model_identity_diagnostics(
        model=MODEL,
        provider=streaming._turn_provider_identity(NAMED, "custom"),
        requested_model=f"@{NAMED}:{MODEL}",
        requested_provider=NAMED,
    )
    assert diag["provider"] == NAMED
    assert diag["requested_provider"] == NAMED
    assert "model_substituted" not in diag
    assert "provider_substituted" not in diag


def test_error_diagnostics_flag_a_real_cross_lane_fallback():
    diag = streaming._model_identity_diagnostics(
        model="deepseek-v4.1-flash",
        provider=NAMED,
        requested_model=f"@{NAMED}:{MODEL}",
        requested_provider=NAMED,
    )
    assert diag["model_substituted"] is True
    assert "provider_substituted" not in diag


def test_error_diagnostics_flag_a_provider_switch_between_named_lanes():
    diag = streaming._model_identity_diagnostics(
        model=MODEL,
        provider=NAMED_OTHER,
        requested_model=f"@{NAMED}:{MODEL}",
        requested_provider=NAMED,
    )
    assert diag["provider_substituted"] is True
