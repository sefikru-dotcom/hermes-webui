"""Contracts for localized activity/status copy and context indicator UX.

The activity renderer and shell markup are intentionally out of scope here; these
source-level checks keep the shared i18n vocabulary and the existing accessibility
contract from regressing without modifying those protected files.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def test_activity_stream_tool_context_statuses_have_i18n_keys():
    required = (
        "activity_status_running", "activity_status_done", "activity_status_error",
        "stream_status_running", "stream_status_done", "stream_status_error",
        "tool_status_running", "tool_status_done", "tool_status_error",
        "thinking_status", "clarification_status", "error_status",
        "context_usage_not_available", "context_window_not_resolved",
    )
    for key in required:
        assert f"{key}:" in I18N, f"missing i18n key: {key}"


def test_context_indicator_never_invents_window_or_percentage():
    assert "128*1024" not in UI.replace(" ", "")
    assert "constctxWindow=Number(usage.context_length)>0?Number(usage.context_length):0" in UI.replace(" ", "")
    assert "Usage: not available yet" in UI
    assert "center.textContent=hasPromptTok?String(pct):'\\u00b7'" in UI.replace(" ", "")


def test_context_indicator_requires_backend_usage_for_numeric_data():
    assert "consthasKnownContext=ctxWindow>0" in UI.replace(" ", "")
    assert "hasPromptTok=hasPromptTok&&hasKnownContext" in UI.replace(" ", "")
    assert "context_length" in UI


def test_activity_renderers_use_real_localized_interpolation():
    assert '" + t("activity_status_running") + "' not in UI
    assert '" + t("activity_tokens") + "' not in UI
    assert 'static/chat_activity.js' in (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_a11y_contract_has_disclosures_and_prompt_live_regions_but_no_token_stream_live_region():
    # Existing activity/tool disclosures and prompt dialogs must remain named.
    assert "tool-call-group-summary" in UI
    assert "aria-expanded" in UI
    # Clarification is a prompt surface; its existing dialog contract remains
    # in the protected message renderer and must stay named.
    assert "clarifyQuestion" in MESSAGES
    assert "aria-labelledby" in MESSAGES
    # Token streaming is deliberately not an aria-live region: announcing every
    # token makes screen readers unusable. Live announcements belong to status UI.
    for line in UI.splitlines():
        if "token" in line.lower() and "aria-live" in line.lower():
            raise AssertionError(f"aria-live attached to token stream: {line}")
