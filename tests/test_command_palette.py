"""
Static checks for the command palette (Ctrl/Cmd+Shift+P).

The palette is a single classic script (static/command_palette.js) plus its
stylesheet. There is no DOM harness in this suite, so everything here is
source-inspection in the style of tests/test_css_tooltips.py: wiring in
index.html / sw.js, keyboard and a11y contracts, the i18n keys in every
locale, and — the interesting one — that the panel commands match the rail
markup instead of a hardcoded list.

Run:
    python -m pytest tests/test_command_palette.py -q
"""

import os
import re
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(BASE_DIR, "static")
PALETTE_JS = os.path.join(STATIC, "command_palette.js")
PALETTE_CSS = os.path.join(STATIC, "command_palette.css")
INDEX_HTML = os.path.join(STATIC, "index.html")
SW_JS = os.path.join(STATIC, "sw.js")
I18N_JS = os.path.join(STATIC, "i18n.js")

LOCALES = ["en", "it", "ja", "ru", "es", "de", "zh", "pt", "ko", "fr",
           "cs", "tr", "pl", "vi"]

PALETTE_KEYS = [
    "palette_group_navigation",
    "palette_group_actions",
    "palette_group_view",
    "palette_open_panel",
    "palette_open_settings",
    "palette_new_chat",
    "palette_search_sessions",
    "palette_toggle_theme",
    "palette_theme_light",
    "palette_theme_dark",
    "palette_theme_system",
    "palette_toggle_sidebar",
    "palette_close",
    "palette_empty",
    "palette_placeholder",
    "palette_aria_label",
]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Locale block extraction (same approach as test_css_tooltips: regex over the
# on-disk, non-split i18n.js where every locale table is inline).
# ---------------------------------------------------------------------------
def _locale_block(source, code, all_codes):
    """Return the text of `code: { ... }` in LOCALES.

    The closing brace is searched for as `\n  },` — the indentation every
    locale block uses — and the next locale header bounds the search so a
    formatting change in one locale cannot silently attribute keys to another.
    """
    start = source.find("  %s: {" % code)
    if start < 0:
        raise AssertionError("locale %r not found in i18n.js" % code)
    end = source.find("\n  },", start)
    if end < 0:
        end = source.find("\n},", start)  # last (vi) block is dedented
    if end < 0:
        raise AssertionError("end of locale %r not found in i18n.js" % code)
    return source[start:end + 1]


# ===========================================================================
# 1. Files and wiring
# ===========================================================================
class TestWiring(unittest.TestCase):
    def test_palette_files_exist(self):
        self.assertTrue(os.path.isfile(PALETTE_JS), "static/command_palette.js missing")
        self.assertTrue(os.path.isfile(PALETTE_CSS), "static/command_palette.css missing")

    def test_palette_is_an_iife(self):
        js = _read(PALETTE_JS)
        self.assertRegex(js, r"^\s*//", "module should start with a header comment")
        self.assertIn("(function(){", js, "expected an IIFE wrapper")
        self.assertIn("'use strict'", js)

    def test_script_included_in_index_html(self):
        html = _read(INDEX_HTML)
        self.assertRegex(
            html, r'<script src="static/command_palette\.js\?v=[^"]+"\s+defer></script>',
            "command_palette.js is not loaded by index.html",
        )

    def test_stylesheet_included_in_index_html(self):
        html = _read(INDEX_HTML)
        self.assertRegex(
            html, r'<link rel="stylesheet" href="static/command_palette\.css\?v=[^"]+">',
            "command_palette.css is not loaded by index.html",
        )

    def test_script_loaded_after_dependencies(self):
        """The palette calls switchPanel/newSession/_pickTheme at run time, but
        the script must sit after panels.js/boot.js so those globals have been
        defined when the palette's constructors evaluate."""
        html = _read(INDEX_HTML)
        palette_at = html.find("static/command_palette.js")
        self.assertGreater(palette_at, 0)
        for dep in ("static/panels.js", "static/boot.js", "static/i18n.js"):
            self.assertLess(html.find(dep), palette_at,
                            "%s must be loaded before command_palette.js" % dep)

    def test_shell_assets_precache_palette(self):
        sw = _read(SW_JS)
        self.assertIn("./static/command_palette.js' + VQ", sw,
                      "command_palette.js missing from SHELL_ASSETS")
        self.assertIn("./static/command_palette.css' + VQ", sw,
                      "command_palette.css missing from SHELL_ASSETS")


# ===========================================================================
# 2. Keyboard contract
# ===========================================================================
class TestKeyboard(unittest.TestCase):
    def setUp(self):
        self.js = _read(PALETTE_JS)

    def test_opens_on_ctrl_or_cmd_shift_p(self):
        self.assertRegex(self.js, r"(?:e|event)\.ctrlKey\s*\|\|\s*(?:e|event)\.metaKey")
        self.assertRegex(self.js, r"(?:e|event)\.shiftKey")
        # The key check doubles as a layout-independent `code` fallback.
        self.assertIn("KeyP", self.js)

    def test_arrow_down_and_up(self):
        self.assertIn("'ArrowDown'", self.js)
        self.assertIn("'ArrowUp'", self.js)

    def test_enter_activates(self):
        self.assertIn("'Enter'", self.js)

    def test_escape_closes(self):
        self.assertIn("'Escape'", self.js)
        escape_block = self.js[self.js.find("case 'Escape'"):]
        self.assertIn("close()", escape_block[:400],
                      "Escape must call close()")

    def test_escape_is_stopped_from_reaching_global_handlers(self):
        """boot.js has a document-level Escape handler (closes settings,
        cancels edits). The palette must stop propagation so one Escape does
        not do two things."""
        escape_block = self.js[self.js.find("case 'Escape'"):][:400]
        self.assertIn("stopPropagation", escape_block)

    def test_backdrop_click_closes(self):
        self.assertRegex(self.js, r"event\.target\s*===\s*_root", "backdrop handler missing")

    def test_does_not_steal_ctrl_k_new_chat(self):
        """Ctrl+K is new chat (static/boot.js) and stays that way. A palette is
        additive: repurposing a documented shortcut would change what users
        already rely on, so the module must register no K chord at all."""
        handlers = re.findall(r"document\.addEventListener\('keydown'.*?\}, true\);",
                              self.js, re.S)
        for handler in handlers:
            self.assertNotIn("KeyK", handler,
                             "the palette must not register a Ctrl+K handler")
            self.assertNotIn("'k'", handler)
            self.assertNotIn("'K'", handler)

    def test_does_not_steal_ctrl_slash_or_comma(self):
        """Ctrl+/ (focus composer) and Ctrl+, (settings) are owned by boot.js;
        the palette must not add handlers for them."""
        handlers = re.findall(r"document\.addEventListener\('keydown'.*?\}, true\);",
                              self.js, re.S)
        self.assertGreaterEqual(len(handlers), 1, "expected the chord handler")
        for handler in handlers:
            self.assertNotIn("','", handler)
            self.assertNotIn("'/'", handler)
            self.assertNotIn("Slash", handler)
            self.assertNotIn("Comma", handler)


# ===========================================================================
# 3. Accessibility contract
# ===========================================================================
class TestAccessibility(unittest.TestCase):
    def setUp(self):
        self.js = _read(PALETTE_JS)

    def test_dialog_roles(self):
        self.assertIn('role: \'dialog\'', self.js, 'dialog role missing')
        self.assertIn("'aria-modal': 'true'", self.js, "aria-modal missing")
        self.assertIn("'aria-label': _t('palette_aria_label')", self.js,
                      "dialog aria-label must come from i18n")

    def test_combobox_contract(self):
        self.assertIn("role: 'combobox'", self.js)
        self.assertIn("'aria-expanded': 'true'", self.js)
        self.assertIn("'aria-controls': 'commandPaletteList'", self.js)
        self.assertIn("aria-activedescendant", self.js)

    def test_listbox_and_options(self):
        self.assertIn("role: 'listbox'", self.js)
        self.assertIn("role: 'option'", self.js)
        self.assertIn("setAttribute('aria-selected'", self.js)

    def test_options_are_keyboard_linked(self):
        """Each rendered option needs an id so aria-activedescendant can point
        at the active one."""
        self.assertIn("commandPaletteOption", self.js)

    def test_tab_is_trapped(self):
        tab_block = self.js[self.js.find("event.key !== 'Tab'"):]
        self.assertIn("preventDefault()", tab_block[:600])


# ===========================================================================
# 4. Behaviour: filtering / empty state
# ===========================================================================
class TestBehaviour(unittest.TestCase):
    def setUp(self):
        self.js = _read(PALETTE_JS)

    def test_filters_on_every_token(self):
        self.assertRegex(self.js, r"\.every\(function\(token\)", "multi-token filter missing")
        self.assertIn(".toLowerCase()", self.js)

    def test_empty_state_uses_i18n(self):
        self.assertIn("_t('palette_empty')", self.js)

    def test_cursor_resets_on_query_change(self):
        self.assertRegex(self.js, r"_active\s*=\s*0", "cursor reset missing")

    def test_arrow_navigation_wraps(self):
        self.assertRegex(self.js, r"_setActive\(_active\s*\+\s*1", "no next-row navigation")
        self.assertRegex(self.js, r"_setActive\(_active\s*-\s*1", "no previous-row navigation")
        self.assertRegex(self.js, r"%\s*rows\.length", "navigation must wrap around")


# ===========================================================================
# 5. Commands come from existing globals, and panel commands match the rail
# ===========================================================================
class TestCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(PALETTE_JS)
        cls.html = _read(INDEX_HTML)

    def test_commands_use_existing_globals(self):
        for name in ("switchPanel", "newSession", "_pickTheme", "toggleSidebar",
                     "toggleSettings"):
            self.assertIn("_fn('%s')" % name, self.js,
                          "%s should be resolved through _fn()" % name)

    def test_panel_commands_are_read_from_the_rail(self):
        self.assertIn(".rail .nav-tab[data-panel]", self.js,
                      "panel commands must be derived from the rail, not hardcoded")

    def test_panel_title_keys_cover_every_panel_key(self):
        """Every panel in PANEL_TITLE_KEYS must name an i18n key that exists."""
        i18n = _read(I18N_JS)
        mapping = re.search(r"var PANEL_TITLE_KEYS = \{(.*?)\};", self.js, re.S)
        self.assertIsNotNone(mapping, "PANEL_TITLE_KEYS not found")
        keys = re.findall(r"\w+:\s*'(\w+)'", mapping.group(1))
        self.assertGreaterEqual(len(keys), 5)
        en_block = _locale_block(i18n, "en", LOCALES)
        for key in keys:
            self.assertIn("%s:" % key, en_block,
                          "PANEL_TITLE_KEYS references missing i18n key %r" % key)

    def test_every_rail_panel_maps_to_a_command(self):
        """The bridge the task asked for: parse the rail out of index.html and
        assert command_palette.js knows how to name every one of those panels.
        A new rail tab without a PANEL_TITLE_KEYS entry would otherwise render
        as an untranslated sentence in the palette."""
        rail = re.search(r'<nav class="rail"[^>]*>(.*?)</nav>', self.html, re.S)
        self.assertIsNotNone(rail, "rail nav not found in index.html")
        panels = set(re.findall(r'data-panel="([a-z0-9_-]+)"', rail.group(1)))
        self.assertGreaterEqual(len(panels), 8, "rail parsed too few panels")
        mapping = re.search(r"var PANEL_TITLE_KEYS = \{(.*?)\};", self.js, re.S)
        self.assertIsNotNone(mapping)
        mapped = set(re.findall(r"(\w+):\s*'", mapping.group(1)))
        missing = sorted(panels - mapped)
        self.assertEqual(
            missing, [],
            "rail panels with no command/palette name: %s — add them to "
            "PANEL_TITLE_KEYS" % ", ".join(missing),
        )

    def test_featured_panels_exist_in_rail(self):
        featured = re.search(r"FEATURED_PANELS = \[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(featured)
        names = re.findall(r"'([^']+)'", featured.group(1))
        for name in names:
            self.assertIn('data-panel="%s"' % name, self.html,
                          "FEATURED_PANELS lists a panel absent from the rail: %s" % name)

    def test_navigation_group_and_open_label(self):
        self.assertIn("'navigation'", self.js)
        self.assertIn("_t('palette_open_panel', label)", self.js)
        # The switchPanel call the rail buttons use.
        self.assertIn("switchPanel(panel, {fromRailClick: true})", self.js)

    def test_every_command_has_the_documented_shape(self):
        for field in ("id:", "title:", "group:", "keywords:", "run:"):
            self.assertIn(field, self.js)
        self.assertRegex(self.js, r"run: function\s*\(", "run must be a function")


# ===========================================================================
# 6. i18n: every key in every locale
# ===========================================================================
class TestI18n(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = _read(PALETTE_JS)
        cls.i18n = _read(I18N_JS)

    def test_palette_keys_used_in_js(self):
        used = set(re.findall(r"'(palette_[a-z_]+)'", self.js))
        self.assertEqual(sorted(used), sorted(PALETTE_KEYS),
                         "the palette's keys and PALETTE_KEYS drifted apart")

    def test_every_locale_has_every_key(self):
        for code in LOCALES:
            block = _locale_block(self.i18n, code, LOCALES)
            for key in PALETTE_KEYS:
                self.assertRegex(
                    block, r"\b%s\s*:" % re.escape(key),
                    "locale %r is missing %r" % (code, key),
                )

    def test_english_is_the_fallback_and_is_not_empty(self):
        en_block = _locale_block(self.i18n, "en", LOCALES)
        for key in PALETTE_KEYS:
            m = re.search(r"\b%s\s*:\s*'([^']*)'" % re.escape(key), en_block)
            self.assertIsNotNone(m, "en: %s is not a plain string" % key)
            self.assertTrue(m.group(1).strip(), "en: %s is empty" % key)

    def test_russian_and_major_locales_are_translated(self):
        """Guard the obvious regression: copy-pasting the English table into a
        locale. These few keys must differ from en in the locales where a
        translation is unambiguous."""
        en_block = _locale_block(self.i18n, "en", LOCALES)
        en_value = lambda key: re.search(
            r"\b%s\s*:\s*'([^']*)'" % re.escape(key), en_block).group(1)
        for code in ("ru", "de", "fr", "es", "it", "pl", "cs", "tr", "vi",
                     "pt", "ja", "ko", "zh"):
            block = _locale_block(self.i18n, code, LOCALES)
            for key in ("palette_empty", "palette_aria_label", "palette_new_chat"):
                value = re.search(
                    r"\b%s\s*:\s*'([^']*)'" % re.escape(key), block).group(1)
                self.assertNotEqual(value, en_value(key),
                                    "%s: %s is still English" % (code, key))

    def test_open_panel_placeholder_present(self):
        """palette_open_panel is formatted with t(key, label) — it needs {0}."""
        en_block = _locale_block(self.i18n, "en", LOCALES)
        self.assertRegex(en_block, r"palette_open_panel\s*:\s*'[^']*\{0\}")


# ===========================================================================
# 7. Styling contract
# ===========================================================================
class TestStyles(unittest.TestCase):
    def setUp(self):
        self.css = _read(PALETTE_CSS)

    def test_anchored_near_the_top(self):
        rule = re.search(r"\.command-palette\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(rule, ".command-palette rule missing")
        self.assertRegex(rule.group(1), r"padding-top\s*:\s*1[0-5]vh",
                         "the dialog must sit at 12-15vh from the top")
        self.assertRegex(rule.group(1), r"align-items\s*:\s*flex-start",
                         "a centered dialog would ignore the top offset")
        self.assertRegex(rule.group(1), r"rgba\(0,\s*0,\s*0,\s*\.?45\)",
                         "backdrop must be rgba(0,0,0,.45)")

    def test_uses_theme_tokens(self):
        for token in ("var(--surface)", "var(--border)", "var(--shadow-lg)",
                      "var(--accent-bg)", "var(--accent-text)"):
            self.assertIn(token, self.css, "%s not used" % token)

    def test_width_and_radius(self):
        dialog = re.search(r"\.command-palette-dialog\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(dialog)
        self.assertRegex(dialog.group(1), r"max-width\s*:\s*6[0-4]0px")
        self.assertRegex(dialog.group(1), r"border-radius\s*:\s*1[2-4]px")

    def test_rows_are_about_forty_pixels(self):
        row = re.search(r"\.command-palette-row\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(row)
        self.assertRegex(row.group(1), r"min-height\s*:\s*(?:38|39|40)px")

    def test_active_row_uses_accent(self):
        selected = re.search(
            r'\.command-palette-row\[aria-selected="true"\]\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(selected, "selected-row styling missing")
        self.assertIn("var(--accent-bg)", selected.group(1))
        self.assertIn("var(--accent-text)", selected.group(1))


if __name__ == "__main__":
    unittest.main()
