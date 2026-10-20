"""Regression tests for the left rail grouping contract.

The desktop rail groups its tabs semantically:

    work        chat, tasks, kanban, todos
    knowledge   projects, workspaces, memory, skills, profiles
    media       music
    observation insights, logs
    (settings stays at the bottom, after .rail-spacer)

Groups are separated by ``<div class="rail-divider" role="separator">`` nodes.
Those nodes must never break the flash-prevention reorder script, which keys
off ``.rail-spacer``, and must collapse when a group has no visible tab left.

Run:
    python -m pytest tests/test_rail_grouping.py -q
"""

import os
import re

import pytest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(BASE_DIR, "static", "index.html")
STYLE_CSS = os.path.join(BASE_DIR, "static", "style.css")
RAIL_NAV_JS = os.path.join(BASE_DIR, "static", "rail_nav.js")

GROUPS = [
    ["chat", "tasks", "kanban", "todos"],
    ["projects", "workspaces", "memory", "skills", "profiles"],
    ["music"],
    ["insights", "logs"],
]
GROUPS_HEADER = (
    "Rail groups (contract): work = chat, tasks, kanban, todos; knowledge =",
    "projects, workspaces, memory, skills, profiles; media = music;",
    "observation = insights, logs.",
)

def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def html():
    return _read(INDEX_HTML)


@pytest.fixture(scope="module")
def css():
    return _read(STYLE_CSS)


def _rail_block(html):
    """Inner markup of the desktop rail, without the mobile sidebar-nav."""
    start = html.find('<nav class="rail"')
    assert start >= 0, "static/index.html must contain <nav class=\"rail\">"
    end = html.find("</nav>", start)
    assert end > start, "<nav class=\"rail\"> must be closed"
    return html[start:end]


def _rail_items(html):
    """Rail children in document order as (kind, value) tuples.

    kind is "divider" or "tab"; value is the panel id for tabs.
    """
    items = []
    for line in _rail_block(html).splitlines():
        if "rail-divider" in line:
            items.append(("divider", None))
            continue
        m = re.search(r'class="[^"]*\brail-btn\b[^"]*"', line)
        if not m:
            continue
        panel = re.search(r'data-panel="([^"]+)"', line)
        assert panel, f"rail button without data-panel: {line[:120]}"
        items.append(("tab", panel.group(1)))
    return items


def test_rail_is_a_vertical_tablist(html):
    block = _rail_block(html)
    assert 'role="tablist"' in block
    assert 'aria-orientation="vertical"' in block


def test_rail_has_four_group_dividers(html):
    dividers = [i for i, (kind, _) in enumerate(_rail_items(html)) if kind == "divider"]
    # Three between the four groups plus one before the bottom settings slot.
    assert len(dividers) == 4, f"expected 4 .rail-divider nodes, found {len(dividers)}"


def test_rail_dividers_are_separators(html):
    nodes = re.findall(r'<div[^>]*rail-divider[^>]*>', _rail_block(html))
    assert len(nodes) == 4
    for node in nodes:
        assert 'role="separator"' in node, node


def test_rail_group_order_matches_contract(html):
    items = _rail_items(html)
    # Drop the trailing divider + settings: dividers split the four groups.
    body = items[:-2]
    assert body[-1] == ("tab", "logs"), "logs closes the observation group"
    assert items[-1] == ("tab", "settings"), "settings must stay the last rail tab"

    rendered = []
    current = []
    for kind, value in body:
        if kind == "divider":
            rendered.append(current)
            current = []
        else:
            current.append(value)
    rendered.append(current)

    assert rendered == GROUPS, f"rail groups drifted from the contract: {rendered}"


def test_settings_sits_after_the_spacer(html):
    block = _rail_block(html)
    spacer = block.find("rail-spacer")
    settings = block.find('data-panel="settings"', spacer)
    assert spacer != -1, ".rail-spacer is the reorder anchor and must stay in the rail"
    assert settings > spacer, "settings must follow the spacer"


def test_dividers_never_sit_between_the_spacer_and_settings(html):
    items = _rail_items(html)
    names = [value for kind, value in items if kind == "tab"]
    # The spacer is not captured as an item; assert via raw order instead.
    block = _rail_block(html)
    spacer = block.find("rail-spacer")
    tail = block[spacer:]
    settings = tail.find('data-panel="settings"')
    assert settings != -1
    assert "rail-divider" not in tail[:settings], (
        "nothing may separate .rail-spacer from the settings button"
    )
    # The divider that closes the observation group belongs before the spacer.
    assert items[-2] == ("divider", None), "last divider must precede the spacer"
    assert names[-1] == "settings"


def test_group_comment_documents_the_contract(html):
    comment_start = html.find("<!-- Rail groups (contract):")
    assert comment_start >= 0, "rail markup must document the group contract"
    comment = html[comment_start:html.find("-->", comment_start)]
    for line in GROUPS_HEADER:
        assert line in comment, f"missing rail group contract line: {line!r}"


def test_reorder_script_still_anchors_on_rail_spacer(html):
    """The inline reorder script inserts tabs before .rail-spacer."""
    marker = html.find("hermes-webui-tab-order")
    assert marker != -1
    script_end = html.find("</script>", marker)
    script = html[html.rfind("<script", 0, marker):script_end]
    assert "rail-spacer" in script, "reorder script lost its .rail-spacer anchor"
    assert "insertBefore" in script
    # It must not treat a divider as the insertion anchor.
    assert "rail-divider" not in script, (
        "adding dividers must not change the reorder script"
    )


def test_rail_divider_is_no_longer_an_orphan(css):
    assert ".rail-divider" in css, "style.css must style .rail-divider"
    # The rule must be scoped to the desktop rail, not the mobile sidebar-nav.
    assert re.search(r"\.rail \.rail-divider\s*\{", css), (
        ".rail-divider rules must be scoped to .rail"
    )
    assert ".rail-divider-hidden" in css, (
        "collapsed dividers need a display:none rule"
    )


def test_divider_hidden_state_is_display_none(css):
    m = re.search(
        r"\.rail \.rail-divider\.rail-divider-hidden\s*\{([^}]*)\}", css
    )
    assert m, "missing .rail-divider.rail-divider-hidden rule"
    assert "display:none" in m.group(1).replace(" ", "")


def test_dividers_do_not_leak_into_mobile_sidebar(css):
    assert ".sidebar-nav .rail-divider" not in css
    assert ".sidebar-nav .rail-divider-hidden" not in css


def test_every_rail_tab_has_localized_aria_label(html):
    block = _rail_block(html)
    keys = re.findall(r'<button[^>]*class="[^"]*\brail-btn\b[^"]*"[^>]*>', block)
    assert len(keys) == 13, f"expected 13 rail buttons, found {len(keys)}"
    for tag in keys:
        panel = re.search(r'data-panel="([^"]+)"', tag).group(1)
        label = re.search(r'\bdata-i18n-aria-label="([^"]+)"', tag)
        assert label, f"rail button {panel} is missing data-i18n-aria-label"
        assert label.group(1) == f"tab_{panel}", (
            f"rail button {panel} must map to the tab_{panel} locale key"
        )
        # aria-label stays as the no-JS fallback.
        assert re.search(r'\baria-label="[^"]+"', tag), (
            f"rail button {panel} lost its fallback aria-label"
        )


def test_mobile_sidebar_tabs_are_localized_too(html):
    start = html.find('<div class="sidebar-nav"')
    assert start >= 0
    end = html.find("</div>", start)
    assert end > start
    section = html[start:end]
    tags = re.findall(r"<button[^>]*\bdata-panel=\"[^\"]+\"[^>]*>", section)
    # The mirrored settings button also lives before the container's </div>.
    assert len(tags) == 13, f"expected 13 sidebar-nav tabs, found {len(tags)}"
    for tag in tags:
        panel = re.search(r'data-panel="([^"]+)"', tag).group(1)
        label = re.search(r'\bdata-i18n-aria-label="([^"]+)"', tag)
        assert label and label.group(1) == f"tab_{panel}", (
            f"sidebar-nav tab {panel} must carry data-i18n-aria-label=tab_{panel}"
        )


def test_settings_sidebar_tab_is_localized(html):
    start = html.find("<!-- Settings button mirrored here")
    assert start >= 0
    end = html.find("</div>", start)
    tag = html[start:end]
    assert 'data-panel="settings"' in tag
    assert 'data-i18n-aria-label="tab_settings"' in tag


def test_i18n_keys_used_by_the_rail_exist():
    i18n = _read(os.path.join(BASE_DIR, "static", "i18n.js"))
    for group in GROUPS + [["settings"]]:
        for panel in group:
            assert f"tab_{panel}:" in i18n, f"missing locale key tab_{panel}"


def test_rail_nav_module_owns_divider_collapse():
    """Divider upkeep lives in rail_nav.js; it must be loaded before app modules."""
    src = _read(RAIL_NAV_JS)
    assert "rail-divider-hidden" in src
    assert "syncRailDividers" in src
