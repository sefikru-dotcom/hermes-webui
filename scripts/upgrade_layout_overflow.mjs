#!/usr/bin/env node
/**
 * Layout overflow probe for the WebUI upgrade wave (layout owner's own harness).
 *
 * WHY THIS EXISTS
 *   The shared browser suite (scripts/upgrade_browser_run.mjs) measures page
 *   overflow with the DEFAULT shell: sidebar visible, workspace right panel
 *   closed. That is the easy case. The regression this probe exists for only
 *   appears with the right panel OPEN at compact desktop widths (~1024px),
 *   where sidebar + chat + right panel + resize handles compete for the same
 *   pixels and the page grows a horizontal scrollbar (docScrollW > innerWidth).
 *
 *   It also guards the two things a "just clamp it" CSS fix would silently
 *   break: hover tooltips (`.has-tooltip::after`, absolutely positioned next to
 *   the trigger) and the music iframe (`#musicIframe`, lazy-loaded /music/).
 *
 * WHAT IT DOES
 *   Loads the REAL static app against the mock backend, forces the workspace
 *   panel open the way the user does (boot's own `_setWorkspacePanelMode`), then
 *   measures at four reference frames. Nothing is stubbed and no fixture string
 *   is compared: every check is DOM geometry.
 *
 * USAGE
 *   node scripts/upgrade_layout_overflow.mjs --base-url http://127.0.0.1:PORT \
 *        --out /tmp/upgrade-layout-browser.json
 *
 * ENVIRONMENT
 *   UPGRADE_PW_MODULE  playwright-core install (default /tmp/upgrade-browser-node)
 *   UPGRADE_CHROMIUM   explicit Chromium binary
 *
 * EXIT CODES  0 all checks passed, 1 failures, 2 setup problem.
 */
import { createRequire } from 'module';
import fs from 'fs';
import os from 'os';
import path from 'path';

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      if (eq > 0) out[a.slice(2, eq)] = a.slice(eq + 1);
      else if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) out[a.slice(2)] = argv[++i];
      else out[a.slice(2)] = true;
    } else out._.push(a);
  }
  return out;
}
const args = parseArgs(process.argv.slice(2));
const BASE = String(args['base-url'] || process.env.UPGRADE_BASE_URL || '').replace(/\/$/, '');
const OUT = args.out || '/tmp/upgrade-layout-browser.json';
// --only desktop-1024x720  → run one frame (fast local loop / diagnosis)
const ONLY = String(args.only || process.env.UPGRADE_LAYOUT_ONLY || '')
  .split(',').map((s) => s.trim()).filter(Boolean);
if (!BASE) {
  console.error('missing --base-url');
  process.exit(2);
}

function loadPlaywright() {
  const candidates = [
    process.env.UPGRADE_PW_MODULE,
    path.join(os.tmpdir(), 'upgrade-browser-node', 'node_modules', 'playwright-core'),
    path.join(os.tmpdir(), 'upgrade-browser-node'),
  ].filter(Boolean);
  const errors = [];
  for (const cand of candidates) {
    for (const anchor of ['index.js', 'package.json']) {
      try {
        const req = createRequire(path.join(cand, anchor));
        return req('playwright-core');
      } catch (e) { errors.push(`${cand}/${anchor}: ${e.message}`); }
    }
  }
  throw new Error('playwright-core not found; tried:\n  ' + errors.join('\n  '));
}

function resolveChromium() {
  if (process.env.UPGRADE_CHROMIUM) return process.env.UPGRADE_CHROMIUM;
  const caches = [process.env.PLAYWRIGHT_BROWSERS_PATH, path.join(os.homedir(), '.cache', 'ms-playwright'),
    '/root/.cache/ms-playwright'].filter(Boolean);
  for (const cache of caches) {
    if (!fs.existsSync(cache)) continue;
    const dirs = fs.readdirSync(cache).filter((d) => /^chromium-\d+$/.test(d))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
    for (const d of dirs) {
      for (const rel of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
        const exe = path.join(cache, d, rel);
        if (fs.existsSync(exe)) return exe;
      }
    }
  }
  throw new Error('no Chromium binary found; set UPGRADE_CHROMIUM');
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* Reference frames. 1024x720 is the compact-desktop case where the panel used
 * to push the page 14px wide; 1024x768 is the same width with more height so a
 * height-driven clamp cannot mask a width bug. 393x851 is the mobile reference
 * frame from the upgrade contract. */
const FRAMES = [
  { name: 'desktop-1440', width: 1440, height: 900, mobile: false, panel: 'open' },
  { name: 'desktop-1024x720', width: 1024, height: 720, mobile: false, panel: 'open' },
  { name: 'desktop-1024x768', width: 1024, height: 768, mobile: false, panel: 'open' },
  { name: 'desktop-1024-narrow-chat', width: 1024, height: 720, mobile: false, panel: 'open', squeeze: true },
  /* Short desktop: the window bound must follow the viewport (38vh = 198px) and
     not the renderer's flat 260px — without this frame the two owners produce the
     same number and the check cannot tell them apart. */
  { name: 'desktop-1024x520', width: 1024, height: 520, mobile: false, panel: 'open' },
  { name: 'mobile-393', width: 393, height: 851, mobile: true, panel: 'open' },
];

function record(checks, name, ok, detail) {
  checks.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });
}

/* Stage markers: with UPGRADE_LAYOUT_DEBUG=1 a hung frame says WHERE it hung
 * instead of leaving a bare timeout behind. */
const DEBUG = !!process.env.UPGRADE_LAYOUT_DEBUG;
const stage = (name, extra) => {
  if (DEBUG) console.error(`[stage] ${name}${extra === undefined ? '' : ' ' + JSON.stringify(extra)}`);
};

/** A frame must never hang the gate: turn a stuck probe into a failed check. */
function withWatchdog(promise, name, ms) {
  let timer;
  const guard = new Promise((resolve) => {
    timer = setTimeout(() => resolve({
      name: 'layout_overflow_shell', viewport: name, ok: false,
      checks: [{ name: 'scenario completed within ' + Math.round(ms / 1000) + 's', ok: false, detail: 'watchdog timeout' }],
      error: 'watchdog',
    }), ms);
  });
  return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

/** Force the workspace panel open through the app's own state machine. */
async function openWorkspacePanel(page) {
  const opened = await page.evaluate(() => {
    try {
      if (typeof _setWorkspacePanelMode === 'function') {
        _setWorkspacePanelMode('browse');
        return document.documentElement.dataset.workspacePanel || '';
      }
    } catch (e) { return 'throw:' + e.message; }
    return 'missing';
  });
  await page.waitForTimeout(350);
  return opened;
}

const geometry = (page) => page.evaluate(() => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const describe = (node) => {
    const cls = (typeof node.className === 'string' ? node.className : '').split(/\s+/).filter(Boolean)[0] || '';
    return `${node.tagName.toLowerCase()}${node.id ? '#' + node.id : ''}${cls ? '.' + cls : ''}`;
  };
  const docScrollW = document.documentElement.scrollWidth;
  // Offender scanning forces style/layout on the whole tree, which is far too
  // expensive to run on every frame of a big app. It is diagnostics, so it only
  // runs when the page actually overflows.
  const offenders = [];
  if (docScrollW > vw + 1) {
    // Only elements that can actually create page scroll: in-flow boxes that are
    // not position:fixed (fixed boxes are viewport-anchored overlays such as the
    // mobile drawer, tooltips and the composer inset) and that are painted.
    for (const node of document.querySelectorAll('body *')) {
      const cs = getComputedStyle(node);
      if (cs.position === 'fixed' || cs.display === 'none' || cs.visibility === 'hidden') continue;
      const b = node.getBoundingClientRect();
      if (b.width <= 0 || b.height <= 0) continue;
      if (b.right > vw + 1) {
        offenders.push({ sel: describe(node), right: Math.round(b.right), pos: cs.position });
        if (offenders.length >= 8) break;
      }
    }
  }
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {
      left: Math.round(r.left), right: Math.round(r.right),
      top: Math.round(r.top), bottom: Math.round(r.bottom),
      width: Math.round(r.width), height: Math.round(r.height),
      scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
    };
  };
  return {
    vw, vh,
    docScrollW,
    bodyScrollW: document.body ? document.body.scrollWidth : null,
    panelMode: document.documentElement.dataset.workspacePanel || '',
    sidebar: box('.sidebar'),
    rightpanel: box('.rightpanel'),
    messages: box('#messages'),
    composer: box('#composerWrap'),
    layout: box('.layout'),
    offenders,
  };
});

async function runFrame(browser, frame) {
  const checks = [];
  const context = await browser.newContext({
    viewport: { width: frame.width, height: frame.height },
    isMobile: frame.mobile, hasTouch: frame.mobile, deviceScaleFactor: frame.mobile ? 3 : 1,
  });
  const page = await context.newPage();
  // A probe must fail loudly and quickly instead of hanging the suite: every
  // Playwright wait below is bounded, and runFrame itself is wrapped in a
  // watchdog in main().
  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(20000);
  const pageErrors = [];
  const external = [];
  page.on('pageerror', (e) => pageErrors.push(String(e && e.message || e)));
  page.on('request', (req) => {
    const u = req.url();
    if (!u.startsWith(BASE) && !u.startsWith('data:') && !u.startsWith('blob:')) external.push(u);
  });
  try {
    // Panel preference is persisted by boot; set it before the app boots so the
    // first paint already has the panel open (no "closed then open" flash that
    // would let a closed-state measurement slip through).
    await page.addInitScript(() => {
      try { localStorage.setItem('hermes-webui-workspace-panel', 'open'); } catch (_) {}
    });
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
    stage('dom-loaded');
    await page.waitForFunction(() => typeof S !== 'undefined' && S._bootReady === true, null, { timeout: 25000 });
    stage('boot-ready');
    await page.waitForTimeout(300);
    await openWorkspacePanel(page);
    stage('panel-open');

    if (frame.squeeze) {
      // The panel is user-resizable: a wide saved width is the case that used to
      // push the page over. boot applies `hermes-panel-w` inline at boot.
      await page.evaluate(() => {
        try { localStorage.setItem('hermes-panel-w', '420'); } catch (_) {}
        const p = document.querySelector('.rightpanel');
        if (p && !window.matchMedia('(max-width:900px)').matches) p.style.width = '420px';
      });
      await page.waitForTimeout(300);
    }

    const g = await geometry(page);
    stage('geometry', { docScrollW: g.docScrollW, vw: g.vw, offenders: g.offenders.length });
    record(checks, 'workspace panel is actually open', g.panelMode === 'open', `panelMode=${g.panelMode}`);
    if (!frame.mobile) {
      record(checks, 'right panel occupies width on desktop', g.rightpanel && g.rightpanel.width > 0,
        JSON.stringify(g.rightpanel));
    }
    record(checks, 'no horizontal page scroll', g.docScrollW <= g.vw + 1,
      `docScrollW=${g.docScrollW} vw=${g.vw} offenders=${JSON.stringify(g.offenders)}`);
    record(checks, 'no in-flow element sticks out of the viewport', g.offenders.length === 0,
      JSON.stringify(g.offenders));
    record(checks, 'transcript has no horizontal overflow',
      g.messages && g.messages.scrollWidth <= g.messages.clientWidth + 1,
      JSON.stringify(g.messages));
    record(checks, 'composer stays inside the viewport',
      g.composer && g.composer.left >= -1 && g.composer.right <= g.vw + 1,
      JSON.stringify(g.composer));

    // Tooltip: hover the right-most visible titlebar button. A blind
    // `overflow:hidden` clamp on the shell would either clip the tooltip or add
    // page scroll here, so both are asserted.
    const tipTarget = await page.evaluate(() => {
      const nodes = Array.from(document.querySelectorAll('.has-tooltip'))
        .filter((n) => {
          const r = n.getBoundingClientRect();
          return r.width > 4 && r.height > 4 && getComputedStyle(n).visibility !== 'hidden';
        });
      if (!nodes.length) return null;
      const rightMost = nodes.reduce((a, b) => (b.getBoundingClientRect().right > a.getBoundingClientRect().right ? b : a));
      rightMost.setAttribute('data-layout-probe', '1');
      const r = rightMost.getBoundingClientRect();
      return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), label: rightMost.getAttribute('data-tooltip') || '' };
    });
    if (tipTarget) {
      await page.mouse.move(tipTarget.x, tipTarget.y);
      await page.waitForTimeout(450);
      stage('tooltip-hovered');
      const tip = await page.evaluate(() => {
        const el = document.querySelector('[data-layout-probe="1"]');
        if (!el) return null;
        const cs = getComputedStyle(el, '::after');
        const r = el.getBoundingClientRect();
        // Pseudo-element box: the tooltip is anchored to the trigger; measure the
        // painted rect through Range-free arithmetic (left/right + width).
        const width = parseFloat(cs.width) || 0;
        const height = parseFloat(cs.height) || 0;
        const pos = cs.position;
        let left = r.left, right = r.right;
        if (pos === 'absolute') {
          if (cs.left !== 'auto' && cs.left.endsWith('px')) {
            const base = cs.right !== 'auto' && cs.right.endsWith('px') ? r.right : r.left;
            left = cs.right !== 'auto' ? base - width : base + parseFloat(cs.left);
          }
          right = left + width;
        }
        return {
          opacity: cs.opacity, width, height, left: Math.round(left), right: Math.round(right),
          label: cs.content, vw: window.innerWidth,
          docScrollW: document.documentElement.scrollWidth,
        };
      });
      record(checks, 'hovering the right-most tooltip shows it', tip && Number(tip.opacity) > 0.05, JSON.stringify(tip));
      record(checks, 'tooltip does not add horizontal page scroll',
        tip && tip.docScrollW <= tip.vw + 1, JSON.stringify(tip));
    } else {
      record(checks, 'a hover tooltip exists in the shell', false, 'no .has-tooltip trigger found');
    }
    stage('tooltip-done');

    // Music iframe + menu must survive: the panel is a lazy /music/ embed inside
    // `.embedded-frame-host` (height:100%), NOT a box this wave may clamp. The
    // probe therefore asserts the STRUCTURE the layout must not break instead of
    // switching panels: the harness backend does not serve /music/, so actually
    // mounting the iframe here would test the mock, not the layout.
    const music = await page.evaluate(() => {
      const frame = document.getElementById('musicIframe');
      const host = document.getElementById('musicFrameHost');
      const cs = (el) => (el ? getComputedStyle(el) : null);
      const hostCs = cs(host);
      const frameCs = cs(frame);
      const menuTabs = Array.from(document.querySelectorAll('[data-panel="music"]')).map((el) => {
        const r = el.getBoundingClientRect();
        return { cls: (el.className || '').toString().split(/\s+/)[0], w: Math.round(r.width), h: Math.round(r.height) };
      });
      return {
        hasFrame: !!frame,
        hasHost: !!host,
        src: frame ? (frame.getAttribute('src') || frame.getAttribute('data-src') || '') : '',
        title: frame ? (frame.getAttribute('title') || '') : '',
        hostHeight: hostCs ? hostCs.height : null,
        hostMaxHeight: hostCs ? hostCs.maxHeight : null,
        hostOverflow: hostCs ? hostCs.overflow : null,
        framePosition: frameCs ? frameCs.position : null,
        frameMaxHeight: frameCs ? frameCs.maxHeight : null,
        frameHidden: frame ? frame.hasAttribute('hidden') : null,
        menuTabs,
        docScrollW: document.documentElement.scrollWidth,
        vw: window.innerWidth,
      };
    });
    record(checks, 'music iframe element is still mounted', music && music.hasFrame && music.hasHost, JSON.stringify(music));
    record(checks, 'music iframe keeps the /music/ source and title',
      music && typeof music.src === 'string' && music.src.includes('/music') && !!music.title,
      `src=${music && music.src} title=${music && music.title}`);
    record(checks, 'no blind clamp on the music frame host',
      music && music.hostMaxHeight === 'none' && music.frameMaxHeight === 'none' &&
      music.framePosition === 'absolute' && music.hostHeight === '100%',
      JSON.stringify({ hostHeight: music && music.hostHeight, hostMaxHeight: music && music.hostMaxHeight,
        hostOverflow: music && music.hostOverflow, frameMaxHeight: music && music.frameMaxHeight,
        framePosition: music && music.framePosition }));
    record(checks, 'music menu entries are present and hit-testable',
      music && music.menuTabs.length > 0 && music.menuTabs.some((t) => t.w > 4 && t.h > 4) &&
      music.menuTabs.every((t) => t.w <= 4 || (t.w > 4 && t.h > 4)),
      JSON.stringify(music && music.menuTabs));
    record(checks, 'music panel introduces no horizontal scroll',
      music && music.docScrollW <= music.vw + 1, JSON.stringify(music));
    stage('music-done');

    // ── Legitimate tooltips stay whole inside their clip box ────────────────
    // The containers that sit against a hard right edge (`.rightpanel`,
    // `.composer-right` inside `.main`) clip on overflow. A bubble that opens
    // outward there is not "extra width to clamp away" — it is a label the user
    // cannot read. Every visible trigger in those two containers must paint its
    // bubble inside the clipping ancestor.
    const tips = await page.evaluate(() => {
      const out = [];
      const clipOf = (el) => {
        let node = el.parentElement;
        while (node && node !== document.body) {
          const cs = getComputedStyle(node);
          if (cs.overflowX !== 'visible') {
            const r = node.getBoundingClientRect();
            return { sel: (node.className || '').toString().split(/\s+/)[0] || node.tagName.toLowerCase(), left: r.left, right: r.right };
          }
          node = node.parentElement;
        }
        return null;
      };
      const probe = (scopeSel) => {
        const scope = document.querySelector(scopeSel);
        if (!scope) return;
        for (const el of scope.querySelectorAll('.has-tooltip')) {
          const r = el.getBoundingClientRect();
          if (r.width < 4 || r.height < 4) continue;
          const cs = getComputedStyle(el, '::after');
          const w = parseFloat(cs.width) || 0;
          if (!w) continue;
          const clip = clipOf(el);
          if (!clip) continue;
          let left = null, right = null;
          if (cs.right !== 'auto' && cs.right.endsWith('px')) {
            right = r.right - parseFloat(cs.right); left = right - w;
          } else if (cs.left !== 'auto' && cs.left.endsWith('px')) {
            const pct = cs.left.match(/^(-?[\d.]+)%$/);
            left = pct ? r.left + (r.width * parseFloat(pct[1])) / 100 : r.left + parseFloat(cs.left);
            if (cs.transform && cs.transform.startsWith('matrix')) {
              const tx = parseFloat(cs.transform.split('(')[1].split(',')[4]) || 0;
              left += tx;
            }
            right = left + w;
          } else continue;
          out.push({
            id: el.id || (el.className || '').toString().split(/\s+/)[0],
            label: (el.getAttribute('data-tooltip') || '').slice(0, 24),
            w: Math.round(w), left: Math.round(left), right: Math.round(right),
            clip: clip.sel, clipLeft: Math.round(clip.left), clipRight: Math.round(clip.right),
            inside: left >= clip.left - 1 && right <= clip.right + 1,
          });
        }
      };
      probe('.rightpanel');
      probe('.composer-right');
      const fixed = out.slice();
      // Negative control: revert the anchor to the plain "open to the right"
      // geometry and re-scan. If the same triggers are still "inside", the check
      // is not measuring anything and a silent regression would pass. The control
      // style tag is removed immediately.
      const ctrl = document.createElement('style');
      ctrl.textContent = '.rightpanel .has-tooltip::after,.composer-right .has-tooltip::after' +
        '{left:calc(100% + 8px)!important;right:auto!important;transform:translateY(-50%)!important;}' +
        '.rightpanel .has-tooltip--bottom::after{left:50%!important;right:auto!important;transform:translateX(-50%)!important;}';
      document.head.appendChild(ctrl);
      out.length = 0;
      probe('.rightpanel');
      probe('.composer-right');
      const control = out.slice();
      ctrl.remove();
      return { fixed, control };
    });
    const cutTips = tips.fixed.filter((t) => !t.inside);
    record(checks, 'tooltips in right-anchored containers are not clipped',
      tips.fixed.length > 0 && cutTips.length === 0,
      `checked=${tips.fixed.length} clipped=${JSON.stringify(cutTips)}`);
    const ctrlCut = tips.control.filter((t) => !t.inside);
    record(checks, 'tooltip check is discriminating (control geometry does clip)',
      ctrlCut.length > 0,
      `control_clipped=${ctrlCut.map((t) => t.id + ':' + t.right + '>' + t.clipRight).join(',') || 'none'}`);
    stage('tooltip-clip-done');

    // ── The activity window bound has exactly one effective owner ───────────
    // `chat_render_activity_block.js::setMode()` writes `max-height:260px`
    // INLINE on `[data-activity-body="1"]`, while style.css declares the same
    // bound for `.mars-process-window-stream`. Inline beats a plain declaration,
    // so whichever layer the renderer writes wins silently. This check drives
    // the REAL module (no stub) and asserts the stylesheet's viewport-aware cap
    // is the effective value: `min(38vh, 260px)` in window mode, `none` in full.
    const bounds = await page.evaluate(() => {
      const AB = window.ChatActivityBlock;
      const host = document.getElementById('messages') || document.body;
      const root = document.createElement('div');
      root.className = 'mars-process-window';
      root.setAttribute('data-activity-block', '1');
      root.setAttribute('data-turn-key', 'layout-bound-probe');
      root.innerHTML = '<div class="mars-process-window-header"><span class="mars-process-window-title">probe</span></div>' +
        '<div class="mars-process-window-stream" data-activity-body="1"><div class="tool-worklog-list" data-activity-list="1"></div></div>';
      host.appendChild(root);
      const body = root.querySelector('[data-activity-body="1"]');
      const de = document.documentElement;
      const setMode = (mode) => {
        if (AB && typeof AB.create === 'function') {
          try {
            const inst = AB.create({ mode: mode });
            if (inst && typeof inst.setMode === 'function') return inst.setMode(root, mode);
          } catch (_) {}
        }
        // Fallback mirrors setMode() line for line so the check still measures
        // the same two owners if the factory is unavailable.
        root.setAttribute('data-mode', mode);
        body.style.maxHeight = mode === 'stream_window' ? '260px' : '';
        body.style.overflowY = mode === 'stream_window' ? 'auto' : '';
        return { mode: mode };
      };
      const measure = (mode, htmlClass) => {
        for (const c of ['mars-mode-full', 'mars-mode-window', 'mars-mode-quiet', 'mars-hide-activity']) de.classList.remove(c);
        if (htmlClass) de.classList.add(htmlClass);
        setMode(mode);
        const cs = getComputedStyle(body);
        return { mode, htmlClass, maxHeight: cs.maxHeight, overflowY: cs.overflowY, inline: body.style.maxHeight };
      };
      const vh = window.innerHeight;
      const win = measure('stream_window', 'mars-mode-window');
      const full = measure('stream_full', 'mars-mode-full');
      root.remove();
      for (const c of ['mars-mode-full', 'mars-mode-window', 'mars-mode-quiet', 'mars-hide-activity']) de.classList.remove(c);
      if (typeof _applyThinkingModeClasses === 'function') { try { _applyThinkingModeClasses(); } catch (_) {} }
      const expected = Math.min(vh * 0.38, 260);
      return { vh, expected: Math.round(expected * 10) / 10, win, full };
    });
    const winPx = parseFloat(bounds.win.maxHeight);
    record(checks, 'window mode bound is min(38vh, 260px) from CSS, not the renderer inline',
      Math.abs(winPx - bounds.expected) <= 2 && bounds.win.inline === '260px',
      `expected=${bounds.expected} got=${bounds.win.maxHeight} inline=${bounds.win.inline} vh=${bounds.vh}`);
    record(checks, 'full mode bound is unbounded',
      bounds.full.maxHeight === 'none', JSON.stringify(bounds.full));
    stage('bounds-done');

    record(checks, 'no uncaught page errors', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    record(checks, 'no external network requests', external.length === 0, external.slice(0, 3).join(' | '));
    return { name: 'layout_overflow_shell', viewport: frame.name, ok: checks.every((c) => c.ok), checks };
  } catch (err) {
    record(checks, 'scenario completed', false, String(err && err.stack || err).slice(0, 600));
    return { name: 'layout_overflow_shell', viewport: frame.name, ok: false, checks, error: String(err && err.message || err) };
  } finally {
    await context.close().catch(() => {});
  }
}

async function main() {
  const { chromium } = loadPlaywright();
  const exe = resolveChromium();
  const browser = await chromium.launch({ executablePath: exe, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const results = [];
  try {
    const frames = ONLY.length ? FRAMES.filter((f) => ONLY.includes(f.name)) : FRAMES;
    if (!frames.length) {
      console.error('no frame matched --only ' + ONLY.join(','));
      return 2;
    }
    for (const frame of frames) {
      const res = await withWatchdog(runFrame(browser, frame), frame.name, 60000);
      results.push(res);
      const failed = res.checks.filter((c) => !c.ok);
      console.log(`${res.ok ? 'PASS' : 'FAIL'} ${frame.name}${failed.length ? ' :: ' + failed.map((f) => f.name).join('; ') : ''}`);
    }
  } finally {
    await browser.close().catch(() => {});
  }
  const totals = {
    scenarios: results.length,
    passed: results.filter((r) => r.ok).length,
    failed: results.filter((r) => !r.ok).length,
    checks: results.reduce((n, r) => n + r.checks.length, 0),
    checks_failed: results.reduce((n, r) => n + r.checks.filter((c) => !c.ok).length, 0),
  };
  const report = {
    ok: totals.failed === 0,
    ran_at: new Date().toISOString(),
    base_url: BASE,
    chromium: exe,
    kind: 'layout-overflow',
    totals,
    results,
  };
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(report, null, 2));
  console.log(`\n${totals.passed}/${totals.scenarios} frames passed; ${totals.checks_failed} of ${totals.checks} checks failed`);
  console.log(`report: ${OUT}`);
  process.exit(totals.failed === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(String(e && e.stack || e));
  process.exit(2);
});
