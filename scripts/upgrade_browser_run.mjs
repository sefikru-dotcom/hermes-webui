#!/usr/bin/env node
/**
 * Browser regression runner for the WebUI upgrade wave.
 *
 * WHAT IT DOES
 *   Loads the REAL static application (index.html + every static/*.js from the
 *   app root) inside Chromium, against the mock API/SSE server
 *   (scripts/upgrade_browser_server.py) on 127.0.0.1. No auth, no live config,
 *   no live sessions, no LLM calls: every byte comes from
 *   tests/fixtures/upgrade_chat/.
 *
 *   Assertions are behavioural (DOM structure, layout geometry, request bodies,
 *   SSE connection counts) and never compare fixture strings, so the suite stays
 *   valid after the render/appearance/layout authors rewrite production modules.
 *
 * USAGE
 *   node scripts/upgrade_browser_run.mjs --base-url http://127.0.0.1:PORT \
 *        --out /tmp/upgrade-browser.json [--viewports desktop,mobile] [--only NAME]
 *   node scripts/upgrade_browser_run.mjs --list
 *
 * ENVIRONMENT
 *   UPGRADE_PW_MODULE   path to a playwright-core install (default: temp npm
 *                       prefix /tmp/upgrade-browser-node; the project's own
 *                       node_modules and the live venv are never touched)
 *   UPGRADE_CHROMIUM    explicit Chromium binary; otherwise the newest
 *                       ~/.cache/ms-playwright/chromium-<rev>/chrome-linux64/chrome
 *   UPGRADE_BASE_URL    alternative to --base-url
 *
 * EXIT CODES  0 all checks passed, 1 at least one check failed, 2 setup problem.
 */
import { createRequire } from 'module';
import fs from 'fs';
import os from 'os';
import path from 'path';

// ---------------------------------------------------------------- CLI ------
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
const BASE = (args['base-url'] || process.env.UPGRADE_BASE_URL || '').replace(/\/$/, '');

// ------------------------------------------------------- playwright load ----
function loadPlaywright() {
  const candidates = [
    process.env.UPGRADE_PW_MODULE,
    path.join(os.tmpdir(), 'upgrade-browser-node', 'node_modules', 'playwright-core'),
    path.join(os.tmpdir(), 'upgrade-browser-node'),
  ].filter(Boolean);
  const errors = [];
  for (const cand of candidates) {
    try {
      const req = createRequire(path.join(cand, 'index.js'));
      return req('playwright-core');
    } catch (e) {
      try {
        const req = createRequire(path.join(cand, 'package.json'));
        return req('playwright-core');
      } catch (e2) {
        errors.push(`${cand}: ${e.message}`);
      }
    }
  }
  try {
    return createRequire(path.join(process.cwd(), 'package.json'))('playwright-core');
  } catch (e) {
    errors.push(`cwd: ${e.message}`);
  }
  throw new Error(
    'playwright-core not found. Install it into a TEMP prefix (never the live venv):\n' +
    '  mkdir -p /tmp/upgrade-browser-node && cd /tmp/upgrade-browser-node && \\\n' +
    '  npm install playwright-core@1.64.1 --no-audit --no-fund\n' +
    'or point UPGRADE_PW_MODULE at an existing install.\nTried:\n  ' + errors.join('\n  '));
}

function resolveChromium() {
  if (process.env.UPGRADE_CHROMIUM) return process.env.UPGRADE_CHROMIUM;
  // Honour the shared Playwright cache explicitly: a scenario may run with a
  // throwaway HOME, and the browser must never be downloaded at test time.
  const caches = [];
  if (process.env.PLAYWRIGHT_BROWSERS_PATH) caches.push(process.env.PLAYWRIGHT_BROWSERS_PATH);
  caches.push(path.join(os.homedir(), '.cache', 'ms-playwright'), '/root/.cache/ms-playwright');
  for (const cache of caches) {
    if (!cache || !fs.existsSync(cache)) continue;
    const dirs = fs.readdirSync(cache)
      .filter((d) => /^chromium-\d+$/.test(d))
      .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
    for (const d of dirs) {
      for (const rel of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
        const exe = path.join(cache, d, rel);
        if (fs.existsSync(exe)) return exe;
      }
    }
  }
  for (const exe of ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']) {
    if (fs.existsSync(exe)) return exe;
  }
  throw new Error('no Chromium binary found; set UPGRADE_CHROMIUM or install ms-playwright chromium');
}

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  // 393x851 is the requested mobile reference frame (Pixel-ish logical size).
  mobile: { width: 393, height: 851, isMobile: true, hasTouch: true, deviceScaleFactor: 3 },
};

// ------------------------------------------------------------- helpers -----
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function record(checks, name, ok, detail) {
  checks.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });
}

async function mockState(page) {
  return page.evaluate(() => fetch('/__mock/state').then((r) => r.json()));
}
/**
 * Read what the page ACTUALLY renders plus the product's single theme signal.
 *
 * The product has exactly one theme marker — `.dark` on <html> (boot.js
 * `_setResolvedTheme`); light is `:root`'s palette and `data-theme` is deleted
 * on purpose. So "light" can never be a token in the class list: it has to be
 * measured as the resolved palette. `--bg`/`--sidebar`/`--text` are read from
 * getComputedStyle on <html>, i.e. the values the browser paints with, so a
 * scenario can compare a captured light palette against a captured dark one.
 */
async function themeProbe(page) {
  return page.evaluate(() => {
    const html = document.documentElement;
    const cs = getComputedStyle(html);
    const picker = document.querySelector('[data-theme-val="light"]');
    const bg = cs.getPropertyValue('--bg').trim();
    return {
      ls: localStorage.getItem('hermes-theme'),
      htmlDark: html.classList.contains('dark'),
      bg,
      sidebar: cs.getPropertyValue('--sidebar').trim(),
      text: cs.getPropertyValue('--text').trim(),
      // Real painted background: <body> inherits var(--bg); a transparent value
      // means the palette never reached layout.
      bodyBg: getComputedStyle(document.body).backgroundColor,
      skin: html.dataset.skin || 'default',
      pickerActive: picker ? picker.classList.contains('active') : null,
    };
  });
}
async function mockPost(page, url, body) {
  return page.evaluate(
    ([u, b]) => fetch(u, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b || {}),
    }).then((r) => r.json()),
    [url, body || {}]
  );
}
const chatSse = (st) => (st.sse_connections || []).filter((c) => c.path === '/api/chat/stream').length;

async function boot(page, { seed = false, fixture = 'stream_long.json', sessionId = '' } = {}) {
  // Land on the origin first so the mock control endpoints are reachable; this
  // load is throwaway, it only proves the app boots.
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof S !== 'undefined' && S._bootReady === true, null, { timeout: 20000 });
  // Let the boot-time settings reconcile POST (if any) land before resetting, so
  // the reset cannot be overtaken by a request already in flight.
  await page.waitForTimeout(150);
  // Reset the backend — settings included — BEFORE the load the scenario will
  // assert on. Without this, one viewport's writes (e.g. theme=light) leak into
  // the next scenario/viewport: the app boots with the previous choice, picking
  // the same value produces an empty store diff, and no POST is sent at all.
  await mockPost(page, '/__mock/config', { seed, stream_fixture: fixture, reset_settings: true });
  await mockPost(page, '/__mock/reset', {});
  // The throwaway load also wrote appearance keys to localStorage; boot prefers
  // localStorage over the server, so they must go too or the leak survives the
  // settings reset.
  await page.evaluate(() => { try { localStorage.clear(); sessionStorage.clear(); } catch (_) {} });
  // Definitive load against the freshly seeded backend.
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => typeof S !== 'undefined' && S._bootReady === true, null, { timeout: 20000 });
  if (sessionId) {
    await page.evaluate((s) => { localStorage.setItem('hermes-webui-session', s); }, sessionId);
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => typeof S !== 'undefined' && S._bootReady === true, null, { timeout: 20000 });
  }
  await page.waitForTimeout(250);
}

async function send(page, text) {
  // Touch layouts treat Enter as "newline" (send_key applies to desktop), so the
  // real send affordance there is the button. Detect it instead of hardcoding.
  const touch = await page.evaluate(() => !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches));
  await page.click('#msg');
  await page.type('#msg', text, { delay: 2 });
  if (touch) {
    await page.click('#btnSend');
    return;
  }
  await page.keyboard.press('Enter');
  // Enter may be remapped by settings; fall back to the button once.
  const started = await page.waitForFunction(
    () => (typeof S !== 'undefined' && S.busy) || (document.getElementById('msgInner') || {}).childElementCount > 0,
    null, { timeout: 4000 },
  ).then(() => true).catch(() => false);
  if (!started) await page.click('#btnSend');
}

/** A real drag/wheel gesture over the transcript, matching the input device. */
async function dragTranscript(page, dy) {
  const touch = await page.evaluate(() => !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches));
  const box = await page.evaluate(() => {
    const el = document.getElementById('messages');
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
  });
  if (!touch) {
    await page.mouse.move(box.x, box.y);
    await page.mouse.wheel(0, dy);
    return;
  }
  const client = await page.context().newCDPSession(page);
  const startY = box.y + Math.round(box.h / 3);
  await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x, y: startY }] });
  const steps = 6;
  for (let i = 1; i <= steps; i++) {
    await client.send('Input.dispatchTouchEvent', {
      type: 'touchMove', touchPoints: [{ x: box.x, y: startY - Math.round((dy / steps) * i) }],
    });
    await sleep(16);
  }
  await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await client.detach().catch(() => {});
}

/** Wait until the transcript stops growing (settled), or timeout. */
async function waitSettled(page, timeout = 25000) {
  const started = Date.now();
  let last = -1, stable = 0;
  while (Date.now() - started < timeout) {
    const n = await page.evaluate(() => {
      const root = document.getElementById('msgInner');
      return root ? (root.innerText || '').length : 0;
    });
    if (n === last) stable += 1; else stable = 0;
    last = n;
    if (stable >= 6 && n > 0) return true;
    await sleep(250);
  }
  return false;
}

const transcript = (page) => page.evaluate(() => {
  const root = document.getElementById('msgInner');
  if (!root) return { len: 0, rows: 0, turns: 0, text: '' };
  return {
    len: (root.innerText || '').length,
    rows: root.querySelectorAll('.msg-row').length,
    turns: root.querySelectorAll('.assistant-turn').length,
    text: (root.innerText || '').replace(/\s+/g, ' ').slice(0, 200),
  };
});

const scrollBox = (page, sel = '#messages') => page.evaluate((s) => {
  const el = document.querySelector(s);
  if (!el) return null;
  return {
    top: Math.round(el.scrollTop),
    height: el.scrollHeight,
    client: el.clientHeight,
    atBottom: el.scrollHeight - el.scrollTop - el.clientHeight <= 8,
    width: el.clientWidth,
    scrollWidth: el.scrollWidth,
  };
}, sel);

const composerBox = (page) => page.evaluate(() => {
  const el = document.getElementById('composerWrap');
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const input = document.getElementById('msg');
  // Which elements actually stick out of the viewport — makes a page-level
  // overflow report actionable instead of just "scrollWidth is 14 too big".
  const offenders = [];
  for (const node of document.querySelectorAll('body *')) {
    const b = node.getBoundingClientRect();
    if (b.width > 0 && b.right > window.innerWidth + 1) {
      offenders.push(`${node.tagName.toLowerCase()}${node.id ? '#' + node.id : ''}.${(typeof node.className === 'string' ? node.className : '').split(/\s+/)[0]}@${Math.round(b.right)}`);
      if (offenders.length >= 4) break;
    }
  }
  return {
    top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height),
    left: Math.round(r.left), right: Math.round(r.right),
    vh: window.innerHeight, vw: window.innerWidth,
    docScrollW: document.documentElement.scrollWidth,
    fontPx: input ? parseFloat(getComputedStyle(input).fontSize) || 0 : 0,
    inputVisible: input ? input.getBoundingClientRect().height > 8 : false,
    offenders,
  };
});

/** Click the titlebar mode button, or invoke what it calls when the mobile
 *  layout hides it (same handler, no duplicated mode logic). */
async function cycleMode(page) {
  const clickable = await page.evaluate(() => {
    const b = document.getElementById('btnTitlebarActivityMode');
    if (!b) return false;
    const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(b).visibility !== 'hidden';
  });
  if (clickable) {
    await page.click('#btnTitlebarActivityMode', { timeout: 5000 }).catch(async () => {
      await page.evaluate(() => { if (typeof _cycleChatActivityDisplayMode === 'function') _cycleChatActivityDisplayMode(); });
    });
    return;
  }
  await page.evaluate(() => { if (typeof _cycleChatActivityDisplayMode === 'function') _cycleChatActivityDisplayMode(); });
}

const modeButton = (page) => page.evaluate(() => {
  const b = document.getElementById('btnTitlebarActivityMode');
  if (!b) return null;
  const r = b.getBoundingClientRect();
  return {
    dataMode: b.getAttribute('data-mode'),
    label: (document.getElementById('btnTitlebarActivityModeLabel') || {}).textContent || '',
    visible: r.width > 0 && r.height > 0,
    mode: window._chatActivityDisplayMode,
  };
});

// ------------------------------------------------------------ scenarios ----
const SCENARIOS = [];

/**
 * 1. Live quiet -> full switch. Contract: switching is immediate, opens no new
 *    SSE, and does not destroy the data being streamed.
 */
SCENARIOS.push({
  name: 'modes_live_quiet_to_full',
  async run(page, checks) {
    await boot(page, { seed: false, fixture: 'stream_long.json' });
    const startMode = await modeButton(page);
    record(checks, 'boots in a canonical activity mode',
      startMode && ['stream_full', 'stream_window', 'stream_quiet'].includes(startMode.dataMode),
      JSON.stringify(startMode));

    await send(page, 'Расскажи про режимы');
    const grew = await page.waitForFunction(() => {
      const root = document.getElementById('msgInner');
      return root && (root.innerText || '').length > 60;
    }, null, { timeout: 20000 }).then(() => true).catch(() => false);
    record(checks, 'live turn streams text into the transcript', grew);

    const before = await transcript(page);
    const sseBefore = chatSse(await mockState(page));

    const t0 = Date.now();
    await cycleMode(page);
    const applied = await page.waitForFunction(
      () => window._chatActivityDisplayMode === 'stream_full' ||
            window._chatActivityDisplayMode === 'stream_window' ||
            window._chatActivityDisplayMode === 'stream_quiet', null, { timeout: 3000 }
    ).then(() => true).catch(() => false);
    const latency = Date.now() - t0;
    const midMode = await modeButton(page);
    record(checks, 'mode switch applies without reload', applied && latency < 3000, `latency=${latency}ms`);
    record(checks, 'mode switch changes data-mode immediately',
      midMode && midMode.dataMode === midMode.mode, JSON.stringify(midMode));

    await sleep(400);
    const afterSwitch = await transcript(page);
    const sseAfter = chatSse(await mockState(page));
    record(checks, 'mode switch opens no new chat SSE', sseAfter === sseBefore,
      `before=${sseBefore} after=${sseAfter}`);
    record(checks, 'mode switch keeps the streamed text',
      afterSwitch.len >= before.len - 2,
      `before=${before.len} after=${afterSwitch.len} text="${afterSwitch.text.slice(0, 80)}"`);
    record(checks, 'mode switch keeps the assistant turn mounted',
      afterSwitch.turns >= 1, `turns=${afterSwitch.turns} rows=${afterSwitch.rows}`);

    const grewAfter = await page.waitForFunction((n) => {
      const root = document.getElementById('msgInner');
      return root && (root.innerText || '').length > n + 40;
    }, afterSwitch.len, { timeout: 8000 }).then(() => true).catch(() => false);
    record(checks, 'stream continues after the switch', grewAfter);

    const settled = await waitSettled(page);
    record(checks, 'turn settles after the switch', settled);
    const final = await transcript(page);
    record(checks, 'final answer survives the mode switch', final.len >= before.len, `len=${final.len}`);
    const sseFinal = chatSse(await mockState(page));
    record(checks, 'whole turn used a single chat SSE', sseFinal === sseBefore,
      `before=${sseBefore} final=${sseFinal}`);
  },
});

/**
 * 2. Settled transcript: cycle every mode. Contract: data is never destroyed by
 *    hiding it, disclosure stays stable, and no SSE is opened.
 */
SCENARIOS.push({
  name: 'modes_settled_cycle',
  async run(page, checks) {
    await boot(page, { seed: true, sessionId: 'fx-session-long' });
    const base = await transcript(page);
    record(checks, 'fixture transcript rendered', base.rows >= 2 && base.len > 800,
      JSON.stringify(base).slice(0, 160));

    const sse0 = chatSse(await mockState(page));
    const seen = [];
    for (let i = 0; i < 3; i++) {
      await cycleMode(page);
      await page.waitForTimeout(500);
      const st = await transcript(page);
      const mb = await modeButton(page);
      seen.push({ mode: mb && mb.mode, len: st.len, rows: st.rows });
      record(checks, `cycle ${i + 1}: transcript preserved (${mb && mb.mode})`, st.len >= base.len - 2,
        `len ${base.len} -> ${st.len}`);
      record(checks, `cycle ${i + 1}: no new SSE`, chatSse(await mockState(page)) === sse0);
    }
    record(checks, 'all three modes reachable by cycling',
      new Set(seen.map((s) => s.mode)).size === 3, JSON.stringify(seen));

    const artifacts = await page.evaluate(() => {
      const root = document.getElementById('msgInner');
      return {
        codeBlocks: root.querySelectorAll('pre code').length,
        tableRows: root.querySelectorAll('table tbody tr').length,
      };
    });
    record(checks, 'code block survives mode cycling', artifacts.codeBlocks >= 1, JSON.stringify(artifacts));
    record(checks, 'table survives mode cycling', artifacts.tableRows >= 3, JSON.stringify(artifacts));
  },
});

/**
 * 3. Appearance: theme persists through the Settings UI, and the titlebar mode
 *    button writes ONLY its own key (single source of truth).
 */
SCENARIOS.push({
  name: 'settings_theme_persist',
  async run(page, checks) {
    await boot(page, { seed: true, sessionId: 'fx-session-long' });
    // Negative control: the seeded backend says theme=dark, so the page must be
    // painting the dark palette. Captured here so "light survived the reload" is
    // measured as a palette change against a distinct, real dark baseline —
    // not as the absence/presence of a class token.
    const darkCtl = await themeProbe(page);
    record(checks, 'seeded theme paints the dark palette (control)',
      darkCtl.ls === 'dark' && darkCtl.htmlDark === true && !!darkCtl.bg,
      JSON.stringify(darkCtl));
    // Open Settings through its real sidebar entry, then the Appearance tab, so
    // the scenario exercises the same path a user takes (and the same hydration
    // order the upgrade touches).
    await page.evaluate(async () => {
      if (typeof switchPanel === 'function') await switchPanel('settings');
    });
    await page.waitForTimeout(400);
    // On a phone the section list lives in the slide-in drawer: the real first
    // step is tapping the hamburger. Detect the affordance instead of branching
    // on the viewport name, so a layout change cannot silently skip it (which is
    // how the mobile run used to click an off-screen element and post nothing).
    const drawer = await page.evaluate(() => {
      const box = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 ? r : null;
      };
      const sb = document.querySelector('.sidebar');
      const hb = document.getElementById('btnHamburger');
      return {
        closed: !!box(sb) && sb.getBoundingClientRect().right <= 1,
        hamburger: !!box(hb) && getComputedStyle(hb).display !== 'none',
      };
    });
    if (drawer.hamburger && drawer.closed) {
      await page.click('#btnHamburger', { timeout: 5000 });
      await page.waitForFunction(() => {
        const sb = document.querySelector('.sidebar');
        if (!sb) return false;
        const r = sb.getBoundingClientRect();
        return r.left >= -1 && r.width > 100;
      }, null, { timeout: 5000 });
    }
    // Desktop shows the section list; a compact frame may show a dropdown. Use
    // whichever control is actually on screen (in-viewport, not merely in the
    // DOM), then fall back to the function that control calls.
    const nav = await page.evaluate(() => {
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.right > 0 && r.left < window.innerWidth
          && getComputedStyle(el).visibility !== 'hidden';
      };
      return {
        tab: visible(document.querySelector('[data-settings-section="appearance"]')),
        dropdown: visible(document.getElementById('settingsSectionDropdown')),
      };
    });
    record(checks, 'settings offers an Appearance entry point', nav.tab || nav.dropdown, JSON.stringify(nav));
    if (nav.tab) {
      await page.click('[data-settings-section="appearance"]', { timeout: 5000 }).catch(() => {});
    } else if (nav.dropdown) {
      await page.selectOption('#settingsSectionDropdown', 'appearance', { timeout: 5000 }).catch(() => {});
    } else {
      await page.evaluate(() => { if (typeof switchSettingsSection === 'function') switchSettingsSection('appearance', { skipLazyLoad: true }); });
    }
    await page.waitForTimeout(500);
    // The picker must be on screen and the pane active — a hidden pane with a
    // present-but-zero-sized button is not a usable entry point.
    const pickerState = await page.evaluate(() => {
      const pane = document.getElementById('settingsPaneAppearance');
      const pick = document.querySelector('[data-theme-val="light"]');
      const geo = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.left), y: Math.round(r.top) };
      };
      return {
        paneActive: !!pane && pane.classList.contains('active'),
        pane: geo(pane),
        picker: geo(pick),
        section: typeof _currentSettingsSection === 'undefined' ? null : _currentSettingsSection,
      };
    });
    const vpSize = page.viewportSize() || { width: 0, height: 0 };
    const pickerReady = pickerState.paneActive
      && !!pickerState.pane && pickerState.pane.w > 0 && pickerState.pane.h > 0
      && !!pickerState.picker && pickerState.picker.w > 0 && pickerState.picker.h > 0
      // Actually on screen: the row a user can tap without hunting for it.
      && pickerState.picker.y >= 0 && pickerState.picker.y < vpSize.height
      && pickerState.picker.x >= 0 && pickerState.picker.x < vpSize.width;
    record(checks, 'appearance pane exposes the theme picker', pickerReady, JSON.stringify(pickerState));
    if (pickerReady) {
      await mockPost(page, '/__mock/reset', {});
      // A real click only: falling back to _pickTheme() would prove nothing about
      // the UI the user actually operates.
      let clickError = '';
      await page.click('[data-theme-val="light"]', { timeout: 8000 })
        .catch((e) => { clickError = String((e && e.message) || e).split('\n')[0]; });
      await page.waitForTimeout(1400);
      const st = await mockState(page);
      const posts = st.settings_posts || [];
      const withTheme = posts.filter((p) => p && p.theme === 'light');
      record(checks, 'picking a theme posts it to /api/settings', withTheme.length >= 1,
        JSON.stringify({ posted: posts.map((p) => p.theme), serverTheme: (st.settings || {}).theme, clickError }));
      record(checks, 'theme is mirrored to localStorage',
        (await page.evaluate(() => localStorage.getItem('hermes-theme'))) === 'light');
      // The choice must reach the page, not just the store: light palette,
      // `.dark` gone, picker marked active.
      const picked = await themeProbe(page);
      record(checks, 'picking light repaints with the light palette',
        picked.htmlDark === false && !!picked.bg && picked.bg !== darkCtl.bg && picked.pickerActive === true,
        JSON.stringify({ picked, darkBg: darkCtl.bg }));
      const known = new Set([
        'theme', 'skin', 'font_size', 'chat_activity_display_mode',
        'transparent_stream_event_timestamps', 'session_jump_buttons', 'session_endless_scroll',
        'auto_scroll_follow', 'render_user_markdown', 'large_text_paste_as_attachment',
        'project_quick_create_buttons', 'show_titlebar_profile', 'worklog_details_expanded_default',
        'activity_feed_expanded_default', 'thinking_mode', 'show_tool_calls',
        'auto_collapse_previous_turns', 'composer_control_order', 'hidden_tabs', 'tab_order',
        'structured_code_auto_tree_lines', 'structured_code_collapsed_default', 'structured_code_view',
        'structured_code_default_view',
      ]);
      const unrelated = withTheme.length
        ? Object.keys(withTheme[0]).filter((k) => !known.has(k) && !k.startsWith('hide_composer_'))
        : [];
      record(checks, 'appearance save sends only appearance keys', unrelated.length === 0,
        `unexpected keys: ${unrelated.join(',')}`);

      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => typeof S !== 'undefined' && S._bootReady === true, null, { timeout: 20000 });
      // The theme is applied after the settings hydration round-trip, so give the
      // boot sequence a bounded moment to reach the stored palette.
      await page.waitForFunction(
        (bg) => getComputedStyle(document.documentElement).getPropertyValue('--bg').trim() === bg,
        picked.bg, { timeout: 8000 },
      ).catch(() => {});
      const afterReload = await themeProbe(page);
      // Strict: the stored choice is light, the page is NOT dark, and the painted
      // palette is byte-identical to the light palette captured before the reload
      // (and therefore different from the dark control). A theme that reverted
      // to dark fails all three.
      record(checks, 'theme survives a reload',
        afterReload.ls === 'light'
          && afterReload.htmlDark === false
          && !!afterReload.bg
          && afterReload.bg === picked.bg
          && afterReload.bg !== darkCtl.bg,
        JSON.stringify({ beforeReload: picked, afterReload, darkControlBg: darkCtl.bg }));
    }

    await mockPost(page, '/__mock/reset', {});
    await page.evaluate(() => { if (typeof switchPanel === 'function') switchPanel('chat'); });
    await page.waitForTimeout(250);
    await cycleMode(page);
    await page.waitForTimeout(1400);
    const st2 = await mockState(page);
    const posts2 = st2.settings_posts || [];
    const keys = posts2.length ? Object.keys(posts2[posts2.length - 1]).sort() : [];
    record(checks, 'header mode button saves only its own key',
      keys.length === 1 && keys[0] === 'chat_activity_display_mode',
      `${keys.length} keys: ${keys.slice(0, 8).join(',')}${keys.length > 8 ? ',…' : ''}`);
  },
});

/**
 * 4. Reading scroll: a user gesture wins over follow/RO/timers, the live DOM is
 *    not rebuilt, and returning to the bottom re-enables following.
 */
SCENARIOS.push({
  name: 'stream_reading_scroll',
  async run(page, checks) {
    await boot(page, { seed: false, fixture: 'stream_long.json' });
    await send(page, 'Расскажи про режимы');
    const overflowed = await page.waitForFunction(() => {
      const el = document.getElementById('messages');
      return el && el.scrollHeight > el.clientHeight + 120;
    }, null, { timeout: 25000 }).then(() => true).catch(() => false);
    record(checks, 'stream fills the transcript past one viewport', overflowed);

    const marked = await page.evaluate(() => {
      const turn = document.querySelector('#msgInner .assistant-turn');
      if (!turn) return false;
      turn.dataset.upgradeProbe = 'live';
      return true;
    });
    record(checks, 'live assistant turn present before the gesture', marked);

    // A real upward gesture over the transcript: wheel on desktop, touch drag on
    // the mobile frame. Both must beat follow/RO/timer driven scrolling.
    await dragTranscript(page, -520);
    await page.waitForTimeout(500);
    const readPos = await scrollBox(page);
    await page.waitForTimeout(1400);
    const readPos2 = await scrollBox(page);
    record(checks, 'reading position is not yanked by incoming tokens',
      readPos && readPos2 && Math.abs(readPos2.top - readPos.top) <= 4,
      `top ${readPos && readPos.top} -> ${readPos2 && readPos2.top} (height ${readPos && readPos.height} -> ${readPos2 && readPos2.height})`);
    record(checks, 'user gesture left the viewport above the bottom', readPos && !readPos.atBottom,
      JSON.stringify(readPos));

    const sameNode = await page.evaluate(() => !!document.querySelector('#msgInner .assistant-turn[data-upgrade-probe="live"]'));
    record(checks, 'streaming DOM is not rebuilt during the read', sameNode);

    await page.evaluate(() => { const el = document.getElementById('messages'); el.scrollTop = el.scrollHeight; });
    await dragTranscript(page, 40);
    await page.waitForTimeout(400);
    await waitSettled(page);
    // After settling, an explicit return to the bottom must keep the newest
    // content in view; if follow is on it must stay pinned while tokens arrive.
    await page.evaluate(() => { const el = document.getElementById('messages'); el.scrollTop = el.scrollHeight; });
    await page.waitForTimeout(400);
    const end = await scrollBox(page);
    record(checks, 'returning to the bottom shows the newest content',
      end && (end.atBottom || end.top + end.client >= end.height - 24), JSON.stringify(end));
    const final = await transcript(page);
    record(checks, 'transcript is not cleared by the scroll cycle', final.len > 200, `len=${final.len}`);
  },
});

/**
 * 5. Completion + resize + composer geometry (incl. the mobile keyboard inset).
 */
SCENARIOS.push({
  name: 'completion_resize_composer_geometry',
  async run(page, checks) {
    await boot(page, { seed: false, fixture: 'stream_basic.json' });
    const before = await composerBox(page);
    record(checks, 'composer starts anchored to the viewport bottom',
      before && before.vh - before.bottom <= 2, JSON.stringify(before));
    record(checks, 'composer does not overflow horizontally',
      before && before.right <= before.vw + 1 && before.left >= -1, JSON.stringify(before));

    await send(page, 'Короткий вопрос');
    await waitSettled(page);
    const after = await composerBox(page);
    record(checks, 'composer stays anchored after completion',
      after && after.vh - after.bottom <= 2, JSON.stringify(after));
    record(checks, 'composer keeps a sane height on completion',
      after && after.height >= 40 && after.height <= Math.max(280, (before ? before.height : 120) + 140),
      `before=${before && before.height} after=${after && after.height}`);

    // Desktop resize: everything must stay inside the new viewport.
    await page.setViewportSize({ width: 1024, height: 720 });
    await page.waitForTimeout(500);
    const small = await composerBox(page);
    record(checks, 'composer stays anchored after a desktop resize',
      small && small.vh - small.bottom <= 2, JSON.stringify(small));
    record(checks, 'desktop resize introduces no horizontal page scroll',
      small && small.docScrollW <= small.vw + 1,
      `docScrollW=${small && small.docScrollW} vw=${small && small.vw} offenders=${JSON.stringify(small && small.offenders)}`);
    const scrolled = await scrollBox(page);
    record(checks, 'transcript still scrollable after resize',
      scrolled && scrolled.client > 100, JSON.stringify(scrolled));

    // Mobile reference frame 393x851: the composer must sit on the bottom edge,
    // stay inside the viewport and keep the input usable (16px stops iOS zoom).
    await page.setViewportSize({ width: 393, height: 851 });
    await page.waitForTimeout(500);
    const mobile = await composerBox(page);
    record(checks, 'mobile composer sits on the bottom edge',
      mobile && mobile.vh - mobile.bottom <= 2, JSON.stringify(mobile));
    record(checks, 'mobile composer input is visible', mobile && mobile.inputVisible);
    record(checks, 'mobile input font-size avoids iOS zoom (>=16px)',
      mobile && mobile.fontPx >= 16, `font=${mobile && mobile.fontPx}px`);
    record(checks, 'mobile composer does not overflow the viewport',
      mobile && mobile.right <= mobile.vw + 1 && mobile.docScrollW <= mobile.vw + 1,
      JSON.stringify(mobile));

    // Soft keyboard: the visual viewport shrinks while the composer keeps the
    // input visible above it. Approximate the keyboard by shrinking the
    // viewport height with the input focused.
    await page.click('#msg');
    await page.setViewportSize({ width: 393, height: 520 });
    await page.waitForTimeout(500);
    const keyboard = await composerBox(page);
    record(checks, 'composer stays inside the viewport with the keyboard inset',
      keyboard && keyboard.vh - keyboard.bottom <= 2 && keyboard.top >= 0, JSON.stringify(keyboard));
    record(checks, 'input remains visible above the keyboard inset',
      keyboard && keyboard.inputVisible, JSON.stringify(keyboard));
    record(checks, 'keyboard inset opens no horizontal scroll',
      keyboard && keyboard.docScrollW <= keyboard.vw + 1,
      `docScrollW=${keyboard && keyboard.docScrollW} vw=${keyboard && keyboard.vw}`);
    const kbScroll = await scrollBox(page);
    record(checks, 'transcript keeps a usable height above the keyboard',
      kbScroll && kbScroll.client > 80, JSON.stringify(kbScroll));
  },
});

/**
 * 6. Long code + wide table: nothing may push the page horizontally, the code
 *    block scrolls inside itself, and the table keeps all its rows.
 */
SCENARIOS.push({
  name: 'long_code_and_table',
  async run(page, checks) {
    await boot(page, { seed: true, sessionId: 'fx-session-long' });
    const art = await page.evaluate(() => {
      const root = document.getElementById('msgInner');
      const pre = root.querySelector('pre');
      const table = root.querySelector('table');
      const scroller = root.querySelector('.msg-body, .assistant-segment');
      // A wide artifact is fine as long as it scrolls inside its own box; what
      // must never happen is the artifact widening the page itself.
      const scrollable = (el) => {
        if (!el) return false;
        const cs = getComputedStyle(el);
        return (cs.overflowX === 'auto' || cs.overflowX === 'scroll') && el.scrollWidth > el.clientWidth;
      };
      const tableBox = table ? (table.closest('.markdown-table-wrap, .table-wrap, .markdown-table') || table) : null;
      return {
        codeBlocks: root.querySelectorAll('pre code').length,
        codeLines: pre ? (pre.innerText || '').split('\n').length : 0,
        preOverflowX: pre ? getComputedStyle(pre).overflowX : null,
        preFits: pre ? (pre.scrollWidth <= pre.clientWidth + 2 || scrollable(pre) || scrollable(pre && pre.parentElement)) : false,
        tableRows: root.querySelectorAll('table tbody tr').length,
        tableCols: root.querySelectorAll('table thead th').length,
        // Containment reference is the transcript viewport, not the text column:
        // a table may legitimately use the body's side padding.
        tableFits: table ? (() => {
          const box = document.getElementById('messages');
          if (!box) return false;
          const limit = box.getBoundingClientRect().right + 1;
          return table.getBoundingClientRect().right <= limit || scrollable(tableBox) || scrollable(table.parentElement);
        })() : false,
        tableW: table ? Math.round(table.getBoundingClientRect().width) : 0,
        colW: scroller ? Math.round(scroller.getBoundingClientRect().width) : 0,
        tableScrollW: table ? table.scrollWidth : 0,
        tableWidensPage: table ? table.getBoundingClientRect().right > window.innerWidth + 1 : false,
        // Artifact containment is judged on the transcript scroller: the page
        // shell's own overflow (side panels) is a different owner's surface and
        // is asserted in the geometry scenario.
        transcriptScrollW: (() => { const el = document.getElementById('messages'); return el ? el.scrollWidth : 0; })(),
        transcriptClientW: (() => { const el = document.getElementById('messages'); return el ? el.clientWidth : 0; })(),
        docScrollW: document.documentElement.scrollWidth,
        vw: window.innerWidth,
        bodyScrollW: document.body.scrollWidth,
      };
    });
    record(checks, 'long code block rendered', art.codeBlocks >= 1 && art.codeLines >= 20, JSON.stringify(art).slice(0, 220));
    record(checks, 'code block scrolls inside its box instead of overflowing', art.preFits,
      `preFits=${art.preFits} overflowX=${art.preOverflowX}`);
    record(checks, 'wide table keeps all rows and columns',
      art.tableRows >= 3 && art.tableCols >= 3, `rows=${art.tableRows} cols=${art.tableCols}`);
    record(checks, 'table stays inside the transcript column', art.tableFits && !art.tableWidensPage,
      `fits=${art.tableFits} tableW=${art.tableW} colW=${art.colW} tableScrollW=${art.tableScrollW} widensPage=${art.tableWidensPage}`);
    record(checks, 'long artifacts do not widen the transcript',
      art.transcriptScrollW <= art.transcriptClientW + 1,
      `scrollW=${art.transcriptScrollW} clientW=${art.transcriptClientW}`);
  },
});

// ---------------------------------------------------------------- main -----
// Known failures: a checked-in list of defects that already reproduce on the
// pre-merge tree. They stay visible in the report but do not fail the run, so
// the suite is green exactly when no NEW regression appears. An entry that
// starts passing is reported as FIXED and should be deleted from the list.
function loadBaseline(file) {
  if (!file) return [];
  const raw = JSON.parse(fs.readFileSync(path.resolve(file), 'utf8'));
  return Array.isArray(raw) ? raw : (raw.known || []);
}

function classify(res, known) {
  for (const c of res.checks) {
    const hit = known.find((e) => e.check === c.name && e.scenario === res.name
      && (!e.viewports || e.viewports.includes(res.viewport)));
    if (!hit) continue;
    if (c.ok) c.baselineFixed = true;
    else { c.known = true; c.reason = hit.reason || ''; }
  }
  res.newFailures = res.checks.filter((c) => !c.ok && !c.known);
  res.knownFailures = res.checks.filter((c) => !c.ok && c.known);
  res.fixed = res.checks.filter((c) => c.baselineFixed);
  res.ok = !res.error && res.newFailures.length === 0;
  return res;
}

async function runOne(browser, scenario, viewportName, known = []) {
  const vp = VIEWPORTS[viewportName];
  const context = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
    isMobile: !!vp.isMobile,
    hasTouch: !!vp.hasTouch,
    deviceScaleFactor: vp.deviceScaleFactor || 1,
    baseURL: BASE,
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
    serviceWorkers: 'block',
  });
  const page = await context.newPage();
  const checks = [];
  const pageErrors = [];
  const http5xx = [];
  const external = [];
  page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 300)));
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const text = m.text();
    // "Failed to load resource" is the browser's own network log line; real
    // 5xx responses are tracked separately below, so it must not double-report.
    if (/Failed to load resource/i.test(text)) return;
    pageErrors.push(`console: ${text.slice(0, 200)}`);
  });
  page.on('response', (r) => {
    if (r.status() >= 500) http5xx.push(`${r.status()} ${r.url().replace(BASE, '')}`);
  });
  page.on('request', (r) => {
    const u = r.url();
    if (!u.startsWith(BASE) && !u.startsWith('data:') && !u.startsWith('blob:') && !u.startsWith('about:')) {
      external.push(u.slice(0, 160));
    }
  });
  // Hard stop: nothing outside the mock origin may be reached, so a stray
  // provider call can never leave the machine during a regression run.
  await page.route('**/*', (route) => {
    const u = route.request().url();
    if (u.startsWith(BASE) || u.startsWith('data:') || u.startsWith('blob:') || u.startsWith('about:')) {
      return route.continue();
    }
    return route.abort();
  });
  let error = '';
  try {
    await scenario.run(page, checks);
  } catch (e) {
    error = String((e && e.stack) || e).slice(0, 800);
  }
  record(checks, 'no uncaught page errors', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  record(checks, 'no 5xx responses from the mock backend', http5xx.length === 0, http5xx.slice(0, 3).join(' | '));
  record(checks, 'no external network requests (no LLM calls)', external.length === 0,
    external.slice(0, 3).join(' | '));
  await context.close().catch(() => {});
  const ok = !error && checks.every((c) => c.ok);
  return classify({ name: scenario.name, viewport: viewportName, ok, error, checks }, known);
}

async function main() {
  if (args.list) {
    for (const s of SCENARIOS) console.log(s.name);
    return 0;
  }
  if (!BASE) {
    console.error('SETUP FAIL: --base-url (or UPGRADE_BASE_URL) is required');
    return 2;
  }
  const { chromium } = loadPlaywright();
  const exe = resolveChromium();
  const only = args.only ? String(args.only).split(',').map((s) => s.trim()).filter(Boolean) : null;
  const vpNames = (args.viewports ? String(args.viewports) : 'desktop,mobile')
    .split(',').map((s) => s.trim()).filter((n) => VIEWPORTS[n]);
  const scenarios = only ? SCENARIOS.filter((s) => only.includes(s.name)) : SCENARIOS;
  if (!scenarios.length) {
    console.error(`SETUP FAIL: no scenario matched ${only}`);
    return 2;
  }

  const known = loadBaseline(args.baseline);
  if (args.baseline) console.log(`[runner] baseline: ${path.resolve(String(args.baseline))} (${known.length} known)`);
  const browser = await chromium.launch({
    executablePath: exe,
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--force-device-scale-factor=1'],
  });
  const results = [];
  try {
    for (const vpName of vpNames) {
      for (const scenario of scenarios) {
        const res = await runOne(browser, scenario, vpName, known);
        results.push(res);
        const failed = res.checks.filter((c) => !c.ok);
        console.log(`${res.ok ? 'PASS' : 'FAIL'} ${res.viewport.padEnd(7)} ${res.name}` +
          (failed.length ? ` — ${failed.length}/${res.checks.length} checks failed` : ''));
        for (const f of failed) {
          console.log(`   ${f.known ? '~ KNOWN' : '✗'} ${f.name}${f.detail ? ` :: ${f.detail}` : ''}`);
        }
        if (res.error) console.log(`   ! ${res.error.split('\n')[0]}`);
      }
    }
  } finally {
    await browser.close().catch(() => {});
  }

  const passed = results.filter((r) => r.ok).length;
  const report = {
    ok: passed === results.length,
    ran_at: new Date().toISOString(),
    base_url: BASE,
    chromium: exe,
    app_version_token: await Promise.resolve(null),
    totals: {
      scenarios: results.length,
      passed,
      failed: results.length - passed,
      checks: results.reduce((n, r) => n + r.checks.length, 0),
      checks_failed: results.reduce((n, r) => n + r.checks.filter((c) => !c.ok).length, 0),
      known_failures: results.reduce((n, r) => n + (r.knownFailures || []).length, 0),
      baseline_fixed: results.reduce((n, r) => n + (r.fixed || []).length, 0),
    },
    results,
  };
  const out = args.out || path.join(os.tmpdir(), 'upgrade-browser-report.json');
  fs.writeFileSync(out, JSON.stringify(report, null, 2));
  const knownTotal = report.totals.known_failures;
  console.log(`\n${passed}/${results.length} scenario runs passed; report: ${out}`);
  if (knownTotal) console.log(`known baseline failures: ${knownTotal} (documented, not counted as regressions)`);
  for (const r of results) for (const c of r.fixed || []) {
    console.log(`FIXED ${r.viewport}/${r.name}/${c.name} — baseline entry is stale, remove it`);
  }
  return report.ok ? 0 : 1;
}

main().then((code) => process.exit(code)).catch((e) => {
  console.error('SETUP FAIL:', (e && e.stack) || e);
  process.exit(2);
});

