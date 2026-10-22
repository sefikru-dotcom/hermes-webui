"""Geometry contract tests for static/style.css (layout zone).

These parse the real stylesheet and assert the cascade-visible invariants that
make the mobile chat usable, without needing a browser:

  * the document itself never scrolls (only the transcript does),
  * the composer's bottom padding is one inset built from safe-area OR keyboard,
    never both stacked,
  * the split panel and the chat column partition the *visible* viewport using
    the same ratio, so the panel cannot displace the chat/composer,
  * mode classes remain compatible with .mars-process-window,
  * content-visibility:auto is not reintroduced for transcript containers.

Run:
    HOME=$(mktemp -d) HERMES_HOME=$(mktemp -d) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
      /opt/hermes-webui/.venv/bin/python3 -m pytest tests/test_upgrade_layout_css.py -q
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

COMMENT = re.compile(r"/\*.*?\*/", re.S)
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def rules():
    """[(selector, declarations, order)] with comments removed."""
    clean = COMMENT.sub("", CSS)
    out = []
    for index, match in enumerate(RULE.finditer(clean)):
        selector = " ".join(match.group(1).split())
        decls = [d.strip() for d in match.group(2).split(";") if d.strip()]
        out.append((selector, decls, index))
    return out


ALL_RULES = rules()


def declarations(selector_substring):
    """Declarations of every rule whose selector contains the substring, in order."""
    found = []
    for selector, decls, _ in ALL_RULES:
        if selector_substring in selector:
            found.extend(decls)
    return found


def last_declaration(selector_substring, prop):
    value = None
    for selector, decls, _ in ALL_RULES:
        if selector_substring not in selector:
            continue
        for decl in decls:
            name, _, raw = decl.partition(":")
            if name.strip() == prop:
                value = raw.strip()
    return value


def test_document_does_not_scroll():
    html_decls = "".join(
        "".join(decls).replace(" ", "") for selector, decls, _ in ALL_RULES if selector == "html"
    )
    assert "overflow:hidden" in html_decls, (
        "html must not scroll: a visualViewport pan would move the composer off screen"
    )
    body = last_declaration("body{", "overflow") or last_declaration("body", "overflow")
    assert "hidden" in (body or ""), "body keeps overflow:hidden"


def last_declaration_exact(selector, prop):
    value = None
    for sel, decls, _ in ALL_RULES:
        if sel != selector:
            continue
        for decl in decls:
            name, _, raw = decl.partition(":")
            if name.strip() == prop:
                value = raw.strip()
    return value


def test_composer_inset_is_safe_area_or_keyboard_never_both():
    value = last_declaration_exact(".composer-wrap", "padding-bottom")
    assert value, ".composer-wrap must set padding-bottom"
    compact = value.replace(" ", "")
    assert "env(safe-area-inset-bottom" in compact, value
    assert "var(--keyboard-bottom-inset" in compact, value
    assert "max(" in compact, f"safe area and keyboard must not stack: {value}"
    assert "flex-shrink:0" in "".join(
        declarations(".composer-wrap")
    ), "the composer must not be squeezed out by a short viewport"


def test_split_panel_partitions_the_visible_viewport():
    chat_height = last_declaration(".mobile-workspace-split #mainChat", "height")
    panel_top = last_declaration(".mobile-workspace-split .rightpanel.mobile-open", "top")
    assert chat_height, "split chat column needs an explicit height"
    assert panel_top, "split panel needs an explicit top"
    for expr in (chat_height, panel_top):
        assert "var(--app-vh" in expr, f"split geometry must follow the visible viewport: {expr}"
        assert "var(--split-chat-ratio" in expr, f"chat and panel must share one ratio: {expr}"
    assert last_declaration(".mobile-workspace-split .rightpanel.mobile-open", "bottom") == "0!important", (
        "the panel must reach the keyboard/safe-area edge, not leave a dead gap"
    )
    assert "--split-chat-ratio:45" in "".join(declarations(".layout.mobile-workspace-split"))


def test_keyboard_open_gives_chat_more_of_the_split():
    assert 'html[data-keyboard-open="1"] .layout.mobile-workspace-split' in CSS
    assert "--split-chat-ratio:58" in CSS


def test_split_composer_does_not_double_count_the_keyboard():
    """The split column is measured against the visible viewport already."""
    value = last_declaration(".mobile-workspace-split #mainChat .composer-wrap", "padding-bottom")
    assert value, "split mode must pin the composer's bottom padding"
    compact = value.replace(" ", "")
    assert "env(safe-area-inset-bottom" in compact
    assert "--keyboard-bottom-inset" not in compact, (
        "split chat height already follows --app-vh; subtracting the keyboard twice "
        "pushes the composer out of the column"
    )


def test_visible_viewport_units_are_defined_with_fallback():
    assert re.search(r":root\{[^}]*--app-vh:1vh", CSS), "--app-vh needs a vh fallback"
    assert "@supports (height:100dvh)" in CSS
    assert re.search(r"@supports \(height:100dvh\)\{:root\{--app-vh:1dvh", CSS)


def test_mode_classes_stay_compatible_with_process_window():
    assert "html.mars-mode-quiet .mars-process-window" in CSS
    assert "html.mars-mode-full .mars-process-window" in CSS
    assert "html.mars-mode-window .mars-process-window" in CSS


def test_content_visibility_auto_is_not_reintroduced():
    assert "content-visibility:auto" not in COMMENT.sub("", CSS).replace(" ", "")
    assert "content-visibility:visible" in CSS


def test_right_anchored_containers_mirror_the_tooltip_anchor():
    """A bubble must never be clipped to a sliver by its own container.

    `.rightpanel` and `.composer-right` sit against a hard right edge and clip on
    overflow, so the default `left:calc(100% + 8px)` bubble landed outside the
    clip box (measured: 167px past `.rightpanel` at 1024x720). The fix mirrors the
    anchor; a clamp on the container would hide the label instead.
    """
    mirrored = [
        decls for sel, decls, _ in ALL_RULES
        if ".rightpanel .has-tooltip" in sel or ".composer-right .has-tooltip" in sel
    ]
    assert mirrored, "tooltip anchor rules for right-anchored containers are missing"
    joined = "".join("".join(d) for d in mirrored)
    assert "right:calc(100% + 8px)" in joined, "bubble must open leftwards"
    # The trigger's own label must stay intact: no truncation/clamp escape hatch.
    for sel, decls, _ in ALL_RULES:
        if ".rightpanel .has-tooltip" in sel or ".composer-right .has-tooltip" in sel:
            body = "".join(decls)
            assert "max-width" not in body and "text-overflow" not in body, (
                "tooltips are mirrored, not truncated: " + sel
            )


def test_activity_window_bound_has_one_owner():
    """The window height is declared once and applied only by the mode layer.

    The renderer also writes an inline `max-height` on the same
    `[data-activity-body]` element, and inline beats a normal declaration — so the
    mode rules must stay authoritative or the viewport-aware 38vh cap is dead.
    """
    assert CSS.count("--activity-window-max:") == 1, (
        "the bound must be declared in exactly one place"
    )
    stream = "".join(declarations(".mars-process-window-stream"))
    assert "max-height:220px" not in stream, "the stale 220px literal disagreed with the renderer"
    assert "--activity-window-max" in stream
    for selector in (
        "html.mars-mode-window .mars-process-window:not(.is-collapsed) .mars-process-window-stream",
        "html.mars-mode-full .mars-process-window .mars-process-window-stream",
    ):
        value = last_declaration(selector, "max-height")
        assert value and "!important" in value, (
            selector + " must win over the renderer's inline max-height"
        )


def test_music_frame_host_is_not_clamped():
    """The lazy /music/ embed keeps its own sizing; this zone may not bound it."""
    host = "".join(declarations("#musicFrameHost"))
    frame = "".join(declarations("#musicIframe"))
    for body, name in ((host, "#musicFrameHost"), (frame, "#musicIframe")):
        assert "max-height" not in body, name + " must not gain a max-height"
        assert "overflow:hidden" not in body.replace(" ", ""), name + " must not be clipped"
