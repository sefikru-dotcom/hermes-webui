#!/usr/bin/env node
// Focused probe: does a REAL touch scroll-up survive on a mobile viewport?
//
// The desktop wheel path already survives (measured). A phone differs in three
// ways that all sit on the scroll path:
//   1. the gesture arrives as touchstart/touchmove (not wheel),
//   2. the on-screen keyboard shrinks the viewport and fires visualViewport
//      resize, which calls _repinMessagesAfterComposerResize(),
//   3. virtualization rebuilds the window as rows leave/enter, which can write
//      scrollTop through _compensateScrollForMeasurementDelta().
//
// This drives genuine CDP touch input (Input.dispatchTouchEvent) and reports the
// position immediately after the gesture and 3 s later, plus every attributed
// scrollTop write that happened in between.

import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';

const TAG = '[touch-probe]';
const PORT = 8787;
const PROFILE = '/tmp/touch-probe-profile';

class CDP {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); this.handlers = new Map(); }
  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);
      this.ws.onopen = () => resolve(this);
      this.ws.onerror = (e) => reject(new Error('ws error'));
      this.ws.onmessage = (m) => {
        let msg; try { msg = JSON.parse(m.data); } catch { return; }
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve: res, reject: rej } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
        }
      };
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const httpJson = (path, { method = 'GET', body, cookie } = {}) => new Promise((resolve, reject) => {
  const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
  const req = http.request({
    host: '127.0.0.1', port: PORT, path, method,
    headers: {
      ...(data ? { 'Content-Type': 'application/json', 'Content-Length': data.length, Origin: `http://127.0.0.1:${PORT}` } : {}),
      ...(cookie ? { Cookie: cookie } : {}),
    },
  }, (res) => {
    let raw = '';
    res.on('data', (c) => { raw += c; });
    res.on('end', () => { let p = null; try { p = JSON.parse(raw); } catch {} resolve({ status: res.statusCode, headers: res.headers, body: p, raw }); });
  });
  req.on('error', reject);
  if (data) req.write(data);
  req.end();
});

async function main() {
  const envFile = fs.readFileSync('/opt/hermes-webui/.env', 'utf8');
  const pw = (envFile.match(/^HERMES_WEBUI_PASSWORD=(.*)$/m) || [])[1]?.trim();
  if (!pw) { console.log(`${TAG} no password`); process.exit(2); }

  const login = await httpJson('/api/auth/login', { method: 'POST', body: { password: pw } });
  if (login.status !== 200) { console.log(`${TAG} login ${login.status}`); process.exit(2); }
  const cookie = (login.headers['set-cookie'] || []).map((c) => String(c).split(';')[0]).join('; ');

  const list = await httpJson('/api/sessions', { cookie });
  const arr = (list.body && (list.body.sessions || list.body.data || list.body)) || [];
  const scored = (Array.isArray(arr) ? arr : []).map((s) => ({
    id: s.session_id || s.id,
    n: Number(s.message_count || s.messageCount || 0) || 0,
  })).filter((s) => s.id).sort((a, b) => b.n - a.n);
  const target = scored[0];
  if (!target) { console.log(`${TAG} no sessions`); process.exit(2); }

  const chrome = spawn('google-chrome', [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--no-sandbox', '--disable-dev-shm-usage', '--disable-setuid-sandbox',
    '--window-size=412,915', 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });

  const wsUrl = await new Promise((resolve, reject) => {
    let buf = '';
    const t = setTimeout(() => reject(new Error('chrome timeout')), 20000);
    chrome.stderr.on('data', (c) => {
      buf += c.toString();
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(t); resolve(m[1]); }
    });
  });

  const browser = await new CDP(wsUrl).connect();
  const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId: sid } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
  const S = (m, p = {}) => browser.send(m, p, sid);

  await S('Page.enable'); await S('Runtime.enable'); await S('Network.enable');
  // Phone geometry: POCO F5 is 393x873 CSS px at dpr 2.75.
  await S('Emulation.setDeviceMetricsOverride', { width: 393, height: 873, deviceScaleFactor: 2.75, mobile: true });
  await S('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  await S('Network.setCookie', { name: cookie.split('=')[0], value: cookie.split('=').slice(1).join('='), domain: '127.0.0.1', path: '/' });

  await S('Page.navigate', { url: `http://127.0.0.1:${PORT}/?session=${encodeURIComponent(target.id)}` });
  await sleep(5500);

  const ev = async (expr, awaitPromise = false) => {
    const r = await S('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
    if (r && r.exceptionDetails) return { __err: r.exceptionDetails.text };
    return r && r.result ? r.result.value : undefined;
  };

  const inst = await ev(`(function(){
    if(window.__tp) return 'already';
    const el=document.getElementById('messages'); if(!el) return 'no-pane';
    window.__tp={writes:[]};
    const d=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop');
    Object.defineProperty(Element.prototype,'scrollTop',{configurable:true,enumerable:d.enumerable,
      get(){return d.get.call(this);},
      set(v){ if(this&&this.id==='messages'){const b=d.get.call(this); d.set.call(this,v); const a=d.get.call(this);
        if(Math.abs(a-b)>=2) window.__tp.writes.push({t:Math.round(performance.now()),from:Math.round(b),to:Math.round(a),stack:(new Error().stack||'').split('\\n').slice(2,6).join(' | ')});
      } else d.set.call(this,v); }});
    return 'ok';
  })()`);
  console.log(`${TAG} instrument: ${JSON.stringify(inst)}`);

  const geo = () => ev(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),sh:el.scrollHeight,ch:el.clientHeight,dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight),unpinned:_messageUserUnpinned,pinned:_scrollPinned,rows:document.querySelectorAll('[data-msg-idx]').length};})()`);
  console.log(`${TAG} geometry: ${JSON.stringify(await geo())}`);

  // Genuine touch swipe: touchStart, N touchMoves upward, touchEnd.
  async function swipeUp(steps = 12, totalPx = 500, holdMs = 16) {
    const x = 196, yStart = 600;
    await S('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: yStart, id: 1 }] });
    for (let i = 1; i <= steps; i++) {
      const y = yStart - Math.round((totalPx * i) / steps);
      await S('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y, id: 1 }] });
      await sleep(holdMs);
    }
    await S('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  }

  const before = await geo();
  await swipeUp();
  await sleep(120);
  const after = await geo();
  await sleep(3000);
  const later = await geo();
  const wroteBack = Number.isFinite(after.top) && Number.isFinite(later.top) && (later.top - after.top) > 40;
  console.log(`${TAG} before=${JSON.stringify(before)}`);
  console.log(`${TAG} after swipe=${JSON.stringify(after)}`);
  console.log(`${TAG} after 3s=${JSON.stringify(later)}`);
  console.log(`${TAG} TOUCH_SCROLLED_UP=${after && before && after.top < before.top - 20 ? 'yes' : 'NO (gesture did not move the pane)'}`);
  console.log(`${TAG} UNPINNED=${after && after.unpinned === true ? 'yes' : 'NO'}`);
  console.log(`${TAG} SNAPPED_BACK_AFTER_3S=${wroteBack ? 'YES (reported bug)' : 'no'}`);

  const w = await ev('(window.__tp.writes||[]).slice(-12)');
  if (Array.isArray(w) && w.length) {
    console.log(`${TAG} writes:`);
    for (const x of w) console.log(`  t=${x.t} ${x.from}->${x.to} :: ${x.stack}`);
  } else console.log(`${TAG} writes: none`);

  browser.close();
  chrome.kill('SIGKILL');
  process.exit(wroteBack ? 1 : 0);
}

main().catch((e) => { console.log(`${TAG} error: ${e.message}`); process.exit(2); });
