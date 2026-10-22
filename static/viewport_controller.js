/* Unified viewport geometry controller (Mars WebUI).
 *
 * One owner for every piece of "how tall is the app right now" state:
 * visualViewport height/offset, on-screen keyboard occlusion, safe-area and
 * dynamic viewport units. Before this module the geometry was recomputed from
 * three unrelated places (a window resize handler, a visualViewport handler and
 * a debounced reflow timer in boot.js) which disagreed during keyboard
 * animation and left the composer under the keyboard on iOS and in split mode.
 *
 * Contract (classic script, no bundler, loaded BEFORE boot.js):
 *   window.MarsViewport.init(win?, doc?)  -> controller (idempotent)
 *   window.MarsViewport.flush()           -> state, applied synchronously
 *   window.MarsViewport.refresh()         -> coalesced (one rAF at most)
 *   window.MarsViewport.onChange(fn)      -> unsubscribe
 *   window.MarsViewport.state             -> last applied state
 *
 * CSS variables written on <html>:
 *   --app-viewport-height  visible height in px (visualViewport aware)
 *   --app-vh               1% of the visible height (replaces 1dvh maths)
 *   --keyboard-bottom-inset  px the keyboard occludes (0 / removed when none)
 *   --safe-area-bottom     measured env(safe-area-inset-bottom)
 * Attributes on <html>:
 *   data-keyboard-open="1|0", data-viewport-height="<px>"
 *
 * No competing listeners: this module is the only place that subscribes to
 * visualViewport/resize/orientationchange/pageshow. boot.js keeps a legacy
 * fallback that is used only when this file is not on the page.
 */
(function (root) {
  'use strict';

  // A shrink smaller than this is browser chrome collapsing / URL-bar
  // animation, not an on-screen keyboard. Keeps the composer from jumping
  // 40-60px on every scroll on iOS and Chrome Android.
  var KEYBOARD_MIN_INSET = 80;
  // Chromium's "force enable zoom" reports a pinch-zoomed viewport whose
  // innerHeight - vv.height is the zoom, not the keyboard.
  var SCALE_EPSILON = 0.05;

  function _num(value, fallback) {
    var n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  }

  /**
   * Pure geometry: no DOM, no globals. Everything the controller applies is
   * derived here so the rules are testable without a browser.
   */
  function computeViewportState(input) {
    var src = input || {};
    var innerHeight = _num(src.innerHeight, 0);
    var innerWidth = _num(src.innerWidth, 0);
    var hasVV = src.vvHeight !== null && src.vvHeight !== undefined &&
      Number.isFinite(Number(src.vvHeight));
    var vvHeight = hasVV ? _num(src.vvHeight, innerHeight) : innerHeight;
    var vvWidth = hasVV ? _num(src.vvWidth, innerWidth) : innerWidth;
    var scale = hasVV ? _num(src.vvScale, 1) : 1;
    var offsetTop = hasVV ? _num(src.vvOffsetTop, 0) : 0;
    var pinchZoomed = Math.abs(scale - 1) > SCALE_EPSILON;
    var occlusion = Math.max(0, Math.ceil(innerHeight - (vvHeight + offsetTop)));
    var threshold = _num(src.keyboardMinInset, KEYBOARD_MIN_INSET);
    var keyboardInset = (!hasVV || pinchZoomed || occlusion < threshold) ? 0 : occlusion;
    return {
      height: Math.max(0, Math.round(vvHeight || innerHeight)),
      width: Math.max(0, Math.round(vvWidth || innerWidth)),
      offsetTop: Math.round(offsetTop),
      scale: scale,
      keyboardInset: keyboardInset,
      keyboardOpen: keyboardInset > 0,
      pinchZoomed: pinchZoomed,
      safeAreaBottom: Math.max(0, Math.round(_num(src.safeAreaBottom, 0))),
      hasVisualViewport: hasVV
    };
  }

  function _signature(state) {
    return [state.height, state.width, state.offsetTop, state.keyboardInset,
      state.safeAreaBottom, state.keyboardOpen ? 1 : 0].join('|');
  }

  class ViewportController {
    constructor(options) {
      var opts = options || {};
      this.win = opts.window || root;
      this.doc = opts.document || (this.win && this.win.document) || null;
      this.minKeyboardInset = _num(opts.keyboardMinInset, KEYBOARD_MIN_INSET);
      this._subs = new Set();
      this._handlers = [];
      this._state = null;
      this._signature = '';
      this._frame = 0;
      this._frameKind = '';
      this._started = false;
      this._probe = null;
      this._onEvent = this._onEvent.bind(this);
      this._runFrame = this._runFrame.bind(this);
    }

    /* ── lifecycle ─────────────────────────────────────────────────────── */

    start() {
      if (this._started) return this;
      this._started = true;
      var vv = this.win && this.win.visualViewport;
      if (vv) {
        this._listen(vv, 'resize', this._onEvent);
        this._listen(vv, 'scroll', this._onEvent, { passive: true });
      }
      // window resize stays registered even with visualViewport: desktop
      // browsers without vv, and Chrome Android where innerHeight is what
      // actually changes when the keyboard opens.
      this._listen(this.win, 'resize', this._onEvent, { passive: true });
      this._listen(this.win, 'orientationchange', this._onEvent);
      this._listen(this.win, 'pageshow', this._onEvent);
      this._listen(this.doc, 'visibilitychange', this._onEvent);
      this.flush();
      return this;
    }

    stop() {
      this._cancelFrame();
      this._handlers.forEach(function (entry) {
        try { entry[0].removeEventListener(entry[1], entry[2], entry[3]); } catch (_) { /* detached target */ }
      });
      this._handlers = [];
      this._started = false;
      return this;
    }

    destroy() {
      this.stop();
      this._subs.clear();
      if (this._probe && this._probe.parentNode) {
        try { this._probe.parentNode.removeChild(this._probe); } catch (_) { /* ignore */ }
      }
      this._probe = null;
      this._state = null;
      this._signature = '';
      return this;
    }

    /* ── public API ────────────────────────────────────────────────────── */

    get state() { return this._state; }

    onChange(fn) {
      if (typeof fn !== 'function') return function () {};
      this._subs.add(fn);
      var subs = this._subs;
      return function () { subs.delete(fn); };
    }

    /** Coalesced refresh: at most one measurement per animation frame. */
    refresh() {
      this._schedule();
      return this;
    }

    /** Immediate measurement + apply (used on boot/pageshow and by boot.js). */
    flush() {
      this._cancelFrame();
      this._apply(this._measure());
      return this._state;
    }

    /* ── internals ─────────────────────────────────────────────────────── */

    _listen(target, type, fn, opts) {
      if (!target || typeof target.addEventListener !== 'function') return;
      try {
        target.addEventListener(type, fn, opts);
        this._handlers.push([target, type, fn, opts]);
      } catch (_) { /* non-DOM target */ }
    }

    _onEvent() { this._schedule(); }

    _schedule() {
      if (this._frame) return;
      var raf = this.win && this.win.requestAnimationFrame;
      if (typeof raf === 'function') {
        this._frameKind = 'raf';
        this._frame = raf(this._runFrame);
      } else {
        this._frameKind = 'timer';
        this._frame = setTimeout(this._runFrame, 16);
      }
    }

    _cancelFrame() {
      if (!this._frame) return;
      var id = this._frame;
      this._frame = 0;
      if (this._frameKind === 'raf' && this.win && typeof this.win.cancelAnimationFrame === 'function') {
        try { this.win.cancelAnimationFrame(id); } catch (_) { /* ignore */ }
      } else {
        clearTimeout(id);
      }
      this._frameKind = '';
    }

    _runFrame() {
      this._frame = 0;
      this._frameKind = '';
      this._apply(this._measure());
    }

    _measure() {
      var win = this.win || {};
      var vv = win.visualViewport || null;
      var state = computeViewportState({
        innerHeight: win.innerHeight,
        innerWidth: win.innerWidth,
        vvHeight: vv ? vv.height : null,
        vvWidth: vv ? vv.width : null,
        vvScale: vv ? vv.scale : null,
        vvOffsetTop: vv ? vv.offsetTop : null,
        safeAreaBottom: this._measureSafeArea(),
        keyboardMinInset: this.minKeyboardInset
      });
      // Android Chrome shrinks innerHeight *with* the keyboard, so the occlusion
      // computed above stays ~0 even though the keyboard is open. Track the
      // tallest height seen for the current width and treat a large drop as a
      // keyboard as well, so consumers that only need the boolean
      // (data-keyboard-open, split ratio) work on both engines.
      if (this._baselineWidth !== state.width) {
        this._baselineWidth = state.width;
        this._baselineHeight = state.height;
      }
      if (state.height > this._baselineHeight) this._baselineHeight = state.height;
      state.baselineHeight = this._baselineHeight;
      state.keyboardOpen = state.keyboardOpen ||
        (this._baselineHeight - state.height >= this.minKeyboardInset);
      return state;
    }

    /**
     * env(safe-area-inset-bottom) is CSS-only, so measure it through a probe
     * element. Consumers that need the number in JS (split panel, future
     * AppearanceStore previews) read --safe-area-bottom instead of duplicating
     * the probe.
     */
    _measureSafeArea() {
      var el = this._ensureProbe();
      if (!el || typeof el.getBoundingClientRect !== 'function') return 0;
      try {
        var rect = el.getBoundingClientRect();
        return Math.max(0, Math.round(_num(rect && rect.height, 0)));
      } catch (_) {
        return 0;
      }
    }

    _ensureProbe() {
      if (this._probe) return this._probe;
      var doc = this.doc;
      if (!doc || !doc.body || typeof doc.createElement !== 'function') return null;
      try {
        var el = doc.createElement('div');
        el.className = 'mars-vp-safe-probe';
        el.setAttribute('aria-hidden', 'true');
        el.style.cssText = 'position:fixed;left:0;bottom:0;width:0;height:env(safe-area-inset-bottom,0px);' +
          'visibility:hidden;pointer-events:none;z-index:-1;';
        doc.body.appendChild(el);
        this._probe = el;
        return el;
      } catch (_) {
        return null;
      }
    }

    _apply(state) {
      this._state = state;
      var doc = this.doc;
      var rootEl = doc && doc.documentElement;
      if (rootEl && rootEl.style) {
        var style = rootEl.style;
        _setVar(style, '--app-viewport-height', state.height ? state.height + 'px' : '');
        _setVar(style, '--app-vh', state.height ? (state.height / 100) + 'px' : '');
        _setVar(style, '--safe-area-bottom', state.safeAreaBottom + 'px');
        // Zero means "no keyboard": remove the variable so every
        // var(--keyboard-bottom-inset, 0px) consumer falls back to 0 and the
        // composer does not keep a stale gap after the keyboard closes.
        _setVar(style, '--keyboard-bottom-inset',
          state.keyboardInset > 0 ? state.keyboardInset + 'px' : '');
        if (rootEl.dataset) {
          rootEl.dataset.keyboardOpen = state.keyboardOpen ? '1' : '0';
          rootEl.dataset.viewportHeight = String(state.height);
        }
      }
      var signature = _signature(state);
      if (signature === this._signature) return;
      this._signature = signature;
      var subs = Array.from(this._subs);
      for (var i = 0; i < subs.length; i++) {
        try { subs[i](state); } catch (_) { /* a subscriber must not break geometry */ }
      }
    }
  }

  function _setVar(style, name, value) {
    try {
      if (value === '' || value === null || value === undefined) style.removeProperty(name);
      else style.setProperty(name, value);
    } catch (_) { /* ignore */ }
  }

  /* ── global wiring ───────────────────────────────────────────────────── */

  var controller = null;

  function init(win, doc) {
    var targetWin = win || root;
    var targetDoc = doc || (targetWin && targetWin.document);
    if (!targetDoc) return null;
    if (controller) return controller;
    controller = new ViewportController({ window: targetWin, document: targetDoc });
    controller.start();
    return controller;
  }

  function refresh() {
    if (!controller) return null;
    controller.refresh();
    return controller.state;
  }

  function flush() {
    if (!controller) return null;
    return controller.flush();
  }

  function onChange(fn) {
    if (!controller) return function () {};
    return controller.onChange(fn);
  }

  var api = {
    version: '1',
    KEYBOARD_MIN_INSET: KEYBOARD_MIN_INSET,
    computeViewportState: computeViewportState,
    ViewportController: ViewportController,
    init: init,
    refresh: refresh,
    flush: flush,
    onChange: onChange,
    get state() { return controller ? controller.state : null; },
    get controller() { return controller; }
  };

  if (root) root.MarsViewport = api;

  // Auto-init: the module is self-sufficient, boot.js only consumes it. In
  // Node (tests) there is no document and this block is skipped.
  if (root && root.document) {
    if (root.document.readyState === 'loading') {
      root.document.addEventListener('DOMContentLoaded', function () {
        init(root, root.document);
      }, { once: true });
    } else {
      init(root, root.document);
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      ViewportController: ViewportController,
      computeViewportState: computeViewportState,
      KEYBOARD_MIN_INSET: KEYBOARD_MIN_INSET
    };
  }
})(typeof window !== 'undefined' ? window : globalThis);
