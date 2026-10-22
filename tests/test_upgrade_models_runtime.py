"""Runtime model identity: context-metadata guard and error diagnostics.

Two runtime behaviours that need model identity to be provider-aware:

1. **Context metadata from a superseded turn.** A turn resolves its model, then
   streams for a while. If the session is pointed at a different model/provider
   mid-stream (another tab, ``POST /api/session/update``), the finishing turn
   must not stamp its context window / threshold / prompt-token snapshot onto
   the new lane.

2. **Error payload attribution.** When a provider call fails, the payload must
   say which model/provider lane was used, which was requested, and whether a
   fallback was in play — otherwise a failure on a substituted lane looks
   identical to a failure on the requested one.
"""

from __future__ import annotations

import api.model_identity as mi
import api.streaming as streaming


# ── context-metadata identity guard ─────────────────────────────────────────

def test_guard_allows_write_for_same_lane():
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model="gpt-5",
        session_provider="openai",
        turn_model="gpt-5",
        turn_provider="openai",
    )
    assert allowed is True
    assert reason == ""


def test_guard_blocks_write_when_provider_changed_mid_turn():
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model="gpt-5",
        session_provider="anthropic",
        turn_model="gpt-5",
        turn_provider="openai",
    )
    assert allowed is False
    assert "openai" in reason and "anthropic" in reason


def test_guard_blocks_write_when_model_changed_mid_turn():
    allowed, _ = streaming._context_metadata_identity_guard(
        session_model="claude-sonnet-4",
        session_provider="anthropic",
        turn_model="gpt-5",
        turn_provider="openai",
    )
    assert allowed is False


def test_guard_allows_write_for_blank_session_model():
    # No identity yet (brand-new session) — pre-existing shape stays writable.
    allowed, _ = streaming._context_metadata_identity_guard(
        session_model="",
        session_provider=None,
        turn_model="gpt-5",
        turn_provider="openai",
    )
    assert allowed is True


def test_guard_allows_write_for_qualified_stored_model():
    allowed, _ = streaming._context_metadata_identity_guard(
        session_model="@openai:gpt-5",
        session_provider=None,
        turn_model="gpt-5",
        turn_provider="openai",
    )
    assert allowed is True


def test_guard_allows_write_for_legacy_bare_session_model():
    allowed, _ = streaming._context_metadata_identity_guard(
        session_model="gpt-5",
        session_provider=None,
        turn_model="gpt-5",
        turn_provider="openai",
    )
    assert allowed is True


def test_guard_resolves_provider_aliases():
    # `claude` is an alias of `anthropic`: same lane, so the guard must not fire
    # on a spelling difference alone.
    allowed, _ = streaming._context_metadata_identity_guard(
        session_model="claude-sonnet-4",
        session_provider="claude",
        turn_model="claude-sonnet-4",
        turn_provider="anthropic",
    )
    assert allowed is True


def test_guard_never_raises_on_garbage():
    allowed, reason = streaming._context_metadata_identity_guard(
        session_model=object(),
        session_provider=object(),
        turn_model=object(),
        turn_provider=object(),
    )
    assert isinstance(allowed, bool)
    assert isinstance(reason, str)


# ── error payload diagnostics ───────────────────────────────────────────────

def test_error_payload_has_no_identity_by_default():
    payload = streaming._provider_error_payload("boom", "error")
    assert payload == {"message": "boom", "type": "error", "details": "boom"}


def test_error_payload_carries_model_identity():
    identity = streaming._model_identity_diagnostics(
        model="gpt-5",
        provider="openai",
    )
    payload = streaming._provider_error_payload("boom", "error", identity=identity)

    assert payload["model_identity"] == {"model": "gpt-5", "provider": "openai"}
    # Flat mirrors for existing clients / log greps.
    assert payload["model"] == "gpt-5"
    assert payload["provider"] == "openai"


def test_diagnostics_flag_substituted_lane():
    diag = streaming._model_identity_diagnostics(
        model="gpt-5-mini",
        provider="openai",
        requested_model="gpt-5",
        requested_provider="anthropic",
    )
    assert diag["model_substituted"] is True
    assert diag["provider_substituted"] is True


def test_diagnostics_no_substitution_flag_when_lane_matches():
    diag = streaming._model_identity_diagnostics(
        model="gpt-5",
        provider="openai",
        requested_model="gpt-5",
        requested_provider="openai",
    )
    assert "model_substituted" not in diag
    assert "provider_substituted" not in diag


def test_diagnostics_expose_reasoning_and_fallback():
    diag = streaming._model_identity_diagnostics(
        model="gpt-5",
        provider="openai",
        reasoning_resolution={
            "effort": "high",
            "source": "model",
            "pin_key": "@openai:gpt-5",
        },
        fallback="activated",
        turn_model="gpt-5",
        turn_provider="openai",
    )
    assert diag["reasoning_effort"] == "high"
    assert diag["reasoning_source"] == "model"
    assert diag["reasoning_pin_key"] == "@openai:gpt-5"
    assert diag["fallback"] == "activated"
    assert diag["turn_model"] == "gpt-5"
    assert diag["turn_provider"] == "openai"


def test_diagnostics_drop_empty_values():
    diag = streaming._model_identity_diagnostics(
        model="gpt-5",
        provider="",
        requested_model="   ",
        reasoning_resolution=None,
        fallback=None,
    )
    assert diag == {"model": "gpt-5"}


def test_error_payload_keeps_redaction_and_truncation():
    payload = streaming._provider_error_payload(
        "x" * 5000,
        "error",
        identity={"model": "gpt-5", "provider": "openai"},
    )
    assert len(payload["details"]) <= 1200
    assert payload["model_identity"]["provider"] == "openai"
