#!/usr/bin/env node
// Probe for the "chat snaps back to the bottom after the assistant reply" report.
//
// Reuses the EXISTING sandbox from scripts/scroll_harness.mjs (same DOM stub,
// same fragment extraction from static/ui.js) and adds two scenarios the
// existing harness does not cover:
//
//   D1 — scroll-up in the 150 ms "programmatic scroll" latch window that the
//        post-render path arms. Contract: the reader must still unpin, because
//        a real touch/wheel gesture is unambiguous evidence of reader intent.
//   D2 — the latch has expired but the writer still holds a pin: a scroll-up
//        (bare/momentum, no capture-phase gesture) must unpin within the
//        1400 ms post-render artifact window. This is scenario (a) of the
//        original harness, re-checked against the CURRENT ui.js.
//
// No logic is copied: every branch is loaded from static/ui.js at run time.
// Exit 0 = contract holds, 1 = defect reproduced, 2 = harness/extraction error.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const UI_JS = path.join(REPO, 'static', 'ui.js');
const TAG = '[scroll-probe]';
let extractionOk = true;
let source = '';
try { source = fs.readFileSync(UI_JS, 'utf8'); }
catch (err) { console.log(`${TAG} EXTRACTION FAIL: ${err.message}`); process.exit(2); }

function anchor(needle, label) {
  const i = source.indexOf(needle);
  if (i < 0) { console.log(`${TAG} EXTRACTION FAIL: ${label}`); extractionOk = false; return -1; }
  if (source.indexOf(needle, i + 1) >= 0) { console.log(`${TAG} EXTRACTION FAIL: not unique: ${label}`); extractionOk = false; return -1; }
  return i;
}

const aStart = anchor("if(typeof window._autoScrollFollow==='undefined')", 'fragment A start');
const aEnd = anchor('function _resetStreamScrollFollow(){', 'fragment A end');
const scrollKeys = anchor('const _MESSAGE_SCROLL_KEYS=new Set([', 'fragment B marker');
const bStart = scrollKeys >= 0 ? source.lastIndexOf('(function(){', scrollKeys) : -1;
const fmtTokens = anchor('function _fmtTokens(n){', 'fragment B end');
const bClose = fmtTokens >= 0 ? source.lastIndexOf('})();', fmtTokens) : -1;
const sbDeclMatch = source.match(/(?:let|var|const)\s+_scrollbarDragActive\s*=\s*[^;\n]*;/);
if (!sbDeclMatch) { console.log(`${TAG} EXTRACTION FAIL: _scrollbarDragActive`); extractionOk = false; }
const stampMatch = source.match(/function renderMessages\(options\)\{\s*\n\s*(_lastMessageRenderAt=performance\.now\(\);)/);
if (!stampMatch) { console.log(`${TAG} EXTRACTION FAIL: render stamp`); extractionOk = false; }
if (!extractionOk || aStart < 0 || aEnd <= aStart || bStart < 0 || bClose <= bStart) process.exit(2);

const fragmentA = source.slice(aStart, aEnd);
const fragmentB = source.slice(bStart, bClose + '})();'.length);
const sbDecl = sbDeclMatch[0];
const stampStmt = stampMatch[1];

const GEOM = { scrollHeight: 5000, clientHeight: 800, bottomTop: 4200 };

function createEnv() {
  const clock = { ms: 10000 };
  const rafQueue = new Map(); let rafId = 1;
  const timers = new Map(); let timerId = 1;
  const docListeners = {}; const elListeners = {}; const winListeners = {};
  const messagesEl = {
    id: 'messages', tagName: 'DIV',
    scrollTop: GEOM.bottomTop, scrollHeight: GEOM.scrollHeight, clientHeight: GEOM.clientHeight, clientWidth: 600,
    style: {},
    addEventListener(type, fn) { (elListeners[type] ||= []).push(fn); },
    removeEventListener() {},
    contains(node) { return node === messagesEl || !!(node && node.__insideMessages); },
    matches() { return false; },
    scrollTo() {}, querySelector() { return null; },
    setAttribute() {}, getAttribute() { return null; },
    classList: { add() {}, remove() {}, toggle() {} },
  };
  const documentStub = {
    activeElement: { tagName: 'BODY' }, visibilityState: 'visible', body: { tagName: 'BODY' },
    addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
    removeEventListener() {},
    getElementById(id) { return id === 'messages' ? messagesEl : null; },
    querySelector() { return null; },
  };
  const sandbox = {
    console,
    document: documentStub,
    performance: { now: () => clock.ms },
    requestAnimationFrame(fn) { const id = rafId++; rafQueue.set(id, fn); return id; },
    cancelAnimationFrame(id) { rafQueue.delete(id); },
    setTimeout(fn, ms) { const id = timerId++; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 0; }, clearInterval() {},
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    $: () => null,
    _scheduleMessageVirtualizedRender() {},
    _markMessageVirtualScrollActive() {},
    _isSessionEndlessScrollEnabled: () => false,
    addEventListener(type, fn) { (winListeners[type] ||= []).push(fn); },
    removeEventListener() {},
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  try {
    vm.runInContext(sbDecl, ctx, { filename: 'ui.js#_scrollbarDragActive' });
    vm.runInContext(fragmentA, ctx, { filename: 'ui.js#fragmentA' });
    vm.runInContext(fragmentB, ctx, { filename: 'ui.js#fragmentB' });
    vm.runInContext(`globalThis.__stampRender=function(){ ${stampStmt} };`, ctx, { filename: 'probe#stamp' });
  } catch (err) {
    console.log(`${TAG} EXTRACTION FAIL load: ${err && err.stack ? err.stack.split('\n')[0] : err}`);
    process.exit(2);
  }
  const readState = () => vm.runInContext(`({
    unpinned: _messageUserUnpinned, pinned: _scrollPinned,
    generation: _messageScrollInputGeneration, lastScrollTop: _lastScrollTop,
    programmaticFresh: _freshProgrammaticScrollActive(),
    renderArtifactWindow: _recentMessageRenderArtifactWindow(1400),
    wheelIntent: _recentMessageWheelIntent(), touchIntent: _recentMessageTouchScrollIntent(),
    nonMessageIntent: _recentNonMessageScrollIntent()
  })`, ctx);
  const dispatch = (listeners, type, ev) => {
    const event = Object.assign({ type, preventDefault() {}, stopPropagation() {} }, ev);
    for (const fn of (listeners[type] || [])) fn(event);
  };
  return {
    ctx, clock, messagesEl, readState,
    resetTracker: () => vm.runInContext('_resetScrollDirectionTracker()', ctx),
    stampRender: () => vm.runInContext('__stampRender()', ctx),
    armProgrammaticLatch: () => vm.runInContext('_programmaticScroll=true;_programmaticScrollSetAt=performance.now();', ctx),
    dispatchDoc: (t, e) => dispatch(docListeners, t, e),
    dispatchEl: (t, e) => dispatch(elListeners, t, e),
    flushRaf: () => { const entries = [...rafQueue.entries()]; rafQueue.clear(); for (const [, fn] of entries) fn(clock.ms); },
    insideNode: { __insideMessages: true, tagName: 'DIV' },
  };
}

function setupEnv() {
  const env = createEnv();
  env.resetTracker();
  env.messagesEl.scrollTop = GEOM.bottomTop;
  env.dispatchEl('scroll', {});
  env.flushRaf();
  return env;
}
function userScrollTo(env, newTop) {
  env.messagesEl.scrollTop = newTop;
  env.dispatchEl('scroll', {});
  env.flushRaf();
}

const results = {};
function check(name, ok, numbers, detail) {
  results[name] = { pass: !!ok, numbers, detail };
  console.log(`${TAG} ${name}: ${ok ? 'PASS' : 'FAIL'} — ${detail}`);
  console.log(`${TAG}   numbers ${JSON.stringify(numbers)}`);
  return !!ok;
}

// D1: touch scroll-up 40 ms after the final render, INSIDE the 150 ms programmatic
// latch that renderMessages()/scrollToBottom() arm. A touchmove is unambiguous
// user intent, so the reader must unpin even though the latch is fresh.
{
  const env = setupEnv();
  env.stampRender();
  env.armProgrammaticLatch();          // post-render path arms the 150 ms guard
  env.clock.ms += 40;                  // real-world: finger lands immediately
  env.dispatchDoc('touchmove', { touches: [{ clientY: 400 }], target: env.insideNode });
  userScrollTo(env, 4080);
  const s = env.readState();
  check('D1_touch_scroll_up_inside_programmatic_latch', s.unpinned === true && s.pinned === false, {
    renderAgeMs: 40, scrollTop: env.messagesEl.scrollTop, ...s,
  }, s.unpinned
    ? 'reader unpinned despite the fresh latch'
    : 'DEFECT: fresh programmatic latch swallowed the touch scroll-up — reader stays pinned');
}

// D2: bare scroll-up (momentum / keyboard with focus outside the pane) 300 ms
// after the render: latch expired, artifact window still open.
{
  const env = setupEnv();
  env.stampRender();
  env.armProgrammaticLatch();
  env.clock.ms += 300;
  userScrollTo(env, 4080);
  const s = env.readState();
  check('D2_bare_scroll_up_after_latch_expiry', s.unpinned === true && s.pinned === false, {
    renderAgeMs: 300, scrollTop: env.messagesEl.scrollTop, ...s,
  }, s.unpinned
    ? 'reader unpinned'
    : 'DEFECT: post-render artifact window swallowed a bare scroll-up');
}

// D3: wheel-up (capture phase) 250 ms after render, latch already expired.
// This is the desktop/mouse case; it must unpin and must not be re-pinned.
{
  const env = setupEnv();
  env.stampRender();
  env.clock.ms += 250;
  env.dispatchDoc('wheel', { deltaY: -40, target: env.insideNode });
  userScrollTo(env, 4080);
  const s = env.readState();
  check('D3_wheel_scroll_up_after_render', s.unpinned === true && s.pinned === false, {
    renderAgeMs: 250, scrollTop: env.messagesEl.scrollTop, ...s,
  }, s.unpinned ? 'reader unpinned' : 'DEFECT: wheel scroll-up left the reader pinned');
}

const allPass = Object.values(results).every(r => r.pass);
const verdict = allPass ? 'PASS' : 'FAIL';
console.log(`${TAG} RESULT_JSON ${JSON.stringify({ verdict, results })}`);
process.exit(allPass ? 0 : 1);
