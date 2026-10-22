"""Reasoning runtime parity: writer, status and the streaming runtime agree.

The three call sites that decide "what reasoning level is in force" used to
disagree:

* the pin writer stored a bare model key,
* ``GET /api/reasoning`` looked the pin up by bare model name,
* the runtime looked it up again with its own inline copy of the rules.

Consequences: a level pinned for ``gpt-5`` on one provider silently applied to
another provider's ``gpt-5``, and a provider-qualified key written by a newer
build was invisible to the status/runtime lookups.

These tests pin the single shared resolution (``api.model_identity``) plus the
provider-aware pin key, and keep the legacy bare keys readable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import api.config as cfg
import api.model_identity as mi
import api.streaming as streaming


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the config read/write helpers at a throwaway YAML file.

    The live profile config must never be touched by tests.
    """
    path = tmp_path / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: path)
    monkeypatch.setattr(cfg, "reload_config", lambda: None)
    return path


def _write_config(path, data):
    import yaml

    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _read_config(path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── pin key shape ───────────────────────────────────────────────────────────

def test_pin_key_is_provider_qualified_when_provider_known():
    assert mi.reasoning_pin_key("gpt-5", "openai") == "@openai:gpt-5"


def test_pin_key_is_bare_without_provider():
    assert mi.reasoning_pin_key("gpt-5", None) == "gpt-5"
    assert mi.reasoning_pin_key("gpt-5", "auto") == "gpt-5"


def test_pin_candidates_are_most_specific_first():
    assert mi.reasoning_pin_candidates("gpt-5", "openai") == ["@openai:gpt-5", "gpt-5"]


def test_pin_candidates_do_not_guess_another_provider():
    # A bare model must not produce candidates for providers it wasn't given.
    assert "@anthropic:gpt-5" not in mi.reasoning_pin_candidates("gpt-5", "openai")


# ── same model, different provider ──────────────────────────────────────────

def test_qualified_pin_does_not_leak_across_providers():
    overrides = {"@openai:gpt-5": "high"}
    assert mi.match_reasoning_pin(overrides, "gpt-5", "openai") == ("@openai:gpt-5", "high")
    assert mi.match_reasoning_pin(overrides, "gpt-5", "anthropic") == (None, None)


def test_legacy_bare_pin_still_applies_to_any_provider():
    # Old builds wrote bare keys; they carry no provider, so they stay readable
    # everywhere rather than silently disappearing after an upgrade.
    overrides = {"gpt-5": "low"}
    assert mi.match_reasoning_pin(overrides, "gpt-5", "openai") == ("gpt-5", "low")
    assert mi.match_reasoning_pin(overrides, "gpt-5", "anthropic") == ("gpt-5", "low")


def test_qualified_pin_wins_over_legacy_bare_pin():
    overrides = {"@openai:gpt-5": "high", "gpt-5": "low"}
    assert mi.match_reasoning_pin(overrides, "gpt-5", "openai") == ("@openai:gpt-5", "high")
    # …while another provider still falls back to the bare key.
    assert mi.match_reasoning_pin(overrides, "gpt-5", "anthropic") == ("gpt-5", "low")


def test_pin_lookup_is_spelling_tolerant():
    # The core already treats `-`/`_`/`.`/space as the same model spelling.
    assert mi.match_reasoning_pin({"gpt-5-4": "low"}, "gpt-5.4", "openai") == ("gpt-5-4", "low")


def test_qualified_model_id_resolves_its_own_provider():
    overrides = {"@openai:gpt-5": "low"}
    assert mi.match_reasoning_pin(overrides, "@openai:gpt-5", None) == ("@openai:gpt-5", "low")


# ── session > pin > global ──────────────────────────────────────────────────

def test_session_beats_pin_and_global():
    res = mi.resolve_reasoning_effort(
        session_effort="minimal",
        overrides={"@openai:gpt-5": "high"},
        global_effort="low",
        model_id="gpt-5",
        provider="openai",
    )
    assert res["effort"] == "minimal"
    assert res["source"] == "session"


def test_pin_beats_global():
    res = mi.resolve_reasoning_effort(
        session_effort=None,
        overrides={"@openai:gpt-5": "high"},
        global_effort="low",
        model_id="gpt-5",
        provider="openai",
    )
    assert res["effort"] == "high"
    assert res["source"] == "model"
    assert res["pin_key"] == "@openai:gpt-5"


def test_global_applies_when_pin_belongs_to_another_provider():
    res = mi.resolve_reasoning_effort(
        session_effort=None,
        overrides={"@openai:gpt-5": "high"},
        global_effort="low",
        model_id="gpt-5",
        provider="anthropic",
    )
    assert res["effort"] == "low"
    assert res["source"] == "default"
    assert res["pin_key"] is None


def test_nothing_set_resolves_to_none():
    res = mi.resolve_reasoning_effort(
        session_effort=None,
        overrides={},
        global_effort=None,
        model_id="gpt-5",
        provider="openai",
    )
    assert res["effort"] is None
    assert res["source"] == "default"


# ── runtime: streaming reads the same resolution ────────────────────────────

def test_runtime_resolution_is_provider_aware():
    runtime_cfg = {"agent": {"reasoning_effort": "low", "reasoning_overrides": {"@openai:gpt-5": "high"}}}
    session = SimpleNamespace(reasoning_effort=None)

    openai_res = streaming._resolve_turn_reasoning_effort(runtime_cfg, session, "gpt-5", "openai")
    anthropic_res = streaming._resolve_turn_reasoning_effort(runtime_cfg, session, "gpt-5", "anthropic")

    assert (openai_res["effort"], openai_res["source"]) == ("high", "model")
    assert (anthropic_res["effort"], anthropic_res["source"]) == ("low", "default")


def test_runtime_session_override_beats_model_pin():
    runtime_cfg = {"agent": {"reasoning_effort": "low", "reasoning_overrides": {"@openai:gpt-5": "high"}}}
    session = SimpleNamespace(reasoning_effort="medium")

    res = streaming._resolve_turn_reasoning_effort(runtime_cfg, session, "gpt-5", "openai")
    assert (res["effort"], res["source"]) == ("medium", "session")


def test_runtime_handles_missing_and_malformed_config():
    for bad_cfg in ({}, None, {"agent": "nonsense"}, {"agent": {"reasoning_overrides": ["x"]}}):
        res = streaming._resolve_turn_reasoning_effort(bad_cfg, None, "gpt-5", "openai")
        assert res["effort"] is None
        assert res["source"] == "default"


# ── writer / status parity (real config round-trip) ─────────────────────────

def test_writer_stores_qualified_key_and_status_reads_it(isolated_config):
    cfg.set_model_reasoning_override("high", model_id="gpt-5", provider_id="openai")

    stored = _read_config(isolated_config)["agent"]["reasoning_overrides"]
    assert stored == {"@openai:gpt-5": "high"}

    status = cfg.get_reasoning_status(model_id="gpt-5", provider_id="openai")
    assert status["reasoning_effort"] == "high"
    assert status["source"] == "model"
    assert status["reasoning_pin_key"] == "@openai:gpt-5"


def test_status_ignores_pin_from_another_provider(isolated_config):
    _write_config(
        isolated_config,
        {"agent": {"reasoning_effort": "low", "reasoning_overrides": {"@openai:gpt-5": "high"}}},
    )

    same_lane = cfg.get_reasoning_status(model_id="gpt-5", provider_id="openai")
    other_lane = cfg.get_reasoning_status(model_id="gpt-5", provider_id="anthropic")

    assert (same_lane["reasoning_effort"], same_lane["source"]) == ("high", "model")
    assert (other_lane["reasoning_effort"], other_lane["source"]) == ("low", "default")
    assert other_lane["reasoning_pin_key"] is None


def test_status_reads_legacy_bare_key(isolated_config):
    _write_config(isolated_config, {"agent": {"reasoning_overrides": {"gpt-5": "minimal"}}})

    status = cfg.get_reasoning_status(model_id="gpt-5", provider_id="openai")
    assert status["reasoning_effort"] == "minimal"
    assert status["reasoning_pin_key"] == "gpt-5"


def test_two_providers_keep_two_levels(isolated_config):
    cfg.set_model_reasoning_override("high", model_id="gpt-5", provider_id="openai")
    cfg.set_model_reasoning_override("low", model_id="gpt-5", provider_id="anthropic")

    stored = _read_config(isolated_config)["agent"]["reasoning_overrides"]
    assert stored == {"@openai:gpt-5": "high", "@anthropic:gpt-5": "low"}

    assert cfg.get_reasoning_status(model_id="gpt-5", provider_id="openai")["reasoning_effort"] == "high"
    assert cfg.get_reasoning_status(model_id="gpt-5", provider_id="anthropic")["reasoning_effort"] == "low"


def test_writer_replaces_near_duplicate_spelling_of_same_lane(isolated_config):
    # Same lane, different spelling of the same model → one key survives.
    _write_config(isolated_config, {"agent": {"reasoning_overrides": {"@openai:gpt-5-4": "low"}}})

    cfg.set_model_reasoning_override("high", model_id="gpt-5.4", provider_id="openai")
    stored = _read_config(isolated_config)["agent"]["reasoning_overrides"]

    assert stored == {"@openai:gpt-5.4": "high"}


def test_writer_leaves_legacy_bare_key_for_other_lanes(isolated_config):
    # A bare key is the any-provider fallback: writing the openai pin must not
    # delete the only level other providers can still read.
    _write_config(isolated_config, {"agent": {"reasoning_overrides": {"gpt-5": "low"}}})

    cfg.set_model_reasoning_override("high", model_id="gpt-5", provider_id="openai")
    stored = _read_config(isolated_config)["agent"]["reasoning_overrides"]

    assert stored == {"gpt-5": "low", "@openai:gpt-5": "high"}
    assert cfg.get_reasoning_status(model_id="gpt-5", provider_id="openai")["reasoning_effort"] == "high"
    assert cfg.get_reasoning_status(model_id="gpt-5", provider_id="anthropic")["reasoning_effort"] == "low"


def test_writer_empty_effort_removes_pin(isolated_config):
    cfg.set_model_reasoning_override("high", model_id="gpt-5", provider_id="openai")
    cfg.set_model_reasoning_override("", model_id="gpt-5", provider_id="openai")

    assert "reasoning_overrides" not in _read_config(isolated_config).get("agent", {})


def test_writer_refuses_unusable_model_id(isolated_config):
    with pytest.raises(mi.ModelIdentityError):
        cfg.set_model_reasoning_override("high", model_id="   ", provider_id="openai")
    with pytest.raises(mi.ModelIdentityError):
        cfg.set_model_reasoning_override("high", model_id="@custom:plusvibe-api:", provider_id="custom:plusvibe-api")
    # Nothing was written.
    assert "reasoning_overrides" not in _read_config(isolated_config).get("agent", {})


def test_writer_refuses_unknown_effort(isolated_config):
    with pytest.raises(ValueError):
        cfg.set_model_reasoning_override("turbo", model_id="gpt-5", provider_id="openai")


# ── status diagnostics for the JS integrator ────────────────────────────────

def test_status_exposes_identity_diagnostics(isolated_config):
    _write_config(
        isolated_config,
        {
            "agent": {"reasoning_effort": "low", "reasoning_overrides": {"@openai:gpt-5": "high"}},
            "model": {"default": "gpt-5", "provider": "openai"},
        },
    )

    status = cfg.get_reasoning_status(model_id="gpt-5", provider_id="openai")

    assert status["model"] == "gpt-5"
    assert status["provider"] == "openai"
    assert status["requested_model"] == "gpt-5"
    assert status["requested_provider"] == "openai"
    assert status["reasoning_pin_key"] == "@openai:gpt-5"
    assert status["reasoning_pin_candidates"] == ["@openai:gpt-5", "gpt-5"]
    assert status["global_reasoning_effort"] == "low"
    assert status["reasoning_pin_effort"] == "high"


def test_status_accepts_qualified_picker_value(isolated_config):
    # The picker sends `@openai:gpt-5` for a provider-group row. Status must
    # report the same lane the runtime will use, and find the pin by it.
    _write_config(
        isolated_config,
        {
            "agent": {"reasoning_effort": "low", "reasoning_overrides": {"@openai:gpt-5": "high"}},
            "model": {"default": "gpt-5", "provider": "openai"},
        },
    )

    status = cfg.get_reasoning_status(model_id="@openai:gpt-5")

    assert status["model"] == "gpt-5"
    assert status["provider"] == "openai"
    assert status["requested_model"] == "@openai:gpt-5"
    assert status["reasoning_effort"] == "high"
    assert status["source"] == "model"
    assert status["reasoning_pin_key"] == "@openai:gpt-5"
