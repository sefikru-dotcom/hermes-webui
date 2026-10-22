/* Behavioural harness for static/viewport_controller.js (no browser, no jsdom).
 *
 * Run: node scripts/test_upgrade_layout_viewport.js
 * Exercises the pure geometry rules and the real controller class against fake
 * window/document objects, including listener ownership, rAF coalescing and the
 * CSS variables boot.js and style.css consume.
 */
'use strict';

const assert = require('assert');
const path = require('path');

const mod = require(path.join(__dirname, '..', 'static', 'viewport_controller.js'));
const { ViewportController, computeViewportState, KEYBOARD_MIN_INSET } = mod;

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

/* ── fakes ─────────────────────────────────────────────────────────────── */

function fakeViewport(height, width, offsetTop, scale) {
  const listeners = Object.create(null);
  return {
    height,
    width,
    offsetTop: offsetTop || 0,
    scale: scale === undefined ? 1 : scale,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter((x) => x !== fn);
    },
    emit(type) { (listeners[type] || []).slice().forEach((fn) => fn()); },
    count(type) { return (listeners[type] || []).length; }
  };
}

function fakeWindow(opts) {
  const o = opts || {};
  const listeners = Object.create(null);
  const win = {
    innerHeight: o.innerHeight || 800,
    innerWidth: o.innerWidth || 390,
    visualViewport: o.withVisualViewport === false ? null : fakeViewport(o.vvHeight || 800, o.vvWidth || 390, o.vvOffsetTop, o.vvScale),
    __frames: [],
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((x) => x !== fn); },
    emit(type) { (listeners[type] || []).slice().forEach((fn) => fn()); },
    count(type) { return (listeners[type] || []).length; },
    requestAnimationFrame(fn) { win.__frames.push(fn); return win.__frames.length; },
    cancelAnimationFrame(id) { win.__frames[id - 1] = null; },
    runFrames() {
      const queued = win.__frames.filter(Boolean);
      win.__frames = [];
      queued.forEach((fn) => fn());
      return queued.length;
    }
  };
  return win;
}

function fakeDocument(safeAreaBottom) {
  const vars = Object.create(null);
  const children = [];
  const probe = {
    style: {},
    parentNode: { removeChild(el) { children.splice(children.indexOf(el), 1); } },
    setAttribute() {},
    getBoundingClientRect() { return { height: safeAreaBottom === undefined ? 0 : safeAreaBottom }; }
  };
  return {
    documentElement: {
      style: {
        setProperty(name, value) { vars[name] = String(value); },
        removeProperty(name) { delete vars[name]; },
        getPropertyValue(name) { return vars[name] || ''; }
      },
      dataset: {}
    },
    body: { appendChild(el) { children.push(el); } },
    createElement() { return probe; },
    vars,
    children
  };
}

function makeController(opts) {
  const win = fakeWindow(opts);
  const doc = fakeDocument(opts && opts.safeAreaBottom);
  const controller = new ViewportController({ window: win, document: doc });
  return { win, doc, controller };
}

/* ── pure geometry ─────────────────────────────────────────────────────── */

test('iOS keyboard: innerHeight stays, visualViewport shrinks -> inset', () => {
  const state = computeViewportState({ innerHeight: 800, innerWidth: 390, vvHeight: 500, vvWidth: 390, vvOffsetTop: 0, vvScale: 1 });
  assert.strictEqual(state.keyboardInset, 300);
  assert.strictEqual(state.keyboardOpen, true);
  assert.strictEqual(state.height, 500);
  assert.strictEqual(state.hasVisualViewport, true);
});

test('keyboard inset accounts for a panned visual viewport', () => {
  const state = computeViewportState({ innerHeight: 800, vvHeight: 480, vvOffsetTop: 20, vvScale: 1 });
  assert.strictEqual(state.keyboardInset, 300);
  assert.strictEqual(state.offsetTop, 20);
});

test('URL-bar sized shrink is chrome, not a keyboard', () => {
  const state = computeViewportState({ innerHeight: 800, vvHeight: 760, vvOffsetTop: 0, vvScale: 1 });
  assert.strictEqual(state.keyboardInset, 0);
  assert.strictEqual(state.keyboardOpen, false);
});

test('pinch zoom never reads as a keyboard', () => {
  const state = computeViewportState({ innerHeight: 800, vvHeight: 400, vvOffsetTop: 0, vvScale: 2 });
  assert.strictEqual(state.keyboardInset, 0);
  assert.strictEqual(state.pinchZoomed, true);
});

test('no visualViewport: geometry falls back to innerHeight', () => {
  const state = computeViewportState({ innerHeight: 720, innerWidth: 360 });
  assert.strictEqual(state.height, 720);
  assert.strictEqual(state.keyboardInset, 0);
  assert.strictEqual(state.hasVisualViewport, false);
});

test('threshold is configurable for tests/hosts', () => {
  const state = computeViewportState({ innerHeight: 800, vvHeight: 730, keyboardMinInset: 40 });
  assert.strictEqual(state.keyboardInset, 70);
  assert.strictEqual(KEYBOARD_MIN_INSET, 80);
});

/* ── controller: CSS contract ──────────────────────────────────────────── */

test('start() writes the geometry variables style.css consumes', () => {
  const { win, doc, controller } = makeController({ innerHeight: 800, vvHeight: 500, safeAreaBottom: 34 });
  controller.start();
  assert.strictEqual(doc.vars['--app-viewport-height'], '500px');
  assert.strictEqual(doc.vars['--app-vh'], '5px');
  assert.strictEqual(doc.vars['--keyboard-bottom-inset'], '300px');
  assert.strictEqual(doc.vars['--safe-area-bottom'], '34px');
  assert.strictEqual(doc.documentElement.dataset.keyboardOpen, '1');
  assert.strictEqual(doc.documentElement.dataset.viewportHeight, '500');
  assert.strictEqual(win.visualViewport.count('resize'), 1);
});

test('closing the keyboard removes the inset variable', () => {
  const { win, doc, controller } = makeController({ innerHeight: 800, vvHeight: 500 });
  controller.start();
  assert.strictEqual(doc.vars['--keyboard-bottom-inset'], '300px');
  win.visualViewport.height = 800;
  win.visualViewport.emit('resize');
  assert.strictEqual(win.runFrames(), 1);
  assert.strictEqual(doc.vars['--keyboard-bottom-inset'], undefined);
  assert.strictEqual(doc.documentElement.dataset.keyboardOpen, '0');
});

test('keyboard is detected on Android where innerHeight shrinks too', () => {
  const { win, doc, controller } = makeController({ innerHeight: 800, vvHeight: 800 });
  controller.start();
  assert.strictEqual(doc.documentElement.dataset.keyboardOpen, '0');
  // Android Chrome: both innerHeight and vv shrink; no occlusion gap remains.
  win.innerHeight = 500;
  win.visualViewport.height = 500;
  win.emit('resize');
  win.runFrames();
  assert.strictEqual(doc.vars['--keyboard-bottom-inset'], undefined, 'layout already shrank: no padding needed');
  assert.strictEqual(doc.documentElement.dataset.keyboardOpen, '1');
  assert.strictEqual(doc.vars['--app-viewport-height'], '500px');
});

test('rotation resets the keyboard baseline', () => {
  const { win, doc, controller } = makeController({ innerHeight: 800, vvHeight: 800 });
  controller.start();
  win.innerWidth = 800;
  win.visualViewport.width = 800;
  win.innerHeight = 360;
  win.visualViewport.height = 360;
  win.emit('orientationchange');
  win.runFrames();
  assert.strictEqual(doc.documentElement.dataset.keyboardOpen, '0');
});

/* ── controller: listener ownership ────────────────────────────────────── */

test('listeners are registered once, removed on stop', () => {
  const { win, controller } = makeController();
  controller.start();
  controller.start();
  assert.strictEqual(win.visualViewport.count('resize'), 1);
  assert.strictEqual(win.visualViewport.count('scroll'), 1);
  assert.strictEqual(win.count('resize'), 1);
  assert.strictEqual(win.count('orientationchange'), 1);
  assert.strictEqual(win.count('pageshow'), 1);
  controller.stop();
  assert.strictEqual(win.visualViewport.count('resize'), 0);
  assert.strictEqual(win.visualViewport.count('scroll'), 0);
  assert.strictEqual(win.count('resize'), 0);
});

test('without visualViewport the window resize listener still drives geometry', () => {
  const { win, doc, controller } = makeController({ withVisualViewport: false, innerHeight: 700 });
  controller.start();
  assert.strictEqual(doc.vars['--app-viewport-height'], '700px');
  win.innerHeight = 420;
  win.emit('resize');
  win.runFrames();
  assert.strictEqual(doc.vars['--app-viewport-height'], '420px');
});

test('refresh coalesces to one frame and applies the latest geometry', () => {
  const { win, doc, controller } = makeController({ innerHeight: 800, vvHeight: 800 });
  controller.start();
  win.__frames = [];
  controller.refresh();
  controller.refresh();
  controller.refresh();
  assert.strictEqual(win.__frames.filter(Boolean).length, 1, 'one rAF per burst');
  win.visualViewport.height = 450;
  assert.strictEqual(win.runFrames(), 1);
  assert.strictEqual(doc.vars['--app-viewport-height'], '450px');
});

test('subscribers fire once per distinct state, not per event', () => {
  const { win, controller } = makeController({ innerHeight: 800, vvHeight: 800 });
  controller.start();
  const seen = [];
  const off = controller.onChange((state) => seen.push(state.height));
  win.visualViewport.emit('resize');
  win.runFrames();
  win.visualViewport.emit('scroll');
  win.runFrames();
  assert.deepStrictEqual(seen, [], 'unchanged geometry must not notify');
  win.visualViewport.height = 520;
  win.visualViewport.emit('resize');
  win.runFrames();
  win.visualViewport.emit('scroll');
  win.runFrames();
  assert.deepStrictEqual(seen, [520]);
  off();
  win.visualViewport.height = 640;
  win.visualViewport.emit('resize');
  win.runFrames();
  assert.deepStrictEqual(seen, [520], 'unsubscribe stops delivery');
});

test('destroy removes listeners and the safe-area probe', () => {
  const { win, doc, controller } = makeController();
  controller.start();
  assert.strictEqual(doc.children.length, 1);
  controller.destroy();
  assert.strictEqual(doc.children.length, 0);
  assert.strictEqual(win.visualViewport.count('resize'), 0);
});

console.log('\nPASS ' + passed + ' checks');
