# HANDOFF — render core (render/modes/scroll)

Worktree: `/root/workspace/webui-upgrade-20261022/render` · branch commit `53a1d1fdc` · no push.
Owner of `static/ui.js` in this worktree. Files touched: `static/ui.js`, `static/index.html`,
3 new modules, 2 new test files + `scripts/lib/mini_dom.mjs`.

## What landed (behaviour, not cosmetics)

1. **Incremental DOM patch instead of transcript wipe.** `renderMessages()` no longer runs
   `inner.innerHTML=''`. It calls `ChatRenderPatch.begin(inner)`, every row is placed through
   `_placeRow()` → `ChatRenderPatch.place()`, and `_finishPatch()` commits right before
   `_scrollAfterMessageRender()`. Unchanged row → same live node (`isEqualNode`), changed row →
   swapped in its own slot, vanished row → removed at finish. Falls back to the old wipe when the
   module is absent (old cached HTML / harnesses).
2. **Quiet → full restoration without a following event.** `_hideLiveActivityForFinalAnswerOnly()`
   now HIDES (inline `display:none` + `data-activity-hidden`) instead of `clearLiveToolCards()` +
   `.remove()`. The rows, the SMD parser target and the disclosure state survive; switching
   quiet → full re-shows the same nodes on the spot.
3. **One disclosure shell for window and full.** `ensureLiveWorklogContainer()` builds the same
   `.mars-process-window` chrome in both modes (`data-mode` distinguishes bounded vs unbounded
   body). Full no longer renders a bare `.live-worklog`, so a window → full switch no longer drops
   the header or the reader's choice. `_toolWorklogListEl()` returns the inner stream body in both
   modes.
4. **One stable turn key for disclosure.** `_activityKeyForLiveTurn()` now returns the turn-ordinal
   key (`<sid>:t<ordinal>`, fallback `<sid>:r<rawIdx>`, last resort `live:<streamId>`), and the
   settled block builds the SAME key (`_turnActivityKeyForRawIdx(rawIdx)`), with
   `anchor-scene:<rawIdx>` only as fallback. `_toggleActivityWindow()` persists to that key.
   Legacy keys are still adopted (`_copyActivityDisclosureState('live:<sid>' → key)`), so nothing
   a user already collapsed re-opens.
5. **Consolidated scroll writes + stale settle cancellation.** `ChatScrollController` is the single
   owner of programmatic `scrollTop` writes: one read + at most one write per frame, extra writes
   coalesce into the next frame, a no-op when already at the bottom, `refused` when the reader is
   unpinned (no `window._autoScrollFollow` / `_messageUserUnpinned` / non-message scroll intent),
   and a monotonic generation so a superseded settle (ResizeObserver tick, quiet timer, final
   timer) can never write. `_cancelBottomSettle()` (fired on real user input) also cancels the
   controller's pending write.
6. `chatActivityMode()` normalises legacy stored values through `ChatActivityBlock.normalizeMode`,
   so ui.js and the module cannot disagree about `transparent_stream` / `hide_all_activity` /
   `compact_worklog` / `full` / `window` / `quiet`.

## API the other workers need

### Immediate mode apply (panels/boot worker)
`ui.js` exposes the bridges; call these and the DOM updates synchronously, no SSE event needed:

```js
window._hideLiveActivityForFinalAnswerOnly()  // quiet: hides live rows, keeps them
_applyThinkingModeClasses()                   // re-applies html.mars-* mode classes
```

For a full module-driven apply, the contract is:

```js
const AB = ChatActivityBlock.create({ document: document });       // storage defaults to localStorage
const win = AB.ensureChrome(hostEl, { turnKey, mode, state, label, duration, count });
// win = {root, header, body, list, created}
AB.setMode(win.root, 'stream_full');            // 'stream_full' | 'stream_window' | 'stream_quiet'
AB.setState(win.root, {state:'settled', label:'Worked for 12s', count:7});
AB.disclosureFor(win.root, {turnKey, legacyKeys:['live:'+streamId, 'anchor-scene:'+rawIdx]});
AB.setDisclosure(win.root, false, {turnKey});   // persists on the stable key
AB.syncVisibility(win.root);                    // hides an empty settled frame
```

`setMode()` is idempotent and instant: it only flips `data-mode` + the body's
`maxHeight`/`overflowY`, it never rebuilds rows. That is the "apply without the next event" path.

### Script order (boot worker — already applied in `static/index.html`)
These three must load BEFORE `chat_activity.js` and `ui.js` (all `defer`, so document order decides):

```html
<script src="static/chat_scroll_controller.js?v=__WEBUI_VERSION__-render-core-v1" defer></script>
<script src="static/chat_render_patch.js?v=__WEBUI_VERSION__-render-core-v1" defer></script>
<script src="static/chat_render_activity_block.js?v=__WEBUI_VERSION__-render-core-v1" defer></script>
```

Each module is a UMD-ish global (`ChatScrollController`, `ChatRenderPatch`, `ChatActivityBlock`)
and also `module.exports`, so node harnesses require them directly. If `index.html` is owned by
the boot worker, re-apply those three tags in that exact order.

### `ChatRenderPatch` contract (for anyone else touching row building)
```js
const ctx = ChatRenderPatch.begin(container);   // snapshot live rows by key
ChatRenderPatch.place(ctx, node, {reuse:false}); // reuse:false for subtrees still growing
ChatRenderPatch.finish(ctx);                     // drop unmatched rows
```
Keys: `data-render-key` → `data-recycle-key` → `id`. A row with no key is always replaced. New row
builders should stamp `data-render-key` (user rows already carry a stable `id`; assistant turns
carry `data-recycle-key`).

### `ChatScrollController` contract
```js
const ctl = ChatScrollController.create({getScroller, getContent, canFollow, onWrite, raf, caf, makeObserver});
ctl.write('reason', {immediate:true, explicit, force});  // force skips canFollow
ctl.settle({force, explicit, reason});                   // anchor now + RO + quiet + final
ctl.cancel('user-scroll');                               // bumps generation, kills timers
ctl.stats();                                             // writes/reads/noop/refused/coalesced/staleCancelled
```
`ui.js` keeps its own singleton (`_messageScrollController()`) wired to `#messages` / `#msgInner`.

## Tests (real behaviour, isolated)
- `scripts/test_upgrade_render_core.mjs` — 49 assertions against the real modules over
  `scripts/lib/mini_dom.mjs` (no jsdom, no network, no pip): node identity preserved across a
  re-render, changed row swapped in slot, removed row dropped, quiet hides without removing and
  full restores the same nodes, window/full one shell, live→settled disclosure key equality,
  one write per frame, no-op at bottom, refusal for unpinned, cancelled settle never writes.
- `tests/test_upgrade_render_core.py` — pytest wrapper (+ assertion-count floor ≥45).
- GREEN here: `pytest tests/test_upgrade_render_core.py tests/test_scroll_harness_behavior.py
  tests/test_activity_window_runtime.py tests/test_chat_activity_adapter.py
  tests/test_a11y_transcript_landmarks.py -q` → 13 passed; `node scripts/scroll_harness.mjs` →
  all four VERDICTs PASS (a/b/c extraction).
- The full `pytest tests` run did not finish inside the checkpoint window (it needs the app
  fixture/servers); it must be run by the coordinator on the release gate, not skipped silently.

## Remaining gaps (explicit, not cosmetic)
1. **`_restoreMessageScrollSnapshot()` still writes `scrollTop` directly** (unpinned-restore path in
   `_scrollAfterMessageRender`). It is the last programmatic writer outside the controller. Moving
   it needs a `ctl.restore(top)` that respects the frame budget; deferred because it is the path
   that keeps an unpinned reader in place, and a wrong refactor there is a jump regression.
2. **Virtualization spacers** are unkeyed, so each render replaces the top/bottom spacer nodes
   (they are cheap and layout-neutral, but they do count as `replaced` in `stats`). Keying them by
   `before:<topPad>` / `after:<bottomPad>` would make a mid-list scroll fully no-touch.
3. **`_recycleStash` + patch overlap.** Recycled nodes are handed to `place()` as the SAME node, so
   they are moved rather than rebuilt — correct, but the stash is now partly redundant with the
   patch. Removing it is a separate change (it also serves the virtual window path).
4. **Settled-block header label** uses `activity_mode_short_stream_full`; if that i18n key is
   missing the code falls back to the window label rather than printing the raw key. The key should
   be added by the l10n owner.
5. **CSS**: the shared shell relies on `data-mode="stream_full|stream_window|stream_quiet"` and
   `[data-activity-expanded]`. `static/style.css` is owned by the style worker; it must not assume
   the old `.live-worklog`-only full mode, and should drop any `max-height` it sets on
   `.mars-process-window-stream` (JS now owns it per mode).
6. `#messages` scroll anchoring (`overflow-anchor`) was not touched — it interacts with the patch
   (fewer layout invalidations) but is style-owned.

## Mobile render blocker — CLOSED (6786602a1)

Root causes, all in the render path (the harness was never the cause of the
transcript failures; see "cross-viewport state" below for the one that was):

1. **Factory vs instance seam.** `ui.js` handed the `ChatActivityBlock` *factory* to
   `_applyActivityModeToHost`, so every live render threw `AB.setMode is not a
   function` and took the turn with it. `_activityModeModule()` now resolves the
   factory to one page-lifetime instance via `create()` (or returns `null` when the
   module is absent) and keeps the pure helpers (`normalizeMode`, `turnKey`) reachable.
2. **The live turn was deleted mid-stream.** `chat_render_patch.finish()` removed any
   old child the current render did not place. A mid-stream session snapshot carries
   no row for the live assistant turn (it has no `_live` marker), so the patch deleted
   the node the stream was appending into → `len=35` / `len=89` "blanked answer".
   `begin(container,{protect})` + `finish()` now keeps a protected node and counts it
   as `kept` in `stats`.
3. **Out-of-cursor anchors flipped the order.** Compression/handoff cards insert
   themselves by direct DOM insertion; the cursor then stayed on the previous row and
   the next `place()` wrote *in front of* the card. Added `Patch.adopt(ctx,node)` (only
   a node that really landed at `cursor.nextSibling` advances it) and `ui.js` adopts
   those anchors.
4. **Inline `max-height` beat the CSS bound.** `chat_render_activity_block` wrote
   `body.style.maxHeight=260px` inline, which overrode `--activity-window-max`, so the
   viewport-aware `min(38vh, --activity-window-max)` cap never applied and the window
   stayed flat 260px (≈half the visual viewport on a phone with the keyboard up). The
   bound now belongs to CSS; only `overflow-y` stays inline.
5. **Reasoning pin leaked across provider lanes.** A bare model name now resolves on
   its own lane only: qualified `@provider` keys win, legacy bare keys still work, and
   a pin made on one lane is not reported for another.

### Cross-viewport state (coordinated with the harness owner)
The combined desktop→mobile failures were a **harness state leak, not product**:
`/__mock/config` did not restore `settings_default.json`, so desktop's
`chat_activity_display_mode=stream_full` survived into the mobile boot (observed:
`settingsBeforeBoot.chat_activity_display_mode=stream_full`, mobile boots
`stream_quiet`, cycles to `stream_window`/`Full` and the activity window renders
instead of the streaming text). Fixed in `f63990557` (`reset_settings` / `seed`
re-reads the fixture). Verified in isolation: with the leak the mobile run fails
exactly the transcript checks; with a fresh backend the same code is green.

### Tests (green on this SHA)
- `node scripts/test_upgrade_render_core.mjs` → **80 assertions** (factory/instance
  seam, `protect`/`kept`, `adopt` ordering, window bound ownership).
- `node tests/js/render_consumer_contract.mjs` → **30 checks** — the `mini_dom`
  `cloneNode` gap is closed in the test's own helper (`cloneTree`), so the contract
  exercises real clone semantics without teaching the shared mini_dom a new API.
- `node tests/js/reasoning_pin_behavior.js` → 24 checks; `pytest
  tests/test_reasoning_pin_behavior.py` → 4 passed.
- Real Chromium, mobile-only (fresh backend, `--viewports mobile --only
  modes_live_quiet_to_full,stream_reading_scroll`) → **2/2 scenarios, 24/24 checks**:
  `/tmp/render-mobile-only.json`.
- Combined desktop+mobile on the clean tree (post-harness fix) → 4/4, 52 checks, by
  the coordinator.
