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

---

# 7. Integration pass (branch `upgrade-20261022-integration`) — layout/browser GREEN

Files touched in this pass, all layout-owned:

| file | change |
|---|---|
| `static/style.css` | tooltip anchor in right-anchored containers; activity-window bound single owner |
| `scripts/upgrade_layout_overflow.mjs` | new — real-Chromium layout/overflow probe (6 frames) |
| `scripts/upgrade_layout_overflow.py` | new — driver that owns the mock server + probe lifetime |
| `HANDOFF-layout.md` | this section |

`static/viewport_controller.js` needed no change in this pass (already wired and
verified by `scripts/test_upgrade_layout_viewport.js`). Nothing else was edited:
`ui.js`, `panels.js`, `chat_render_*.js`, `index.html`, `api/*` belong to other authors
and their uncommitted work is left alone.

## 7.1 Horizontal overflow — the mandatory frames

Real Chromium, real app, mock backend, workspace panel forced open through boot's own
`_setWorkspacePanelMode()` (i.e. the state a user reaches by clicking the panel icon).

| frame | `documentElement.scrollWidth` | `innerWidth` | in-flow offenders |
|---|---|---|---|
| 1440x900, panel open | 1440 | 1440 | none |
| **1024x720, panel open** | **1024** | **1024** | **none** |
| 1024x768, panel open | 1024 | 1024 | none |
| 1024x720, panel open + sidebar narrowed | 1024 | 1024 | none |
| 1024x520, panel open | 1024 | 1024 | none |
| **393x851, panel open** | **393** | **393** | **none** |

At 1024 with the panel open the columns are rail 48 + sidebar 278 + chat 420 + panel
278; the chat column sits exactly on its `min-width:420px` floor and still does not
push the page. `.main{overflow:hidden}` absorbs the residue, so nothing escapes into a
page scrollbar.

Two things that *look* like overflow and are not — do not "fix" them with a clamp:

- `.rightpanel` reports `scrollWidth 333` vs `clientWidth 277`, `.composer-footer`
  `418` vs `378`. Both numbers come from **tooltip pseudo-elements**
  (`.has-tooltip::after`, `white-space:nowrap`, absolutely positioned, `opacity:0`):
  they inflate the scrollWidth of the nearest scrolling ancestor while being invisible.
  Clamping `overflow` there is exactly what slices the tooltip — see 7.2.
- The `.panel-icon-btn` / `.icon-btn` clusters have `scrollWidth > clientWidth` for the
  same reason.

## 7.2 Real defect found: tooltips in right-anchored containers were sliced

Measured at 1024x720 with the panel open, before the fix:

| trigger | bubble | clip box | result |
|---|---|---|---|
| `#btnWorkspacePrefs` ("Параметры рабочей области", 221px) | 970 → 1191 | `.rightpanel` 746 → 1024 | **167px cut off** |
| `#btnUploadWorkspace`, `#btnClearPreview` | ~1075 right edge | `.rightpanel` right 1024 | cut |
| `#btnMic` ("Диктовка") | 679 → 766 | `.main` right 746 | **20px cut off** |
| `#btnSend` (with the control geometry) | right 898 | `.main` right 746 | cut |

The default bubble opens to the **right** of its trigger, which is the wrong side for
the two containers that sit against a hard right edge. Fixed in `style.css` by mirroring
the anchor — the same geometry the existing `.has-tooltip--bottom-right` /
`.has-tooltip--left` variants use — scoped by **container**, not by per-button class, so
no `panels.js`/`index.html` edit is needed:

```css
.rightpanel .has-tooltip:not(.has-tooltip--left):not(.has-tooltip--bottom):not(.has-tooltip--bottom-right)::after,
.composer-right .has-tooltip:not(.has-tooltip--left):not(.has-tooltip--bottom):not(.has-tooltip--bottom-right)::after{
  left:auto;right:calc(100% + 8px);
}
.rightpanel .has-tooltip--bottom::after{left:auto;right:0;transform:none;}
```

No label is truncated, no `max-width`, no `text-overflow`, no `overflow:hidden` added:
the bubbles are whole and fully inside their clip box. The probe proves this is a real
measurement, not a tautology — it re-scans the same triggers with the old geometry
re-injected and asserts that the check then **does** report clipping (5 triggers per
frame). If that control ever stops biting, the check is worthless and will fail loudly.

## 7.3 Bounds ownership — second owner removed from the effective cascade (→ render)

Two writers were setting the vertical bound of the same element
(`.mars-process-window-stream`, which carries `data-activity-body="1"`):

- **CSS** (`style.css`): `220px` base, `none` in `stream_full`, `min(38vh,260px)` in
  `stream_window`;
- **renderer** (`chat_render_activity_block.js::setMode()`, line ~244): inline
  `body.style.maxHeight = WINDOW_BODY_MAX_PX + 'px'` (260px) in `stream_window`, `''`
  otherwise.

Inline beats a normal declaration, so the renderer silently owned the effective value:
the `38vh` part never applied. Concretely, on a 393x520 phone with the keyboard up the
window stayed at a flat 260px (~50% of the visual viewport) instead of 197px, and the
220px/260px literals contradicted each other on desktop.

Resolution inside the layout zone (no render file edited):

- the number now exists **once**, as `--activity-window-max:260px` on
  `.mars-process-window`;
- the base stream rule reads `min(38vh, var(--activity-window-max))` (with the
  `--app-vh` variant, which is the one that tells the truth under the keyboard);
- both `html.mars-mode-*` rules are `!important`, so the CSS cap is the effective value
  regardless of what the renderer writes, and a mode switch still flips the same
  element between `none` and the cap.

Verified in the browser at 1024x520: inline style is `260px`, computed `max-height` is
`197.6px` = `min(38vh,260px)`. In `stream_full`: inline `''`, computed `none`.

**Renderer follow-up (one-line change, render zone):** in `setMode()` drop
`body.style.maxHeight = …` and keep `body.style.overflowY = …`. Then the `!important`
flags in `style.css` can be removed and CSS stays the only owner with no cascade
override. Until then the `!important` is the guard, not a stylistic choice. The legacy
shape path in `ui.js` (`_applyActivityModeToContainers`, inline maxHeight on
`.tool-worklog-body/.tool-worklog-list/.tool-call-group-body`) is a *different* element
with no CSS bound, so it is not a second owner and was left alone.

## 7.4 Checks run in this pass

```
node --check scripts/upgrade_layout_overflow.mjs
node scripts/test_upgrade_layout_viewport.js            # PASS
node scripts/test_upgrade_layout_chat_modes.js          # PASS

# shared browser harness, the three required scenarios, real Chromium
python3 scripts/upgrade_browser_harness.py --app-root . --viewports desktop,mobile \
  --only completion_resize_composer_geometry,long_code_and_table,modes_settled_cycle \
  --out /tmp/upgrade-layout-browser.json
#   PASS desktop/mobile × 3 scenarios — 6/6 runs, 78 checks, 0 failed

# layout probe (own harness), 6 frames incl. 1024+panel and 393+panel
python3 scripts/upgrade_layout_overflow.py --app-root . \
  --out /tmp/upgrade-layout-overflow.json
#   PASS 6/6 frames, 113 checks, 0 failed
```

Report files:

- `/tmp/upgrade-layout-browser.json` — shared harness, 3 scenarios × desktop/mobile.
- `/tmp/upgrade-layout-overflow.json` — layout probe, 6 frames (geometry, tooltips,
  music iframe, bound ownership, page errors, external requests).

`/tmp/upgrade-candidate-browser.json` was **overwritten** by an earlier subset run of
this zone before the "do not overwrite" instruction arrived. It now holds the 6/6
layout subset, not the coordinator's 12-scenario candidate report
(`12 scenarios / 6 passed / 146 checks / 9 failed`). The closest surviving full-suite
snapshot from that period is `/tmp/rep7.json`
(`12 / 6 / 146 / 14`, `ran_at 2026-10-22T15:32:56Z`); the coordinator's per-check
details for the 9 failures are not recoverable from this zone. All further layout
outputs go to the two paths above.

## 7.5 Probe usage

```
python3 scripts/upgrade_layout_overflow.py --app-root .                       # all 6 frames
python3 scripts/upgrade_layout_overflow.py --app-root . --only mobile-393     # one frame
UPGRADE_LAYOUT_DEBUG=1 python3 scripts/upgrade_layout_overflow.py --app-root . # stage markers
```

The driver owns the mock server (`127.0.0.1:0`, port read from the child's stdout, one
successful `GET /` before Chromium starts) and stops it through its own Popen process
group — never by port, never by a broad `pkill`. The node probe has a 60s per-frame
watchdog and exits non-zero on timeout: a hung frame can never be reported as a pass.
