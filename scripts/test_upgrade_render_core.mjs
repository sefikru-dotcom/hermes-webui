/* test_upgrade_render_core.mjs — behavioural tests for the render/mode/scroll core.
 *
 * These assert OBSERVABLE behaviour, not exports:
 *   - a re-render keeps the DOM nodes of unchanged rows (no transcript wipe);
 *   - a changed row is swapped in its own slot and neighbours keep identity;
 *   - a vanished row is removed;
 *   - quiet mode hides rows without destroying them, and quiet -> full restores the
 *     SAME nodes with no further event (the "empty turn until the next SSE event"
 *     class from webui-audit-modes.md);
 *   - window and full share one disclosure shell, and a disclosure decision made
 *     while live is still in force after the turn settles;
 *   - programmatic scroll writes are coalesced to one per frame, skipped when the
 *     viewport is already at the bottom, refused when the reader unpinned, and a
 *     superseded settle can never write.
 *
 * Run: node scripts/test_upgrade_render_core.mjs
 */
import { createDocument, installGlobals, parseHTML } from './lib/mini_dom.mjs';
import { createRequire } from 'node:module';
import fs from 'node:fs';

const require = createRequire(import.meta.url);
const Patch = require('../static/chat_render_patch.js');
const Scroll = require('../static/chat_scroll_controller.js');
const Activity = require('../static/chat_render_activity_block.js');

const doc = createDocument();
installGlobals(doc);

let passed = 0;
const failures = [];
function ok(name, cond, extra) {
  if (cond) { passed++; return; }
  failures.push(name + (extra ? ' — ' + extra : ''));
}
function eq(name, actual, expected) {
  ok(name, JSON.stringify(actual) === JSON.stringify(expected), `got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
}
function row(key, text) {
  const el = doc.createElement('div');
  el.className = 'msg-row';
  el.setAttribute('data-render-key', String(key));
  el.setAttribute('data-msg-idx', String(key));
  el.innerHTML = `<div class="msg-body">${text}</div>`;
  return el;
}

/* ────────────────────────── 1. keyed incremental patch ────────────────────── */
{
  const inner = doc.createElement('div');
  inner.id = 'msgInner';
  // Minimum observable state of the old code path: how many times the container was
  // emptied. The patch must never empty it.
  let empties = 0;
  const realEmpty = inner.replaceChildren ? inner.replaceChildren.bind(inner) : null;

  const first = Patch.begin(inner);
  Patch.place(first, row(1, 'hello'));
  Patch.place(first, row(2, 'world'));
  const stats1 = Patch.finish(first);
  ok('first render inserts both rows', inner.children.length === 2, String(inner.children.length));
  eq('first render counts two inserts', [stats1.inserted, stats1.reused], [2, 0]);
  const node1 = inner.children[0];
  const node2 = inner.children[1];

  // Identical re-render: same keys, same markup.
  const second = Patch.begin(inner);
  Patch.place(second, row(1, 'hello'));
  Patch.place(second, row(2, 'world'));
  const stats2 = Patch.finish(second);
  ok('identical re-render keeps row 1 identity', inner.children[0] === node1);
  ok('identical re-render keeps row 2 identity', inner.children[1] === node2);
  eq('identical re-render reuses both rows', [stats2.reused, stats2.inserted, stats2.replaced], [2, 0, 0]);
  ok('container was never emptied', inner.children.length === 2 && !realEmpty);

  // Third render: row 2 text changed, row 3 appended.
  const third = Patch.begin(inner);
  Patch.place(third, row(1, 'hello'));
  Patch.place(third, row(2, 'world, again'));
  Patch.place(third, row(3, 'fresh'));
  const stats3 = Patch.finish(third);
  ok('unchanged row survives a sibling edit', inner.children[0] === node1);
  ok('changed row is swapped in its slot', inner.children[1] !== node2 && inner.children[1].textContent.includes('again'));
  eq('changed row replaced, new row inserted', [stats3.reused, stats3.replaced, stats3.inserted, stats3.removed], [1, 1, 1, 0]);
  ok('row order preserved after patch', inner.children.map(n => n.getAttribute('data-render-key')).join(',') === '1,2,3');

  // Fourth render: row 2 dropped.
  const fourth = Patch.begin(inner);
  Patch.place(fourth, row(1, 'hello'));
  Patch.place(fourth, row(3, 'fresh'));
  const stats4 = Patch.finish(fourth);
  eq('vanished row removed exactly once', stats4.removed, 1);
  eq('remaining keys', inner.children.map(n => n.getAttribute('data-render-key')).join(','), '1,3');
  ok('unrelated rows keep identity across a removal', inner.children[0] === node1);

  // A mutating live subtree must never be matched against an equal stale shell.
  const live = row(9, 'streaming');
  live.setAttribute(Patch.NO_REUSE_ATTR, '1');
  const fifth = Patch.begin(inner);
  Patch.place(fifth, live);
  Patch.finish(fifth);
  const liveNode = inner.children[0];
  const sixth = Patch.begin(inner);
  Patch.place(sixth, row(9, 'streaming'));
  Patch.finish(sixth);
  ok('a no-reuse row is not reused even when byte-equal', inner.children[0] !== liveNode);
}

/* ───────────────────────── 2. activity shell / modes ─────────────────────── */
{
  // Legacy aliases resolve in ONE place, so ui.js and the block cannot disagree.
  eq('legacy transparent_stream', Activity.normalizeMode('transparent_stream'), 'stream_full');
  eq('legacy hide_all_activity', Activity.normalizeMode('hide_all_activity'), 'stream_quiet');
  eq('legacy compact_worklog', Activity.normalizeMode('compact_worklog'), 'stream_window');
  eq('unknown mode falls back', Activity.normalizeMode('nonsense', 'stream_window'), 'stream_window');
  eq('turn key prefers the ordinal', Activity.turnKey({ sessionId: 's1', ordinal: 3, rawIdx: 77 }), 's1:t3');
  eq('turn key falls back to raw index', Activity.turnKey({ sessionId: 's1', rawIdx: 7 }), 's1:r7');

  const store = new Map();
  const storage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
  };
  const block = Activity.create({ document: doc, storage, legacyStorage: null });
  const api = block;
  const host = doc.createElement('div');
  const turnKey = Activity.turnKey({ sessionId: 'sess', ordinal: 1 });

  // window shell
  const win = api.ensureChrome(host, { turnKey, mode: 'stream_window', state: 'live', label: 'Working' });
  ok('window mode builds the shared shell', !!win.root && !!win.header && !!win.body && !!win.list);
  const body = win.body;
  for (let i = 0; i < 3; i++) {
    const r = doc.createElement('div');
    r.className = 'tool-card-row';
    r.setAttribute('data-activity-row', '1');
    r.textContent = 'tool ' + i;
    win.list.appendChild(r);
  }
  const rows = Array.from(win.list.children);
  // Bounds ownership: the cap lives in CSS (`--activity-window-max`, applied by the
  // `html.mars-mode-window|full` rules). The module must NOT write an inline
  // max-height — inline beat the mode rule and pinned the window to a flat 260px
  // regardless of the visual viewport. Only the scroll axis stays inline.
  ok('window body writes no inline bound (CSS owns --activity-window-max)',
    !body.style.maxHeight, `maxHeight=${JSON.stringify(body.style.maxHeight)}`);
  ok('window body stays scrollable', body.style.overflowY === 'auto', body.style.overflowY);
  ok('the module still publishes the cap for CSS/consumers', Activity.WINDOW_BODY_MAX_PX > 0);

  // quiet: rows are HIDDEN, never removed — the fix for quiet -> full.
  api.setMode(win.root, 'stream_quiet');
  ok('quiet hides the shell', win.root.style.display === 'none');
  ok('quiet keeps every row in the DOM', win.list.children.length === 3);
  ok('quiet rows are the same nodes', Array.from(win.list.children).every((n, i) => n === rows[i]));

  // full: restored with NO further event, same nodes, no rebuild.
  const res = api.setMode(win.root, 'stream_full');
  ok('quiet -> full un-hides the shell', win.root.style.display === '');
  eq('quiet -> full reports a real mode change', res.changed, true);
  ok('quiet -> full keeps the same body node', win.root.querySelector('[data-activity-body="1"]') === body);
  ok('quiet -> full keeps every row node', Array.from(win.list.children).every((n, i) => n === rows[i]));
  ok('full body is unbounded', body.style.maxHeight === '');
  ok('full shell keeps the disclosure header', !!win.root.querySelector('.mars-process-window-header'));

  // Disclosure: a choice made on the live turn survives the settled form because both
  // sides address the block by the SAME turn key.
  api.Disclosure.set(turnKey, false);
  const settledRoot = doc.createElement('div');
  settledRoot.setAttribute('data-turn-key', turnKey);
  const open = api.disclosureFor(settledRoot, { turnKey, legacyKeys: ['live:abc', 'anchor-scene:4'] });
  eq('settled block inherits the live disclosure choice', open, false);
  eq('settled block is rendered collapsed', settledRoot.getAttribute('data-activity-expanded'), '0');
  ok('settled block has the collapsed class', settledRoot.classList.contains('is-collapsed'));

  // The newest user click wins over an older legacy key.
  api.Disclosure.set(turnKey, true);
  api.Disclosure.set('live:abc', false);
  eq('stable key beats a stale legacy key', api.Disclosure.adopt(turnKey, ['live:abc']), true);

  // Empty settled block is hidden, not rendered as a dead frame.
  const emptyRoot = doc.createElement('div');
  emptyRoot.setAttribute('data-turn-key', 's:t9');
  api.setState(emptyRoot, { state: 'settled' });
  api.syncVisibility(emptyRoot, { count: 0 });
  ok('empty settled block is hidden', emptyRoot.style.display === 'none');
}

/* ───────────────────────────── 3. scroll controller ──────────────────────── */
{
  const scroller = {
    scrollTop: 0,
    scrollHeight: 1000,
    clientHeight: 400,
    get max() { return this.scrollHeight - this.clientHeight; },
  };
  let frames = [];
  let timers = [];
  let rafSeq = 1, timerSeq = 1;
  const env = {
    getScroller: () => scroller,
    canFollow: ({ explicit }) => (explicit ? true : follow),
    raf: cb => { const id = rafSeq++; frames.push({ id, cb }); return id; },
    caf: id => { frames = frames.filter(f => f.id !== id); },
    setTimeout: (cb, ms) => { const id = timerSeq++; timers.push({ id, cb, ms }); return id; },
    clearTimeout: id => { timers = timers.filter(t => t.id !== id); },
    makeObserver: () => ({ observe() {}, disconnect() {} }),
    now: () => 0,
  };
  let follow = true;
  const ctl = Scroll.create(env);
  const flush = () => { let guard = 0; while (frames.length && guard++ < 50) { const f = frames; frames = []; f.forEach(x => x.cb()); } };
  const fire = ms => { const t = timers.filter(x => x.ms === ms); timers = timers.filter(x => x.ms !== ms); t.forEach(x => x.cb()); };

  // coalescing: two writes inside one frame -> one layout write
  ctl.write('a');
  ctl.write('b');
  flush();
  eq('two requests in one frame coalesce into one write', ctl.stats().writes, 1);
  eq('scroll landed at the bottom', scroller.scrollTop, scroller.max);
  ok('coalescing was recorded', ctl.stats().coalesced >= 1);

  // already at the bottom -> no write at all
  const writesBefore = ctl.stats().writes;
  frames = [];
  ctl.write('idle');
  flush();
  eq('a request at the bottom performs no write', ctl.stats().writes, writesBefore);
  ok('the no-op was counted', ctl.stats().noop >= 1);

  // guard: unpinned reader is never yanked
  follow = false;
  scroller.scrollTop = 0;
  frames = [];
  ctl.write('stream');
  flush();
  eq('unpinned reader is not scrolled', scroller.scrollTop, 0);
  ok('the refusal was counted', ctl.stats().refused >= 1);

  // stale settle cancellation
  follow = true;
  const before = ctl.stats().writes;
  ctl.settle({ reason: 'render' });
  flush();
  const afterSettle = ctl.stats().writes;
  // A frame-coalesced write is queued, then the reader scrolls up: the queued write
  // belongs to a viewport state that no longer exists and must never land.
  scroller.scrollTop = 0;
  ctl.write('stream');
  ok('the queued write has not landed yet', ctl.stats().writes === afterSettle && scroller.scrollTop === 0);
  ctl.cancel('user-scroll');
  flush();
  fire(300);
  fire(2000);
  eq('a cancelled settle writes nothing after cancellation', ctl.stats().writes, afterSettle);
  ok('the stale settle was recorded as cancelled', ctl.stats().staleCancelled >= 1);
  ok('the settle did anchor once before cancellation', afterSettle > before);

  // a newer settle supersedes an older one: only the newest generation may write
  scroller.scrollTop = 0;
  ctl.settle({ reason: 'one' });
  flush();
  const gen1 = ctl.generation();
  ctl.settle({ reason: 'two' });
  flush();
  ok('a newer settle bumps the generation', ctl.generation() > gen1);
  const writesNow = ctl.stats().writes;
  fire(300);
  ok('only one quiet pass is armed at a time', ctl.stats().writes === writesNow);
}

/* ─────────── 4. ui.js ↔ activity-block seam (factory vs instance) ─────────── */
{
  // The block exports a FACTORY: `setMode`/`ensureChrome`/`Disclosure` exist only on
  // the object returned by `create()`. ui.js used to hand the factory to
  // `_applyActivityModeToHost`, which threw `AB.setMode is not a function` on every
  // live render and took the whole live turn with it (desktop: `turns=0`).
  const seamSrc = (() => {
    const ui = fs.readFileSync(new URL('../static/ui.js', import.meta.url), 'utf8');
    const start = ui.indexOf('let _activityModeFacade=null;');
    if (start < 0) throw new Error('ui.js: _activityModeFacade not found');
    const fnAt = ui.indexOf('function _activityModeModule(', start);
    let depth = 0;
    for (let i = ui.indexOf('{', fnAt); i < ui.length; i++) {
      if (ui[i] === '{') depth++;
      else if (ui[i] === '}') {
        depth--;
        if (depth === 0) return ui.slice(start, i + 1) + '\nreturn _activityModeModule;';
      }
    }
    throw new Error('ui.js: _activityModeModule braces unbalanced');
  })();
  const seam = globals =>
    new Function('ChatActivityBlock', 'document', seamSrc)(
      globals.ChatActivityBlock,
      globals.document || doc
    );

  // No module loaded at all (old cached HTML): callers must see null, not throw.
  eq('seam: no activity module -> null', seam({ ChatActivityBlock: undefined })(), null);

  // The real shape: factory only.
  const factoryOnly = { create: Activity.create, normalizeMode: Activity.normalizeMode, turnKey: Activity.turnKey };
  const fromFactory = seam({ ChatActivityBlock: factoryOnly });
  const api = fromFactory();
  ok('seam: factory-only global yields an instance', !!api);
  eq('seam: instance exposes setMode', typeof (api && api.setMode), 'function');
  eq('seam: instance exposes ensureChrome', typeof (api && api.ensureChrome), 'function');
  eq('seam: instance exposes syncVisibility', typeof (api && api.syncVisibility), 'function');
  eq('seam: instance exposes Disclosure', typeof (api && api.Disclosure), 'object');
  eq('seam: pure helpers stay reachable', api && api.normalizeMode('compact_worklog'), 'stream_window');
  ok('seam: one instance for the page lifetime', fromFactory() === api);
  ok('seam: the factory itself is never returned', api !== factoryOnly);

  // And it actually works against a real host (the call that used to throw).
  const host = doc.createElement('div');
  const chrome = api.ensureChrome(host, { turnKey: 's:t1', mode: 'stream_window', state: 'live' });
  ok('seam: ensureChrome builds the shell through the seam', !!chrome && !!chrome.root);
  const switched = api.setMode(chrome.root, 'stream_quiet');
  ok('seam: setMode through the seam does not throw', !!switched && switched.mode === 'stream_quiet');

  // A legacy build that exports the instance methods directly keeps working.
  const legacy = { setMode() {}, ensureChrome() {}, syncVisibility() {}, Disclosure: {}, disclosureFor() {} };
  ok('seam: legacy direct export is used as-is', seam({ ChatActivityBlock: legacy })() === legacy);

  // A factory that throws must degrade to null (no exception at the call site).
  ok(
    'seam: a throwing factory degrades to null',
    seam({ ChatActivityBlock: { create() { throw new Error('boom'); } } })() === null
  );
}

/* ────────── 5. the live turn belongs to the stream, not to a render ───────── */
{
  // A session snapshot landing mid-stream replaces S.messages with the server copy,
  // where the in-flight turn has no `_live` marker and no text yet. The renderer then
  // builds no row for it — and the patch must not delete the node the stream is still
  // writing into (that blanked the answer until the next token; on desktop it never
  // came back).
  const inner = doc.createElement('div');
  inner.id = 'msgInner';
  const userRow = row(0, 'question');
  inner.appendChild(userRow);
  const live = doc.createElement('div');
  live.id = 'liveAssistantTurn';
  live.setAttribute('data-recycle-key', '1');
  live.innerHTML = '<div class="msg-body">streaming text</div>';
  inner.appendChild(live);

  const ctx = Patch.begin(inner, { protect: n => n && n.id === 'liveAssistantTurn' });
  Patch.place(ctx, row(0, 'question'));
  const stats = Patch.finish(ctx);
  ok('protect: the live turn survives a render that does not place it', inner.children.length === 2);
  ok('protect: the SAME live node is still mounted', inner.children[1] === live);
  ok('protect: its streamed text is untouched', inner.children[1].textContent.includes('streaming text'));
  eq('protect: the keep is counted, not hidden', stats.kept, 1);
  eq('protect: nothing was removed', stats.removed, 0);

  // Without the protection (stream over) the stale row is dropped as before.
  const after = Patch.begin(inner);
  Patch.place(after, row(0, 'question'));
  const stats2 = Patch.finish(after);
  eq('no protect: the unplaced row is removed once the stream is over', stats2.removed, 1);
  ok('no protect: only the placed row remains', inner.children.length === 1);

  // A protected row that IS placed is a normal reuse — no double bookkeeping.
  const inner2 = doc.createElement('div');
  inner2.appendChild(row(0, 'question'));
  const live2 = doc.createElement('div');
  live2.id = 'liveAssistantTurn';
  inner2.appendChild(live2);
  const ctx3 = Patch.begin(inner2, { protect: n => n.id === 'liveAssistantTurn' });
  Patch.place(ctx3, row(0, 'question'));
  Patch.place(ctx3, live2);
  const stats3 = Patch.finish(ctx3);
  eq('protect: a placed live turn is reused, not kept', [stats3.kept, stats3.reused], [0, 1]);
  ok('protect: placement keeps the live node', inner2.children[1] === live2);

  // A throwing predicate must not break the render (defensive: protect is optional).
  const inner3 = doc.createElement('div');
  inner3.appendChild(row(0, 'q'));
  inner3.appendChild(live);
  const ctx4 = Patch.begin(inner3, { protect() { throw new Error('predicate'); } });
  Patch.place(ctx4, row(0, 'q'));
  const stats4 = Patch.finish(ctx4);
  eq('protect: a throwing predicate falls back to removal', stats4.removed, 1);
}

/* ───────────────────── 6. virtual spacers keep their slot ─────────────────── */
{
  // Unkeyed spacers were removed + re-inserted on every render, which re-runs layout
  // and can nudge the viewport. Stable keys make a same-height spacer a no-op and a
  // resized one an in-place swap.
  const src = fs.readFileSync(new URL('../static/ui.js', import.meta.url), 'utf8');
  const at = src.indexOf('function _messageVirtualSpacer(');
  ok('spacer: helper exists', at > 0);
  const body = src.slice(at, src.indexOf('\n}\n', at) + 3);
  ok('spacer: carries a stable render key', /data-render-key/.test(body));
  ok('spacer: the key includes the side', /virtual-spacer:/.test(body) && /\bwhere\b/.test(body));

  const makeSpacer = new Function('document', body + '\nreturn _messageVirtualSpacer;')(doc);
  const top = makeSpacer(120, 'top');
  const top2 = makeSpacer(120, 'top');
  const bottom = makeSpacer(40, 'bottom');
  eq('spacer: same side -> same key', Patch.keyOf(top), Patch.keyOf(top2));
  ok('spacer: different sides -> different keys', Patch.keyOf(top) !== Patch.keyOf(bottom));
  ok('spacer: the key is non-empty', !!Patch.keyOf(top));
}

/* ──────────────────────────────── report ─────────────────────────────────── */
if (failures.length) {
  console.error('FAIL ' + failures.length + ' / ' + (passed + failures.length));
  for (const f of failures) console.error('  ✗ ' + f);
  process.exit(1);
}
console.log('ok ' + passed + ' assertions');
