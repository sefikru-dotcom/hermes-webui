"""Behavioural tests for boot-time chat mode classes (layout zone).

The harness extracts the real functions from static/boot.js and static/ui.js and
executes them against a fake <html>, so these assertions are about the classes
that actually land on the document, not about source text. The text checks at
the end only pin wiring that cannot be observed without a DOM: both settings
paths (success and failure) must run the same projection.

Run:
    HOME=$(mktemp -d) HERMES_HOME=$(mktemp -d) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
      /opt/hermes-webui/.venv/bin/python3 -m pytest tests/test_upgrade_layout_modes.py -q
"""

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOT = ROOT / "static" / "boot.js"
HARNESS = ROOT / "scripts" / "test_upgrade_layout_chat_modes.js"


@pytest.fixture(scope="module")
def harness_output():
    if shutil.which("node") is None:
        pytest.skip("node is required for the mode harness")
    result = subprocess.run(
        ["node", str(HARNESS)],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_mode_class_harness_passes(harness_output):
    assert "PASS" in harness_output
    assert "FAIL" not in harness_output


def test_both_settings_paths_project_the_same_classes():
    source = BOOT.read_text(encoding="utf-8")
    calls = source.count("_applyBootChatUxSettings();")
    assert calls == 2, f"expected one projection per settings path, found {calls} calls"
    success_block = source.split("window._thinkingMode = _effectiveChatThinkingMode")[1]
    assert "_applyBootChatUxSettings();" in success_block.split("window._workspaceTodosTab")[0]
    failure_block = source.split("window._chatActivityDisplayMode='stream_window';")[1]
    assert "_applyBootChatUxSettings();" in failure_block.split("window._workspaceTodosTab")[0], (
        "settings-fetch failure must still apply the mode classes"
    )


def test_single_projection_replaces_ad_hoc_toggles():
    source = BOOT.read_text(encoding="utf-8")
    assert "classList.toggle('mars-thinking-collapsed'" not in source
    assert "classList.toggle('mars-hide-tools'" not in source
    assert "classList.toggle('mars-autocollapse-turns'" not in source
    assert "classList.toggle('mars-transparent-stream'" not in source


def test_boot_does_not_compete_with_the_controller():
    """boot.js may only fall back to listeners when the controller is absent."""
    source = BOOT.read_text(encoding="utf-8")
    assert source.count("window.visualViewport.addEventListener(") == 2, (
        "the only visualViewport listeners in boot.js are the legacy fallback pair"
    )
    fallback = source.split("function _attachLegacyViewportListeners()")[1]
    fallback = fallback.split("// Boot: restore last session")[0]
    assert "window.visualViewport.addEventListener('resize',handler)" in fallback
    assert "window.visualViewport.addEventListener('scroll',handler)" in fallback
    assert "vv.removeEventListener('resize',handler)" in fallback
    assert "vv.removeEventListener('scroll',handler)" in fallback
    # The controller path is a subscription, never a listener.
    bind = source.split("function _bindViewportGeometry()")[1].split("\n}")[0]
    assert "viewport.onChange(_onViewportGeometryChange)" in bind
    assert "addEventListener" not in bind
    # And the fallback is torn down whenever the controller is present.
    assert "_detachLegacyViewportListeners();" in bind


def test_geometry_change_does_not_schedule_the_legacy_reflow():
    source = BOOT.read_text(encoding="utf-8")
    handler = source.split("function _onViewportGeometryChange(){")[1].split("\n}")[0]
    assert "_scheduleMobileViewportReflow" not in handler, (
        "the 60 ms reflow timer belongs to the legacy path; with the controller the "
        "CSS variable write is already the invalidation"
    )
    assert "syncWorkspacePanelState()" in handler


def test_boot_applies_geometry_before_the_first_paint():
    source = BOOT.read_text(encoding="utf-8")
    setup = source.split("if(window.visualViewport){")[1].split("}else{")[0]
    assert "_syncKeyboardBottomInset();" in setup, (
        "boot must prime the keyboard inset before the fallback listeners are attached"
    )
    assert setup.index("_syncKeyboardBottomInset();") < setup.index("_attachLegacyViewportListeners()")


def test_appearance_store_is_optional():
    source = BOOT.read_text(encoding="utf-8")
    assert "window.MarsAppearanceStore||window.AppearanceStore" in source
    assert "typeof store.get!=='function'" in source
    # Boot must not load or require the appearance layer's file.
    assert "<script" not in source.split("MarsAppearanceStore")[0][-200:]
    assert "src='static/appearance_store" not in source
    assert 'src="static/appearance_store' not in source


def test_public_hook_for_later_layers():
    source = BOOT.read_text(encoding="utf-8")
    for symbol in (
        "window.MarsChatUx=",
        "apply:_applyChatUxSettings",
        "bindAppearanceStore:_bindChatUxAppearanceStore",
        "classes:_chatUxClassState",
    ):
        assert symbol in source
