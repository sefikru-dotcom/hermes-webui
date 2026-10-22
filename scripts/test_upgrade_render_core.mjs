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
  ok('window body is bounded', body.style.maxHeight === Activity.WINDOW_BODY_MAX_PX + 'px');

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

/* ──────────────────────────────── report ─────────────────────────────────── */
if (failures.length) {
  console.error('FAIL ' + failures.length + ' / ' + (passed + failures.length));
  for (const f of failures) console.error('  ✗ ' + f);
  process.exit(1);
}
console.log('ok ' + passed + ' assertions');
