"""Durable fallback attribution: the server-side half.

The client marker (``static/turn_model_attribution.js``) survives re-render,
reconnect and the transcript replace, but not a page reload — the server never
persisted it. ``api/streaming.py`` now stamps ``_turnModelAttribution`` next to
``_usedModel`` at turn settle, and ``_SESSION_MESSAGE_DISPLAY_METADATA_KEYS``
carries it through the transcript merge.

Behaviour under test (narrow, end to end inside this process):

1. The marker is built ONLY from observed state. No notice, no lane change ->
   no marker at all; nothing is inferred from the picker or defaulted.
2. A real fallback records the requested lane (canonical named-custom provider,
   never the SDK's bare ``custom``), the effective lane, the agent's own reason
   and retry cooldown, and the reasoning level that was in force.
3. The stamped marker survives the display-metadata merge that a reload path
   runs, so a hydrated transcript can still render it.
"""

from __future__ import annotations

import copy

import api.models as models
import api.streaming as streaming


NAMED = "custom:plusvibe-api"
REQUESTED_MODEL = "gpt-6.1-sol"
FALLBACK_MODEL = "deepseek-v4.1-flash"
NOTICE = (
    "\u26a0\ufe0f Model fallback: gpt-6.1-sol via custom:plusvibe-api unavailable "
    "(rate limit exceeded); using deepseek-v4.1-flash via custom:plusvibe-api. "
    "Primary retry eligible in ~30 s; requests go to the fallback lane until then."
)


# ── 1. no evidence, no marker ───────────────────────────────────────────────

def test_no_marker_without_any_fallback_evidence():
    """A clean turn on the same lane must not be labelled a fallback."""
    assert streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=REQUESTED_MODEL,
        effective_provider="custom",
        notice=None,
    ) is None


def test_transport_rewrite_alone_is_not_a_fallback():
    """``custom:<slug>`` -> ``custom`` is the SDK rewrite, not a lane change."""
    assert streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=REQUESTED_MODEL,
        effective_provider="custom",
        notice="",
    ) is None


# ── 2. real fallback state -> exact marker ──────────────────────────────────

def test_marker_records_real_fallback_facts():
    marker = streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=FALLBACK_MODEL,
        effective_provider="custom",
        notice=NOTICE,
        reasoning_effort="high",
        reasoning_source="model",
    )
    assert marker is not None
    assert marker["v"] == 1
    assert marker["kind"] == "fallback"
    assert marker["source"] == "server_fallback_state"
    # Canonical lane, NOT the transport ``custom`` that broke pin/guard.
    assert marker["requested_model"] == REQUESTED_MODEL
    assert marker["requested_provider"] == NAMED
    assert marker["effective_model"] == FALLBACK_MODEL
    # The agent did not name a different provider, so none is claimed.
    assert "effective_provider" not in marker
    assert marker["reason"] == "rate limit exceeded"
    assert marker["retry_seconds"] == 30.0
    assert marker["reasoning_effort"] == "high"
    assert marker["substituted"] is True


def test_marker_omits_reason_when_the_notice_does_not_match():
    """An unmatched notice records lanes only — never an invented reason."""
    marker = streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=FALLBACK_MODEL,
        effective_provider="custom",
        notice="\u26a0\ufe0f Rate limited — switching to fallback provider...",
    )
    assert marker is not None
    assert "reason" not in marker
    assert "retry_seconds" not in marker
    assert marker["effective_model"] == FALLBACK_MODEL


def test_marker_records_a_proven_cross_provider_fallback():
    """A different provider is recorded only when the agent actually says so."""
    marker = streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=FALLBACK_MODEL,
        effective_provider="openrouter",
        notice=NOTICE,
    )
    assert marker["effective_provider"] == "openrouter"
    assert marker["substituted"] is True


def test_marker_has_no_reasoning_when_the_turn_had_none():
    marker = streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=FALLBACK_MODEL,
        effective_provider="custom",
        notice=NOTICE,
    )
    assert "reasoning_effort" not in marker
    assert "reasoning" not in marker


def test_marker_renders_from_the_client_module_shape():
    """The nested lanes the client renderer reads are present and consistent."""
    marker = streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=FALLBACK_MODEL,
        effective_provider="custom",
        notice=NOTICE,
        reasoning_effort="high",
    )
    assert marker["requested"] == {
        "model": REQUESTED_MODEL,
        "provider": NAMED,
        "source": "server_fallback_state",
    }
    assert marker["effective"] == {
        "model": FALLBACK_MODEL,
        "source": "server_fallback_state",
    }
    assert marker["reasoning"] == {"effort": "high"}
    assert marker["retry"] == "30s"
    assert isinstance(marker["at"], int)
    assert marker["message"] == NOTICE


# ── 3. the stamp survives the reload merge ─────────────────────────────────

def test_stamped_marker_survives_the_display_metadata_merge():
    """The reload path copies allowlisted keys onto the transcript message."""
    marker = streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=FALLBACK_MODEL,
        effective_provider="custom",
        notice=NOTICE,
    )
    stored = {
        "role": "assistant",
        "content": "answer from the fallback lane",
        "_usedModel": FALLBACK_MODEL,
        "_turnModelAttribution": marker,
    }
    # Fresh transcript object as a reload would build it: content only.
    hydrated = {"role": "assistant", "content": stored["content"]}
    models._merge_session_display_metadata(hydrated, stored)

    assert hydrated["_turnModelAttribution"] == marker
    assert hydrated["_turnModelAttribution"] is not stored["_turnModelAttribution"]
    assert hydrated["_usedModel"] == FALLBACK_MODEL


def test_marker_survives_a_json_round_trip():
    """Display metadata is persisted as JSON — the marker must stay plain data."""
    import json

    marker = streaming.build_turn_model_attribution(
        requested_model=REQUESTED_MODEL,
        requested_provider=NAMED,
        effective_model=FALLBACK_MODEL,
        effective_provider="custom",
        notice=NOTICE,
        reasoning_effort="high",
    )
    revived = json.loads(json.dumps(copy.deepcopy(marker)))
    assert revived == marker
    assert revived["kind"] == "fallback"
