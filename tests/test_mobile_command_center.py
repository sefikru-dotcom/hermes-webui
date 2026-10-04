"""Regression guards for the mobile command center and workspace touch actions."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def test_mobile_command_center_exposes_hidden_composer_features():
    for element_id in (
        "composerMobileAttachAction",
        "composerMobileSavedPromptsAction",
        "composerMobileToolsetsAction",
    ):
        assert f'id="{element_id}"' in INDEX
    assert "toggleSavedPromptsPopup()" in INDEX
    assert "toggleToolsetsDropdown()" in INDEX
    assert "composerToolsetsDropdown" in INDEX


def test_mobile_command_center_keeps_dropdown_accessibility_state():
    assert "composerMobileToolsetsAction" in UI
    assert "mobileAction.setAttribute('aria-expanded','true')" in UI
    assert "mobileAction.setAttribute('aria-expanded','false')" in UI
    assert "#savedPromptsPopup" in UI
    assert "composerMobileSavedPromptsAction" in MESSAGES
    assert "!e.target.closest('#composerMobileToolsetsAction')" in UI


def test_toolsets_can_anchor_to_mobile_action_when_desktop_chip_hidden():
    start = UI.index("function _positionToolsetsDropdown()")
    body = UI[start : start + 1800]
    assert "composerMobileToolsetsAction" in body
    assert "panel.classList.contains('open')" in body
    assert "const anchor" in body
    toggle = UI.index("function toggleToolsetsDropdown()")
    toggle_body = UI[toggle : toggle + 1600]
    assert "anchor.offsetParent === null" in toggle_body


def test_mobile_panel_open_restores_interactivity():
    start = UI.index("function openMobileComposerConfig()")
    body = UI[start : UI.index("function toggleMobileComposerConfig()", start)]
    assert "panel.inert=false" in body
    assert "panel.setAttribute('aria-hidden','false')" in body


def test_mobile_close_does_not_close_desktop_dropdowns():
    start = UI.index("function closeMobileComposerConfig()")
    body = UI[start : UI.index("function openMobileComposerConfig()", start)]
    guard = body.index("if(!panel || !panel.classList.contains('open')) return;")
    assert guard < body.index("closeModelDropdown();")
    assert "panel.inert=true" in body


def test_mobile_saved_prompts_popup_is_touch_sized():
    assert ".composer-footer .saved-prompts-popup.saved-prompts-open{display:flex!important;position:fixed" in STYLE
    assert ".composer-footer .composer-toolsets-dropdown.open{display:block;position:fixed" in STYLE
    assert "popup.classList.add('saved-prompts-open')" in MESSAGES
    assert "popup.classList.remove('saved-prompts-open')" in MESSAGES
    assert ".saved-prompts-popup .saved-prompt-row,.saved-prompts-popup .saved-prompt-save-btn{min-height:44px;" in STYLE


def test_workspace_long_press_opens_mobile_sheet_and_keeps_desktop_menu():
    assert "_fileLongPressTimer" in UI
    assert "mobileSheet:true" in UI
    assert "file-ctx-menu-mobile" in UI
    assert "if(mobileSheet)" in UI
    assert "oncontextmenu" in UI


def test_workspace_mobile_actions_have_touch_targets():
    assert ".file-ctx-menu-mobile>div{min-height:44px" in STYLE
    assert ".file-item.file-long-pressing" in STYLE
    assert "role','menu'" in UI
