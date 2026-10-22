# HANDOFF — layout zone (mobile viewport geometry + boot mode classes)

Worktree: `/root/workspace/webui-upgrade-20261022/layout` (base `ebb0f0da6`).
Files owned/edited: `static/boot.js`, `static/style.css`, new `static/viewport_controller.js`.
New tests/scripts: `tests/test_upgrade_layout_{viewport,modes,css}.py`,
`scripts/test_upgrade_layout_{viewport,chat_modes}.js`.
Not touched: `static/index.html`, `static/ui.js`, `static/panels.js`, backend, live config/data.

---

## 1. Script order the coordinator must wire

`static/index.html`, before `static/boot.js` (line ~2036 today), as a classic
(non-module) deferred script:

```html
<script src="static/viewport_controller.js?v=__WEBUI_VERSION__-layout-viewport-v1" defer></script>
...
<script src="static/boot.js?v=__WEBUI_VERSION__-mars-chatnav-v13" defer></script>
```

Both are `defer`, so execution order = document order. The module must come first.
It is tolerant of a wrong order (`load`/`pageshow` re-bind in boot.js), but then the
first frame paints with the legacy fallback geometry.

Also add `static/viewport_controller.js` to the service-worker precache list in
`static/sw.js` (integration zone) — otherwise the first offline load falls back to
legacy geometry.

`boot.js` consumes it through `window.MarsViewport` (`init/refresh/flush/onChange`,
`computeViewportState`, `KEYBOARD_MIN_INSET`). The module self-inits on
`DOMContentLoaded` when `document` exists, so no call from `index.html` is needed.

## 2. What the module owns (single geometry pipeline)

`static/viewport_controller.js` is the only writer of:

| surface | value |
|---|---|
| `--app-viewport-height` | visible height px (`visualViewport.height` when available) |
| `--app-vh` | `height/100` px — replaces `dvh` in places where `dvh` lies under the keyboard |
| `--keyboard-bottom-inset` | keyboard occlusion px, removed (not `0px`) when closed |
| `--safe-area-bottom` | measured `env(safe-area-inset-bottom)` |
| `<html data-keyboard-open>` | `"1"` / `"0"` |
| `<html data-viewport-height>` | integer px, diagnostics |

Rules that were the actual bugs:

- keyboard inset is `innerHeight - (vv.height + vv.offsetTop)`, **not** gated on
  `pointer:coarse` any more (touch laptops/DeX/tablets with a fine pointer used to get
  no inset at all — composer under the keyboard);
- pinch-zoom (`|scale-1| > 0.05`) never reads as a keyboard;
- a URL-bar-sized shrink (`innerHeight` also shrank, no offset) is not a keyboard;
- measurement is coalesced to one `requestAnimationFrame`, and listeners are exactly
  one `resize` + one `scroll` on `visualViewport` plus `window resize`,
  `orientationchange`, `pageshow`, `visibilitychange` — no per-event timers.

`boot.js` no longer registers a competing pipeline: `_bindViewportGeometry()`
subscribes via `onChange()` and tears down the legacy listeners. The old 60 ms
debounced `_forceMobileViewportReflow()` (which promoted `.layout` to its own
compositing layer on every visualViewport event — the iOS scroll-position dropper)
now runs **only** on the legacy path, i.e. when the module is absent.

## 3. CSS contract

- `html{height:100%;overflow:hidden;overscroll-behavior:none}` — the document never
  scrolls; the transcript is the only scroller, so a `visualViewport` pan cannot drag
  the composer off screen.
- Composer padding is `calc(14px + max(env(safe-area-inset-bottom), var(--keyboard-bottom-inset)))`
  — one inset, never two stacked; `!important` phone rule at `≤640px` matches.
- Split mode: `#mainChat` height is `calc(var(--app-vh) * var(--split-chat-ratio,45))`
  with `min-height:0; overflow:hidden`, panel `top` uses the same expression, so the
  panel can never displace the chat/composer. `html[data-keyboard-open="1"]` raises the
  chat ratio to 58 and the split composer keeps only the home-indicator gap (the
  keyboard is already inside `--app-vh`; subtracting it twice pushed the composer out).
- Modes: additive rules only, on top of the existing `html.mars-mode-*` block —
  `full` uncaps `.mars-process-window-stream`, `window` caps it at
  `min(calc(var(--app-vh)*38),260px)`, `hide-activity` hides the window; quiet keeps
  the existing `display:none` set. `.mars-process-window.is-collapsed` stays tappable
  when reasoning is hidden.
- `content-visibility:auto` is **not** reintroduced anywhere.

## 4. Boot-time mode classes (Settings not opened yet)

`boot.js` projects settings to `<html>` once per settings path, through the same
resolver `ui.js` uses, before Settings is ever opened:

- `_applyChatUxSettings()` — used by `_applyBootChatUxSettings()` and by the
  Settings-driven path; `ui.js`'s `_applyThinkingModeClasses()` is delegated to when
  present so there is one class writer;
- called on the settings-success path and again on the settings-failure path
  (`stream_window` + expanded defaults), so a failed `/api/settings` no longer leaves
  the transcript without mode classes;
- legacy aliases resolve identically to `ui.js`: `transparent_stream→stream_full`,
  `hide_all_activity→stream_quiet`, `compact_worklog→stream_window`, empty→`stream_window`;
- exactly one of `mars-mode-full|window|quiet` is ever present.

### AppearanceStore touchpoint (coordinator integrates after the appearance author)

Boot already calls an **optional** API and never requires the file to exist:

```js
window.MarsAppearanceStore || window.AppearanceStore   // either global name
store.get(key)        -> value | {value: ...}          // per-key read
store.subscribe(fn)   -> unsubscribe                   // optional
```

Keys read: `chat_activity_display_mode`, `thinking_mode`, `show_thinking`,
`show_tool_calls`, `auto_collapse_previous_turns`, `transparent_stream_event_timestamps`.
If the store is absent (today) server settings stay authoritative; if it appears,
its values win over the boot snapshot and each change re-projects the classes
(subscription bound once). Nothing in `boot.js` loads or imports `appearance_store.js`,
so the appearance zone can ship the file without touching layout files.

## 5. Checks run (this worktree)

```
node --check static/boot.js && node --check static/viewport_controller.js
node scripts/test_upgrade_layout_viewport.js      # PASS 15 checks
node scripts/test_upgrade_layout_chat_modes.js    # PASS 13 checks
HOME=$(mktemp -d) HERMES_HOME=$(mktemp -d) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /opt/hermes-webui/.venv/bin/python3 -m pytest \
  tests/test_upgrade_layout_viewport.py tests/test_upgrade_layout_modes.py \
  tests/test_upgrade_layout_css.py -q             # 19 passed
git diff --check
```

Regression sweep over neighbouring files (same command, no other flags):
`test_mobile_layout.py test_issue3820_chat_activity_display_mode.py
test_issue3595_activity_default_expanded.py test_activity_window_runtime.py
test_issue5545_three_panel_layout.py test_issue4856_mobile_transcript_unscrollable.py
test_2111_ios_pwa_bottom_scroll_stutter.py` → 146 passed, 2 failed, both
**pre-existing at base `ebb0f0da6`** (verified with `git show HEAD:...`):

- `test_mobile_layout.py::test_viewport_disables_page_zoom_for_native_pwa_shell` —
  `static/index.html` meta viewport has no `maximum-scale=1, user-scalable=no`.
  `index.html` is the integration zone; left untouched.
- `test_2111_ios_pwa_bottom_scroll_stutter.py::test_scroll_controls_are_overlays_outside_messages_scroller`
  — asserts the single-line string `.scroll-to-bottom-btn{position:absolute;`, but the
  rule is written multi-line (`.scroll-to-bottom-btn{\n    position:absolute;`) at base
  as well, so it fails on formatting, not behaviour. Not a layout-zone change.

## 6. Known limits / follow-ups for the coordinator

- `data-keyboard-open` (and therefore the 58% split ratio) exists only when the
  controller is loaded; the CSS fallback keeps the 45% split, which is the old behaviour.
- Safe-area bottom is measured with a throwaway probe element; it is re-measured on
  `orientationchange` and on `pageshow`, not continuously.
- The legacy `_forceMobileViewportReflow()` path is kept for bundles without the
  module; delete it (and `_isTouchKeyboardViewport`) once the script is in `index.html`
  for a release.
- `tests/test_mobile_layout.py` still asserts the old inline
  `visualViewport`-setup shape; it passes because the bootstrap block keeps the
  `if(window.visualViewport){ _syncKeyboardBottomInset(); ... }` form, but it should be
  rewritten against the module in a follow-up (render/integration zone, not edited here).
