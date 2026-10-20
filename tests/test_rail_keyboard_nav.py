"""Static contract tests for rail keyboard navigation (ARIA tablist pattern).

The desktop rail is ``<nav class="rail" role="tablist" aria-orientation="vertical">``.
ArrowUp/ArrowDown must move focus between visible tabs (wrapping), Home/End must
jump to the edges, hidden/unavailable tabs must be skipped, and activation must
stay on Enter/Space. None of this may be handled globally.

Run:
    python -m pytest tests/test_rail_keyboard_nav.py -q
"""

import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE_DIR, "static")
RAIL_NAV_JS = os.path.join(STATIC, "rail_nav.js")
INDEX_HTML = os.path.join(STATIC, "index.html")
SW_JS = os.path.join(STATIC, "sw.js")
PANELS_JS = os.path.join(STATIC, "panels.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_rail_nav_module_exists():
    assert os.path.isfile(RAIL_NAV_JS), "static/rail_nav.js must exist"


def test_module_is_isolated_iife():
    src = _read(RAIL_NAV_JS)
    assert src.lstrip().startswith("//"), "module should open with a comment header"
    assert "(function(){" in src.replace(" (", " (")
    assert "'use strict';" in src
    assert src.rstrip().endswith("})();"), "module must be a closed IIFE"


def test_handles_arrow_home_end():
    src = _read(RAIL_NAV_JS)
    for key in ("ArrowUp", "ArrowDown", "Home", "End"):
        assert repr(key) in src or f"'{key}'" in src or f'"{key}"' in src, (
            f"rail_nav.js must handle {key}"
        )
    # Cyclic movement for arrows.
    assert "% tabs.length" in src and "+ tabs.length)" in src, (
        "ArrowUp/ArrowDown must wrap around the visible tab list"
    )


def test_skips_hidden_and_disabled_tabs():
    src = _read(RAIL_NAV_JS)
    assert "nav-tab-hidden" in src
    assert "aria-disabled" in src, "unavailable embedded-section tabs must be skipped"
    assert "nav-tab-unavailable" in src or "aria-disabled" in src
    assert "getClientRects" in src, (
        "tabs hidden by display:none with no class must still be skipped"
    )


def test_listener_is_scoped_to_the_rail():
    src = _read(RAIL_NAV_JS)
    assert "document.addEventListener('keydown', _onKeyDown)" in src
    # The handler must bail out unless focus is inside the rail.
    assert "rail.contains(current)" in src, "keys outside .rail must pass through"
    # No global arrow hijack: preventDefault only after a match.
    pd_indices = [m.start() for m in re.finditer(r"e\.preventDefault\(\)", src)]
    assert len(pd_indices) == 1, "exactly one preventDefault, on a confirmed key match"
    assert "if(!next || next === current) return;" in src


def test_focus_does_not_activate_panel():
    src = _read(RAIL_NAV_JS)
    assert "switchPanel(" not in src, "arrow navigation must not switch panels itself"
    assert ".click()" not in src, "arrow navigation must only move focus"
    assert "tab.focus(" in src


def test_module_is_loaded_before_app_modules():
    html = _read(INDEX_HTML)
    idx = html.find("static/rail_nav.js")
    assert idx != -1, "index.html must load static/rail_nav.js"
    # First module in the deferred bundle: nothing it relies on may be required
    # to parse before it, and every app module must still load after it.
    assert idx < html.find("static/i18n.js")
    assert idx < html.find("static/ui.js")
    assert idx < html.find("static/panels.js")
    assert idx < html.find("static/boot.js")
    assert "defer" in html[html.rfind("<script", 0, idx):html.find("</script>", idx)]


def test_module_is_in_shell_assets():
    sw = _read(SW_JS)
    m = re.search(r"const SHELL_ASSETS\s*=\s*\[(.*?)\];", sw, re.S)
    assert m, "SHELL_ASSETS array not found in sw.js"
    assert "'./static/rail_nav.js'" in m.group(1)


def test_divider_collapse_hook_survives():
    src = _read(RAIL_NAV_JS)
    assert "rail-divider" in src
    assert "rail-divider-hidden" in src
    assert "MutationObserver" in src, (
        "dividers must re-evaluate when tab visibility changes at runtime"
    )
    # A divider collapses when the group below it has no visible tab.
    assert "hasBelow" in src and "hasAbove" in src
    # The bottom `settings` slot is not a group: it lives past .rail-spacer.
    assert "=== 'settings'" in src
    # Hidden dividers must not be treated as tabs when measuring neighbours.
    assert "contains('rail-divider')" in src


def test_reorder_script_anchor_is_intact():
    """Regression marker: the flash-prevention reorder still keys off .rail-spacer."""
    html = _read(INDEX_HTML)
    start = html.find("hermes-webui-tab-order")
    assert start != -1
    script = html[html.rfind("<script", 0, start):html.find("</script>", start)]
    assert "rail-spacer" in script
    assert "insertBefore" in script
    assert ".rail" in script and ".sidebar-nav" in script


def test_panels_tab_order_anchor_is_intact():
    """Runtime reorder in panels.js uses the same anchor."""
    src = _read(PANELS_JS)
    start = src.find("function _applyTabOrder(")
    assert start != -1
    end = src.find("\nfunction ", start + 1)
    body = src[start:end]
    assert "rail-spacer" in body
    assert "insertBefore" in body
