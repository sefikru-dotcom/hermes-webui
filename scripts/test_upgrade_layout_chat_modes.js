/* Behavioural harness for the boot-side chat mode class projection.
 *
 * Run: node scripts/test_upgrade_layout_chat_modes.js
 *
 * The functions under test are extracted from the real static/boot.js source
 * (not re-implemented) and evaluated against a fake <html>. ui.js's
 * _applyThinkingModeClasses / thinkingMode / chatActivityMode are extracted the
 * same way, so the parity checks compare the two real implementations instead of
 * a copy of one of them.
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const BOOT_SRC = fs.readFileSync(path.join(ROOT, 'static', 'boot.js'), 'utf8');
const UI_SRC = fs.readFileSync(path.join(ROOT, 'static', 'ui.js'), 'utf8');

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log('  ok  ' + name);
  } catch (err) {
    console.error('  FAIL ' + name);
    throw err;
  }
}

/* ── source extraction ─────────────────────────────────────────────────── */

function skipString(src, i) {
  const quote = src[i];
  i++;
  while (i < src.length) {
    if (src[i] === '\\') { i += 2; continue; }
    if (src[i] === quote) return i;
    i++;
  }
  throw new Error('unterminated string');
}

function extractFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('missing function ' + name);
  let depth = 0;
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    const ch = src[i];
    if (ch === '"' || ch === "'" || ch === '`') { i = skipString(src, i); continue; }
    if (ch === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); if (i < 0) break; continue; }
    if (ch === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 1; continue; }
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error('unterminated function ' + name);
}

function fakeClassList() {
  const set = new Set();
  const calls = [];
  return {
    set,
    calls,
    add(name) { set.add(name); calls.push(['add', name]); },
    remove(name) { set.delete(name); calls.push(['remove', name]); },
    contains(name) { return set.has(name); },
    toggle(name, force) {
      const on = force === undefined ? !set.has(name) : !!force;
      calls.push(['toggle', name, on]);
      if (on) set.add(name); else set.delete(name);
      return on;
    }
  };
}

function bootSandbox() {
  const win = {};
  const doc = { documentElement: { classList: fakeClassList() } };
  const context = vm.createContext({ window: win, document: doc, console });
  const names = [
    '_marsChatActivityModes', '_resolveChatActivityMode', '_resolveChatThinkingMode',
    '_effectiveChatThinkingMode', '_chatUxClassState', '_chatUxValuesFromMirrors',
    '_writeChatUxMirrors', '_applyChatUxClasses', '_chatUxStoreKeys',
    '_chatUxAppearanceStore', '_chatUxStoreValue', '_chatUxValuesFromStore',
    '_applyChatUxSettings', '_bindChatUxAppearanceStore', '_applyBootChatUxSettings'
  ];
  vm.runInContext(names.map((n) => extractFunction(BOOT_SRC, n)).join('\n'), context);
  return { win, doc, context };
}

function uiSandbox() {
  const win = {};
  const doc = { documentElement: { classList: fakeClassList() } };
  const context = vm.createContext({ window: win, document: doc, console });
  const names = ['thinkingMode', 'chatActivityMode', '_applyThinkingModeClasses'];
  vm.runInContext(names.map((n) => extractFunction(UI_SRC, n)).join('\n'), context);
  // `_applyThinkingModeClasses()` also calls `_reconcileActivityMode()` (ui.js, render
  // zone) which walks the live transcript and applies the mode to already-rendered
  // activity blocks. That is DOM reconciliation, not class projection: it needs
  // `liveAssistantTurn` / `msgInner` / `S` and is covered end-to-end by the browser
  // scenarios (`modes_settled_cycle`). Stubbed here so the parity test keeps comparing
  // the two real class writers instead of failing on a helper that is out of scope.
  context._reconcileActivityMode = function () {};
  win._reconcileActivityMode = context._reconcileActivityMode;
  return { win, doc, context };
}

const boot = bootSandbox();
const B = boot.context;
const classSet = () => Array.from(boot.doc.documentElement.classList.set).sort();

function resetBoot() {
  boot.win = {};
  Object.keys(boot.win).forEach((k) => delete boot.win[k]);
  // vm context kept a reference to the original window object; reuse it.
  const liveWin = B.window;
  Object.keys(liveWin).forEach((k) => delete liveWin[k]);
  boot.doc.documentElement.classList.set.clear();
  boot.doc.documentElement.classList.calls.length = 0;
  return liveWin;
}

/* ── mode resolution ───────────────────────────────────────────────────── */

test('legacy activity aliases resolve like ui.js', () => {
  const resolve = B._resolveChatActivityMode;
  assert.strictEqual(resolve('stream_full'), 'stream_full');
  assert.strictEqual(resolve('transparent_stream'), 'stream_full');
  assert.strictEqual(resolve('hide_all_activity'), 'stream_quiet');
  assert.strictEqual(resolve('compact_worklog'), 'stream_window');
  assert.strictEqual(resolve('nonsense'), 'stream_window');
  assert.strictEqual(resolve(undefined), 'stream_window');
  assert.strictEqual(resolve('  stream_quiet  '), 'stream_quiet');
});

test('thinking setting resolves to auto/expanded/collapsed/hidden', () => {
  const resolve = B._resolveChatThinkingMode;
  ['auto', 'expanded', 'collapsed', 'hidden'].forEach((v) => assert.strictEqual(resolve(v), v));
  assert.strictEqual(resolve('nope'), 'auto');
  assert.strictEqual(resolve(null), 'auto');
});

/* ── class projection ──────────────────────────────────────────────────── */

test('stream_quiet applies quiet classes before Settings is ever opened', () => {
  resetBoot();
  const state = B._applyChatUxSettings({
    activityMode: 'stream_quiet',
    thinkingMode: 'hidden',
    showToolCalls: false,
    autoCollapsePreviousTurns: true
  });
  const cls = classSet();
  assert.deepStrictEqual(cls, [
    'mars-autocollapse-turns', 'mars-hide-activity', 'mars-hide-tools',
    'mars-mode-quiet', 'mars-thinking-hidden'
  ]);
  assert.strictEqual(state['mars-mode-quiet'], true);
  assert.strictEqual(B.window._chatActivityDisplayMode, 'stream_quiet');
  assert.strictEqual(B.window._thinkingMode, 'hidden');
  assert.strictEqual(B.window._showThinking, false);
});

test('exactly one mode class is ever present', () => {
  resetBoot();
  ['stream_full', 'stream_window', 'stream_quiet'].forEach((mode) => {
    B._applyChatUxSettings({ activityMode: mode, thinkingMode: 'expanded' });
    const modes = classSet().filter((c) => c.startsWith('mars-mode-'));
    assert.deepStrictEqual(modes, ['mars-mode-' + mode.replace('stream_', '')], mode);
  });
});

test('stream_full keeps transparent-stream; window keeps neither hide class', () => {
  resetBoot();
  B._applyChatUxSettings({ activityMode: 'stream_full', thinkingMode: 'expanded' });
  assert(classSet().includes('mars-transparent-stream'));
  assert(!classSet().includes('mars-hide-activity'));
  resetBoot();
  B._applyChatUxSettings({ activityMode: 'stream_window', thinkingMode: 'expanded' });
  assert(!classSet().includes('mars-transparent-stream'));
  assert(!classSet().includes('mars-hide-activity'));
  assert(classSet().includes('mars-mode-window'));
});

test('empty mirrors project the documented defaults', () => {
  const win = resetBoot();
  const state = B._applyChatUxClasses(B._chatUxValuesFromMirrors());
  assert.deepStrictEqual(classSet(), [
    'mars-autocollapse-turns', 'mars-mode-window', 'mars-thinking-expanded'
  ]);
  assert.strictEqual(win._chatActivityDisplayMode, undefined, 'projection does not write mirrors');
  assert.strictEqual(state['mars-hide-tools'], false);
});

test('show_thinking=false with auto mode hides reasoning', () => {
  const win = resetBoot();
  win._showThinking = false;
  B._applyChatUxSettings({ activityMode: 'stream_window', thinkingMode: 'auto' });
  assert(classSet().includes('mars-thinking-hidden'));
  assert(!classSet().includes('mars-thinking-expanded'));
});

/* ── ui.js ownership ───────────────────────────────────────────────────── */

test('boot delegates to ui.js _applyThinkingModeClasses when present', () => {
  const win = resetBoot();
  let calls = 0;
  win._applyThinkingModeClasses = function () { calls++; };
  B._applyChatUxSettings({ activityMode: 'stream_full', thinkingMode: 'collapsed' });
  assert.strictEqual(calls, 1);
  assert.deepStrictEqual(classSet(), [], 'no local toggling when ui.js applied it');
  assert.strictEqual(win._thinkingMode, 'collapsed', 'mirrors are written before delegating');
});

/* ── AppearanceStore (optional API) ────────────────────────────────────── */

test('absent AppearanceStore leaves server settings authoritative', () => {
  resetBoot();
  assert.strictEqual(B._chatUxAppearanceStore(), null);
  B._applyChatUxSettings({ activityMode: 'stream_full', thinkingMode: 'expanded' });
  assert(classSet().includes('mars-mode-full'));
});

test('AppearanceStore values win over the boot snapshot', () => {
  const win = resetBoot();
  win.MarsAppearanceStore = {
    get(key) {
      return {
        chat_activity_display_mode: 'stream_quiet',
        thinking_mode: 'collapsed',
        show_tool_calls: false
      }[key];
    }
  };
  B._applyChatUxSettings({ activityMode: 'stream_full', thinkingMode: 'expanded', showToolCalls: true });
  assert(classSet().includes('mars-mode-quiet'));
  assert(classSet().includes('mars-thinking-collapsed'));
  assert(classSet().includes('mars-hide-tools'));
  assert.strictEqual(win._chatActivityDisplayMode, 'stream_quiet');
});

test('store may return {value} envelopes and string booleans', () => {
  const win = resetBoot();
  win.AppearanceStore = {
    get(key) {
      return {
        chat_activity_display_mode: { value: 'stream_full' },
        show_tool_calls: 'false'
      }[key];
    }
  };
  B._applyChatUxSettings({ activityMode: 'stream_window', thinkingMode: 'expanded' });
  assert(classSet().includes('mars-mode-full'));
  assert(classSet().includes('mars-hide-tools'));
});

test('store subscription is bound once and re-projects on change', () => {
  const win = resetBoot();
  let value = 'stream_full';
  let subscriber = null;
  let subscribes = 0;
  win.MarsAppearanceStore = {
    get(key) { return key === 'chat_activity_display_mode' ? value : undefined; },
    subscribe(fn) { subscribes++; subscriber = fn; return function () {}; },
    ready: { then(fn) { fn(); } }
  };
  assert.strictEqual(B._bindChatUxAppearanceStore(), true);
  assert.strictEqual(B._bindChatUxAppearanceStore(), false, 'second bind is a no-op');
  assert.strictEqual(subscribes, 1);
  assert(classSet().includes('mars-mode-full'));
  value = 'stream_quiet';
  subscriber();
  assert(classSet().includes('mars-mode-quiet'));
  assert(!classSet().includes('mars-mode-full'));
});

/* ── parity with ui.js (the rendering-side owner) ──────────────────────── */

test('boot projection matches ui.js for every mode combination', () => {
  const ui = uiSandbox();
  const activityModes = ['stream_full', 'stream_window', 'stream_quiet'];
  const thinkingModes = ['auto', 'expanded', 'collapsed', 'hidden'];
  const toolStates = [true, false];
  activityModes.forEach((activityMode) => {
    thinkingModes.forEach((thinkingMode) => {
      toolStates.forEach((showToolCalls) => {
        const win = resetBoot();
        win._chatActivityDisplayMode = activityMode;
        win._thinkingModeSetting = thinkingMode;
        win._showThinking = true;
        win._showToolCalls = showToolCalls;
        win._autoCollapsePreviousTurns = true;
        const bootSet = Object.keys(B._chatUxClassState(B._chatUxValuesFromMirrors()))
          .filter((k) => B._chatUxClassState(B._chatUxValuesFromMirrors())[k]).sort();

        Object.assign(ui.win, {
          _chatActivityDisplayMode: activityMode,
          _thinkingModeSetting: thinkingMode,
          // boot writes both mirrors; ui.js reads the effective one.
          _thinkingMode: thinkingMode === 'auto' ? 'expanded' : thinkingMode,
          _showThinking: true,
          _showToolCalls: showToolCalls,
          _autoCollapsePreviousTurns: true
        });
        ui.doc.documentElement.classList.set.clear();
        ui.context._applyThinkingModeClasses();
        const uiSet = Array.from(ui.doc.documentElement.classList.set).sort();

        assert.deepStrictEqual(bootSet, uiSet, activityMode + '/' + thinkingMode + '/tools=' + showToolCalls);
      });
    });
  });
});

console.log('\nPASS ' + passed + ' checks');
