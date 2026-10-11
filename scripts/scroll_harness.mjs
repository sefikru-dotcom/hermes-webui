#!/usr/bin/env node
// Behavioral harness for the post-render scroll-suppression defect (WebUI Mars).
//
// WHY: the existing scroll tests in this repo are STATIC (search lines in
// static/ui.js via .index() and assert their order). Such tests stay green
// while the live bug exists and break on harmless refactors. This harness is
// BEHAVIORAL: it loads the REAL scroll-handling code cut out of the
// repository's static/ui.js into a node:vm sandbox with a hand-rolled DOM
// stub, dispatches real event objects through the real listeners and asserts
// the DESIRED contract on observable state (_messageUserUnpinned,
// _scrollPinned, _messageScrollInputGeneration, ...).
//
// NO logic is copied into this file: every branch under test is extracted
// from static/ui.js at run time via unique content anchors (line numbers are
// computed and printed, never hardcoded). If an anchor stops matching, the
// harness exits 2 with VERDICT extraction: FAIL instead of silently passing.
//
// DEFECT UNDER TEST (base 53af756a, static/ui.js ~6820-6834):
//   inside the #messages 'scroll' handler, when
//     movedUp && _recentMessageRenderArtifactWindow(1400)
//             && !_recentMessageTouchScrollIntent()
//             && !_recentNonMessageScrollIntent()
//             && !_recentMessageWheelIntent()
//             && !_recentMessageKeyScrollIntent()
//   the handler executes `_lastScrollTop=top; return;` — it silently DISCARDS
//   an upward user scroll and never sets _messageUserUnpinned=true. Right
//   after a reply finishes (renderMessages() at ui.js:17378 stamps
//   _lastMessageRenderAt=performance.now() unconditionally), any upward
//   scroll whose gesture events did not reach the document capture listeners
//   (keyboard scroll with focus/hover outside the pane, momentum/chained
//   scrolls, assistive tech, middle-click autoscroll, ...) is swallowed for
//   the whole 1400 ms window: auto-follow keeps snapping the reader to the
//   bottom and only a page reload cures it.
//
// SCENARIOS (each prints numbers + "VERDICT <name>: PASS|FAIL"):
//   (a) a_post_render_user_scroll_up_not_suppressed — RED on base (bug).
//       a1: bare upward scroll event 300 ms after render completion.
//       a2: keyboard PageUp (focus on <body>, pointer not hovering the pane)
//           300 ms after render completion.
//       Desired: viewport moves up AND _messageUserUnpinned becomes true
//       (auto-follow released). On base both are swallowed -> FAIL.
//   (b) b_real_gesture_resets_suppression_window — fidelity anchor, green on
//       base. A real gesture inside the pane (gentle wheel deltaY=-8,
//       touchmove dy=5, wheel at renderAge 1390 ms) must neutralize the
//       artifact window no matter how recent the render is.
//   (c) c_generation_input_acceptance — fidelity anchor, green on base.
//       c1: render window EXPIRED (1600 ms) with unchanged input generation
//           -> the scroll must NOT be suppressed (must be processed).
//       c2: generation GREW via real input (gentle wheel) INSIDE the window
//           -> the scroll must be accepted (unpin).
//
// Exit codes: 0 = all verdicts PASS, 1 = at least one FAIL (expected on the
// buggy base for scenario a), 2 = extraction/load failure (harness broken).
//
// DOM-STUB ASSUMPTIONS (what they may distort — see report):
//   * No real layout/inertia: the harness itself moves el.scrollTop the way
//     a browser would after user input, then fires the 'scroll' event and
//     flushes the rAF queue synchronously.
//   * requestAnimationFrame callbacks are flushed manually (one frame per
//     flush); setTimeout/clearTimeout are recorded but never fired (no
//     scenario depends on deferred timers; endless-scroll prefetch is
//     disabled via _isSessionEndlessScrollEnabled stub -> false).
//   * performance.now() is a controllable clock.
//   * document.capture listeners are the extracted real ones; dispatching
//     'wheel'/'touchmove'/'keydown' on the stub document feeds the real
//     _recordNonMessageScrollIntent / key-intent handlers.
//   * el.matches(':hover') === false (pointer away from the pane),
//     document.activeElement === <body>-like node.
//   * Peripherals NOT part of the defect are stubbed no-ops:
//     _scheduleMessageVirtualizedRender, _markMessageVirtualScrollActive
//     (virtualization scheduling), $ -> null (bottom-cue button chrome),
//     _isSessionEndlessScrollEnabled -> false (prefetch). Everything that
//     participates in the suppression decision comes from ui.js verbatim.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const UI_JS = path.join(REPO, 'static', 'ui.js');
const TAG = '[scroll-harness]';

const out = [];
const log = (msg) => { out.push(msg); console.log(msg); };

let extractionOk = true;
function extractionFail(reason) {
  extractionOk = false;
  log(`${TAG} EXTRACTION FAIL: ${reason}`);
}

// ---------------------------------------------------------------------------
// 1. Read the REAL source from the repository and cut fragments by anchors.
// ---------------------------------------------------------------------------
let source = '';
try {
  source = fs.readFileSync(UI_JS, 'utf8');
} catch (err) {
  extractionFail(`cannot read ${UI_JS}: ${err.message}`);
  finish({ extraction: { ok: false }, verdicts: { extraction: 'FAIL' } });
  process.exit(2);
}
const lineNumberAt = (idx) => source.slice(0, idx).split('\n').length;

function uniqueIndex(needle, label) {
  const i = source.indexOf(needle);
  if (i < 0) { extractionFail(`anchor not found: ${label}`); return -1; }
  if (source.indexOf(needle, i + 1) >= 0) { extractionFail(`anchor not unique: ${label}`); return -1; }
  return i;
}

// Fragment A: scroll state vars + intent/artifact helpers + _recordNonMessage
// ScrollIntent + document capture listener registration + reset tracker.
// ui.js base 53af756a: lines 6244-6625.
const aStartIdx = uniqueIndex("if(typeof window._autoScrollFollow==='undefined')", 'fragment A start');
const aEndIdx = uniqueIndex('function _resetStreamScrollFollow(){', 'fragment A end (exclusive)');
const fragmentA = (aStartIdx >= 0 && aEndIdx > aStartIdx) ? source.slice(aStartIdx, aEndIdx) : '';
if (!fragmentA) extractionFail('fragment A slice is empty');

// Fragment B: the IIFE that registers pointerdown/keydown handlers and the
// #messages 'scroll' listener containing the suppression branch under test.
// ui.js base 53af756a: lines 6718-6893.
const scrollKeysIdx = uniqueIndex('const _MESSAGE_SCROLL_KEYS=new Set([', 'fragment B marker');
const bStartIdx = scrollKeysIdx >= 0 ? source.lastIndexOf('(function(){', scrollKeysIdx) : -1;
if (bStartIdx < 0) extractionFail('fragment B start not found');
const fmtTokensIdx = uniqueIndex('function _fmtTokens(n){', 'fragment B end marker');
const bCloseIdx = fmtTokensIdx >= 0 ? source.lastIndexOf('})();', fmtTokensIdx) : -1;
if (bCloseIdx < 0 || bCloseIdx < bStartIdx) extractionFail('fragment B close not found');
const fragmentB = (bStartIdx >= 0 && bCloseIdx > bStartIdx) ? source.slice(bStartIdx, bCloseIdx + '})();'.length) : '';
if (!fragmentB) extractionFail('fragment B slice is empty');

// The real declaration of _scrollbarDragActive (ui.js base: line 599) — it is
// read by the suppression condition, so it must exist in the sandbox.
const sbDeclMatch = source.match(/(?:let|var|const)\s+_scrollbarDragActive\s*=\s*[^;\n]*;/);
if (!sbDeclMatch) extractionFail('_scrollbarDragActive declaration not found');
const scrollbarDragDecl = sbDeclMatch ? sbDeclMatch[0] : '';

// The real unconditional render stamp: first statement of renderMessages()
// (ui.js base: line 17378). Executed verbatim by __stampRender() so the
// harness provably uses the repo's render side, not a copy.
const stampMatch = source.match(/function renderMessages\(options\)\{\s*\n\s*(_lastMessageRenderAt=performance\.now\(\);)/);
if (!stampMatch) extractionFail('renderMessages() render stamp not found');
const renderStampStmt = stampMatch ? stampMatch[1] : '';
const renderStampLine = stampMatch ? lineNumberAt(source.indexOf(stampMatch[1], source.indexOf('function renderMessages(options){'))) : -1;

const extractionReport = {
  ok: extractionOk,
  ui_js: path.relative(REPO, UI_JS),
  fragment_a_lines: fragmentA ? [lineNumberAt(aStartIdx), lineNumberAt(aEndIdx) - 1] : null,
  fragment_b_lines: fragmentB ? [lineNumberAt(bStartIdx), lineNumberAt(bCloseIdx + 3)] : null,
  scrollbar_drag_decl_line: scrollbarDragDecl ? lineNumberAt(source.indexOf(scrollbarDragDecl)) : null,
  render_stamp_line: renderStampLine,
  render_stamp_stmt: renderStampStmt,
};
if (extractionOk) {
  log(`${TAG} source: ${extractionReport.ui_js} (${source.split('\n').length} lines) — logic read from repo, not copied`);
  log(`${TAG} fragment A (scroll state + intent helpers + doc capture listeners): lines ${extractionReport.fragment_a_lines[0]}-${extractionReport.fragment_a_lines[1]}`);
  log(`${TAG} fragment B (pointerdown/keydown/scroll listener IIFE): lines ${extractionReport.fragment_b_lines[0]}-${extractionReport.fragment_b_lines[1]}`);
  log(`${TAG} _scrollbarDragActive decl: line ${extractionReport.scrollbar_drag_decl_line} \`${scrollbarDragDecl}\``);
  log(`${TAG} renderMessages() stamp: line ${renderStampLine} \`${renderStampStmt}\``);
} else {
  finish({ extraction: extractionReport, verdicts: { extraction: 'FAIL' } });
  process.exit(2);
}

// ---------------------------------------------------------------------------
// 2. Sandbox with hand-rolled DOM stub (no jsdom/playwright/node_modules).
// ---------------------------------------------------------------------------
const GEOM = { scrollHeight: 5000, clientHeight: 800, bottomTop: 4200 }; // bottomDistance=0 at start

function createEnv() {
  const clock = { ms: 10000 };
  const calls = { virtualRender: 0, virtualScrollActive: 0 };
  const rafQueue = new Map(); let rafId = 1;
  const timers = new Map(); let timerId = 1;
  const docListeners = {}; const elListeners = {}; const winListeners = {};

  const messagesEl = {
    id: 'messages',
    tagName: 'DIV',
    scrollTop: GEOM.bottomTop,
    scrollHeight: GEOM.scrollHeight,
    clientHeight: GEOM.clientHeight,
    clientWidth: 600,
    style: {},
    addEventListener(type, fn) { (elListeners[type] ||= []).push(fn); },
    removeEventListener() {},
    contains(node) { return node === messagesEl || !!(node && node.__insideMessages); },
    matches() { return false; }, // assumption: pointer not hovering the pane
    scrollTo() {},
    querySelector() { return null; },
    setAttribute() {}, getAttribute() { return null; },
    classList: { add() {}, remove() {}, toggle() {} },
  };

  const documentStub = {
    activeElement: { tagName: 'BODY' },
    visibilityState: 'visible',
    body: { tagName: 'BODY' },
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
    setInterval() { return 0; },
    clearInterval() {},
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    // Peripheral, NOT part of the suppression decision (documented stubs):
    $: () => null,
    _scheduleMessageVirtualizedRender() { calls.virtualRender++; },
    _markMessageVirtualScrollActive() { calls.virtualScrollActive++; },
    _isSessionEndlessScrollEnabled: () => false,
    addEventListener(type, fn) { (winListeners[type] ||= []).push(fn); },
    removeEventListener() {},
  };
  sandbox.window = sandbox;

  const ctx = vm.createContext(sandbox);
  try {
    vm.runInContext(scrollbarDragDecl, ctx, { filename: 'ui.js#_scrollbarDragActive' });
    vm.runInContext(fragmentA, ctx, { filename: 'ui.js#fragmentA' });
    vm.runInContext(fragmentB, ctx, { filename: 'ui.js#fragmentB' });
    vm.runInContext(`globalThis.__stampRender=function(){ ${renderStampStmt} };`, ctx, { filename: 'harness#renderStamp' });
  } catch (err) {
    extractionFail(`fragment load error: ${err && err.stack ? err.stack.split('\n').slice(0, 3).join(' | ') : err}`);
    finish({ extraction: extractionReport, verdicts: { extraction: 'FAIL' } });
    process.exit(2);
  }

  const readState = () => vm.runInContext(`({
    unpinned: _messageUserUnpinned,
    pinned: _scrollPinned,
    lastScrollTop: _lastScrollTop,
    generation: _messageScrollInputGeneration,
    renderArtifactWindow: _recentMessageRenderArtifactWindow(1400),
    touchIntent: _recentMessageTouchScrollIntent(),
    wheelIntent: _recentMessageWheelIntent(),
    keyIntent: _recentMessageKeyScrollIntent(),
    nonMessageIntent: _recentNonMessageScrollIntent(),
    programmaticFresh: _freshProgrammaticScrollActive(),
    touchActive: _messageTouchScrollActive,
    scrollbarDrag: _scrollbarDragActive
  })`, ctx);

  const dispatch = (listeners, type, ev) => {
    const event = Object.assign({ type, preventDefault() {}, stopPropagation() {} }, ev);
    for (const fn of (listeners[type] || [])) fn(event);
  };

  return {
    ctx, clock, calls, messagesEl, readState,
    resetTracker: () => vm.runInContext('_resetScrollDirectionTracker()', ctx),
    stampRender: () => vm.runInContext('__stampRender()', ctx),
    dispatchDoc: (type, ev) => dispatch(docListeners, type, ev),
    dispatchEl: (type, ev) => dispatch(elListeners, type, ev),
    dispatchWin: (type, ev) => dispatch(winListeners, type, ev),
    flushRaf: () => { const entries = [...rafQueue.entries()]; rafQueue.clear(); for (const [, fn] of entries) fn(clock.ms); },
    insideNode: { __insideMessages: true, tagName: 'DIV' },
  };
}

// Standard preamble: fresh env, session-switch reset, one baseline scroll
// frame at the pinned bottom (like the follow-scroll after the last token).
function setupEnv() {
  const env = createEnv();
  env.resetTracker();
  env.messagesEl.scrollTop = GEOM.bottomTop;
  env.dispatchEl('scroll', {});
  env.flushRaf();
  return env;
}

// Simulate the browser moving the viewport on user scroll, then deliver the
// native 'scroll' event and run the handler's rAF frame.
function userScrollTo(env, newTop) {
  env.messagesEl.scrollTop = newTop;
  env.dispatchEl('scroll', {});
  env.flushRaf();
}

const fmt = (o) => JSON.stringify(o);
const results = {};

function check(name, ok, numbers, detail) {
  results[name] = { pass: !!ok, numbers, detail };
  log(`${TAG} ${name} numbers: ${fmt(numbers)}`);
  log(`${TAG} ${name}: ${ok ? 'PASS' : 'FAIL'} — ${detail}`);
  return !!ok;
}

// ---------------------------------------------------------------------------
// Scenario (a): user scroll-up right after render completion must NOT be
// suppressed. Desired: _messageUserUnpinned=true, _scrollPinned=false.
// On base 53af756a the artifact branch swallows both -> FAIL (RED = bug).
// ---------------------------------------------------------------------------
{
  // a1: bare upward scroll event 300 ms after the final render — models any
  // real scroll whose gesture events never reached the document capture
  // listeners (momentum/chained scroll, assistive tech, middle-click
  // autoscroll). The handler cannot tell it from a render artifact and must
  // still not strand the reader: contract = unpin.
  const env = setupEnv();
  const genBefore = env.readState().generation;
  env.stampRender();                       // renderMessages() first statement (ui.js:17378)
  env.clock.ms += 300;                     // inside the 1400 ms artifact window, past the 150 ms programmatic guard
  userScrollTo(env, 4080);                 // user scrolled up 120 px
  const s = env.readState();
  const ok = s.unpinned === true && s.pinned === false;
  check('a1_bare_scroll_up_300ms_after_render', ok, {
    renderAgeMs: 300, scrollTop: env.messagesEl.scrollTop, generation: s.generation, genBefore,
    unpinned: s.unpinned, pinned: s.pinned, lastScrollTop: s.lastScrollTop,
    renderArtifactWindow: s.renderArtifactWindow, programmaticFresh: s.programmaticFresh,
  }, ok
    ? 'upward scroll accepted: reader unpinned from auto-follow'
    : 'DEFECT: upward scroll silently discarded (_lastScrollTop=top; return) — reader stays pinned under auto-follow');

  // a2: keyboard PageUp with focus on <body> and pointer NOT hovering the
  // pane. The real keydown handler (ui.js ~6757-6778) stamps key intent only
  // when a===el || el.contains(a) || el.matches(':hover'); none holds here,
  // so the native scroll event arrives with no intent recorded and the base
  // suppression swallows a genuine keyboard scroll-up.
  const env2 = setupEnv();
  env2.stampRender();
  env2.clock.ms += 300;
  env2.dispatchDoc('keydown', { key: 'PageUp', target: { tagName: 'BODY' } });
  userScrollTo(env2, 3400);                // PageUp moved the viewport 800 px up
  const s2 = env2.readState();
  const ok2 = s2.unpinned === true && s2.pinned === false;
  check('a2_keyboard_pageup_300ms_after_render', ok2, {
    renderAgeMs: 300, scrollTop: env2.messagesEl.scrollTop, generation: s2.generation,
    unpinned: s2.unpinned, pinned: s2.pinned, lastScrollTop: s2.lastScrollTop,
    keyIntent: s2.keyIntent, renderArtifactWindow: s2.renderArtifactWindow,
  }, ok2
    ? 'keyboard scroll-up accepted: reader unpinned'
    : 'DEFECT: keyboard PageUp (focus outside pane, no hover) swallowed by the artifact window');
}
const verdictA = results.a1_bare_scroll_up_300ms_after_render.pass && results.a2_keyboard_pageup_300ms_after_render.pass;

// ---------------------------------------------------------------------------
// Scenario (b): a real gesture in the pane must neutralize the suppression
// window regardless of how recent the render is. Fidelity anchor: PASS on
// base (the intent-flag mitigation works); proves the harness can also see
// accepted scrolls, so (a) is not red because everything is red.
// ---------------------------------------------------------------------------
{
  // b1: gentle wheel-up (deltaY=-8, below the -30 sticky-unpin threshold)
  // 100 ms after render.
  const env = setupEnv();
  const g0 = env.readState().generation;
  env.stampRender();
  env.clock.ms += 100;
  env.dispatchDoc('wheel', { target: env.messagesEl, deltaY: -8 });
  const g1 = env.readState().generation;
  userScrollTo(env, 4150);
  const s = env.readState();
  check('b1_gentle_wheel_up_100ms_after_render', s.unpinned === true && g1 > g0, {
    renderAgeMs: 105, deltaY: -8, generationBefore: g0, generationAfterGesture: g1,
    scrollTop: env.messagesEl.scrollTop, unpinned: s.unpinned, pinned: s.pinned, wheelIntent: s.wheelIntent,
  }, (s.unpinned && g1 > g0) ? 'gesture grew generation and scroll-up was accepted' : 'gesture path broken in harness');

  // b2: touchmove inside the pane (dy=5 — too small to unpin by itself: the
  // direct-unpin branch needs dy>8 with a recorded touchstart), 50 ms after
  // render. The unpin must come from the accepted scroll-up, proving the
  // gesture neutralized the artifact window.
  const env2 = setupEnv();
  env2.stampRender();
  env2.clock.ms += 50;
  env2.dispatchDoc('touchmove', { target: env2.insideNode, touches: [{ clientY: 105 }] });
  userScrollTo(env2, 4100);
  const s2 = env2.readState();
  check('b2_touchmove_50ms_after_render', s2.unpinned === true && s2.touchActive === true, {
    renderAgeMs: 55, dyPx: 5, scrollTop: env2.messagesEl.scrollTop,
    unpinned: s2.unpinned, pinned: s2.pinned, touchActive: s2.touchActive, touchIntent: s2.touchIntent,
  }, (s2.unpinned && s2.touchActive) ? 'touch gesture neutralized the window; scroll-up accepted' : 'touch path broken in harness');

  // b3: wheel-up at renderAge 1390 ms — window nearly expired; the gesture
  // must still win "regardless of how long ago the render was".
  const env3 = setupEnv();
  env3.stampRender();
  env3.clock.ms += 1390;
  env3.dispatchDoc('wheel', { target: env3.messagesEl, deltaY: -12 });
  env3.clock.ms += 5;                      // renderAge 1395 ms < 1400 ms
  userScrollTo(env3, 4140);
  const s3 = env3.readState();
  check('b3_wheel_up_at_render_age_1390ms', s3.unpinned === true, {
    renderAgeMs: 1395, deltaY: -12, scrollTop: env3.messagesEl.scrollTop,
    unpinned: s3.unpinned, pinned: s3.pinned, renderArtifactWindow: s3.renderArtifactWindow,
  }, s3.unpinned ? 'gesture at window edge accepted' : 'window-edge gesture path broken in harness');
}
const verdictB = results.b1_gentle_wheel_up_100ms_after_render.pass
  && results.b2_touchmove_50ms_after_render.pass
  && results.b3_wheel_up_at_render_age_1390ms.pass;

// ---------------------------------------------------------------------------
// Scenario (c): suppression must be a property of the artifact window and of
// input generation, not a coin flip:
//   c1: window EXPIRED (renderAge 1600 ms) + generation unchanged (pure
//       artifact shape) -> must NOT be suppressed; the event is processed.
//   c2: generation GREW via real input (gentle wheel) INSIDE the window ->
//       must be accepted (unpin), i.e. real input always beats suppression.
// Fidelity anchor: PASS on base; documents the generation numbers a
// generation-based fix should keep honoring.
// ---------------------------------------------------------------------------
{
  // c1
  const env = setupEnv();
  env.stampRender();
  const genAtRender = env.readState().generation;
  env.clock.ms += 1600;                    // outside the 1400 ms window
  userScrollTo(env, 4000);
  const s = env.readState();
  check('c1_expired_window_generation_unchanged', s.unpinned === true && s.generation === genAtRender, {
    renderAgeMs: 1600, generationAtRender: genAtRender, generationAfter: s.generation,
    renderArtifactWindow: s.renderArtifactWindow, scrollTop: env.messagesEl.scrollTop,
    unpinned: s.unpinned, pinned: s.pinned,
  }, (s.unpinned && s.generation === genAtRender)
    ? 'outside the window the scroll-up is processed even with unchanged generation'
    : 'scroll-up suppressed outside the artifact window');

  // c2
  const env2 = setupEnv();
  env2.stampRender();
  const g0 = env2.readState().generation;
  env2.clock.ms += 200;
  env2.dispatchDoc('wheel', { target: env2.messagesEl, deltaY: -5 });
  const g1 = env2.readState().generation;
  userScrollTo(env2, 4170);
  const s2 = env2.readState();
  check('c2_generation_grew_real_input_inside_window', s2.unpinned === true && g1 === g0 + 1, {
    renderAgeMs: 205, generationBefore: g0, generationAfterInput: g1,
    renderArtifactWindow: s2.renderArtifactWindow, scrollTop: env2.messagesEl.scrollTop,
    unpinned: s2.unpinned, pinned: s2.pinned,
  }, (s2.unpinned && g1 === g0 + 1)
    ? 'real input grew generation and was accepted inside the window'
    : 'real input inside the window was not accepted');
}
const verdictC = results.c1_expired_window_generation_unchanged.pass
  && results.c2_generation_grew_real_input_inside_window.pass;

// ---------------------------------------------------------------------------
// Verdicts + machine-readable result
// ---------------------------------------------------------------------------
const verdicts = {
  extraction: extractionOk ? 'PASS' : 'FAIL',
  a_post_render_user_scroll_up_not_suppressed: verdictA ? 'PASS' : 'FAIL',
  b_real_gesture_resets_suppression_window: verdictB ? 'PASS' : 'FAIL',
  c_generation_input_acceptance: verdictC ? 'PASS' : 'FAIL',
};
for (const [name, v] of Object.entries(verdicts)) {
  if (name === 'extraction') continue;
  log(`${TAG} VERDICT ${name}: ${v}`);
}
log(`${TAG} NOTE (a) on buggy base 53af756a: expected FAIL — the swallow branch (ui.js ~6820-6834) discards the user scroll-up; FAIL here = defect reproduced behaviorally.`);

function finish(payload) {
  log(`${TAG} RESULT_JSON ${JSON.stringify(payload)}`);
}
finish({ extraction: extractionReport, checks: results, verdicts });

const allPass = verdictA && verdictB && verdictC && extractionOk;
process.exit(allPass ? 0 : 1);
