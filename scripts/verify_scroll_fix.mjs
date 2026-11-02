#!/usr/bin/env node
// Verify the fix for the reported "chat springs back to the bottom" defect.
//
// The fix lives in _restorePinnedMessageScrollSnapshot (static/ui.js): a tail
// restore is now refused when it would move a pinned reader UP to a position the
// transcript has already grown past. That is the write that used to ping-pong
// against the live-follow writer and, after a reply, pull the reader back.
//
// This probe reproduces the two halves of the defect on a phone viewport:
//   A. load a long session -> the pane must settle at the tail, no oscillation.
//   B. seed a long conversation, read UP, then append a new reply and replay the
//      finalize path (scrollToBottom + settle) the way `done` does. With the fix,
//      the reader keeps their place and stays unpinned; without it, the restore
//      drags them to the tail.
//
// Exit 0 = contract holds.

import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';

const TAG = '[verify]';
const PORT = 8787;
const PROFILE = '/tmp/verify-probe-profile';

class CDP {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); }
  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);
      this.ws.onopen = () => resolve(this);
      this.ws.onerror = () => reject(new Error('ws error'));
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
  const login = await httpJson('/api/auth/login', { method: 'POST', body: { password: pw } });
  if (login.status !== 200) { console.log(`${TAG} login ${login.status}`); process.exit(2); }
  const cookie = (login.headers['set-cookie'] || []).map((c) => String(c).split(';')[0]).join('; ');

  const list = await httpJson('/api/sessions', { cookie });
  const arr = (list.body && (list.body.sessions || list.body.data || list.body)) || [];
  const target = (Array.isArray(arr) ? arr : []).map((s) => ({
    id: s.session_id || s.id, n: Number(s.message_count || s.messageCount || 0) || 0,
  })).filter((s) => s.id).sort((a, b) => b.n - a.n)[0];
  if (!target) { console.log(`${TAG} no sessions`); process.exit(2); }

  const chrome = spawn('google-chrome', [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--no-sandbox', '--disable-dev-shm-usage', '--disable-setuid-sandbox',
    '--window-size=393,873', 'about:blank',
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
  await S('Emulation.setDeviceMetricsOverride', { width: 393, height: 873, deviceScaleFactor: 2.75, mobile: true });
  await S('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  await S('Network.setCookie', { name: cookie.split('=')[0], value: cookie.split('=').slice(1).join('='), domain: '127.0.0.1', path: '/' });

  // Frame sampler + write attribution, installed before navigation.
  await S('Page.addScriptToEvaluateOnNewDocument', {
    source: `(function(){
      window.__v={writes:[],samples:[],on:false};
      function hook(){ const d=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop'); if(!d||!d.set||d.set.__h) return;
        const w=function(v){ if(this&&this.id==='messages'){const b=d.get.call(this); d.set.call(this,v); const a=d.get.call(this);
          if(Math.abs(a-b)>=2 && window.__v.writes.length<400) window.__v.writes.push({t:Math.round(performance.now()),from:Math.round(b),to:Math.round(a),stack:(new Error().stack||'').split('\\n').slice(2,6).join(' | ')});
        } else d.set.call(this,v); };
        w.__h=true; Object.defineProperty(Element.prototype,'scrollTop',{configurable:true,enumerable:d.enumerable,get(){return d.get.call(this);},set:w});
      }
      const iv=setInterval(()=>{ if(document.getElementById('messages')){hook();clearInterval(iv);} },50);
      (function loop(){ if(window.__v.on){ const el=document.getElementById('messages');
        if(el) window.__v.samples.push({t:Math.round(performance.now()),top:Math.round(el.scrollTop)}); } requestAnimationFrame(loop); })();
    })();`,
  });

  await S('Page.navigate', { url: `http://127.0.0.1:${PORT}/?session=${encodeURIComponent(target.id)}` });
  await sleep(2500);

  const ev = async (expr, awaitPromise = false) => {
    const r = await S('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
    if (r && r.exceptionDetails) return { __err: r.exceptionDetails.text };
    return r && r.result ? r.result.value : undefined;
  };
  const sample = async (label, ms) => {
    await ev('window.__v.samples=[];window.__v.on=true');
    await sleep(ms);
    const s = await ev('(window.__v.samples||[]).filter(x=>x.top>0)');
    await ev('window.__v.on=false');
    if (!Array.isArray(s) || !s.length) { console.log(`${TAG} ${label}: no samples`); return null; }
    const tops = s.map((x) => x.top);
    const min = Math.min(...tops), max = Math.max(...tops);
    let rev = 0, pd = 0;
    for (let i = 1; i < tops.length; i++) { const d = tops[i] - tops[i - 1]; const dir = d > 2 ? 1 : d < -2 ? -1 : 0; if (dir && pd && dir !== pd) rev++; if (dir) pd = dir; }
    console.log(`${TAG} ${label}: frames=${s.length} min=${min} max=${max} span=${max - min} reversals=${rev}`);
    return { min, max, span: max - min, rev, last: tops[tops.length - 1] };
  };

  const geo = () => ev(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),max:Math.round(el.scrollHeight-el.clientHeight),dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight),unpinned:_messageUserUnpinned,pinned:_scrollPinned};})()`);

  // A. load settles at the tail without fighting
  const loadA = await sample('A_load_idle', 2500);
  const g1 = await geo();
  const aOk = loadA && loadA.rev <= 2 && loadA.span <= 200 && g1 && g1.dist <= 2;
  console.log(`${TAG} A: geometry=${JSON.stringify(g1)} -> ${aOk ? 'PASS (settles at tail, no oscillation)' : 'FAIL'}`);

  // B. reader reads up, then a reply finalises and the tail-follow writer runs.
  async function dragDown(steps = 14, totalPx = 560, holdMs = 18) {
    const x = 196, yStart = 200;
    await S('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: yStart, id: 1 }] });
    for (let i = 1; i <= steps; i++) {
      await S('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: yStart + Math.round((totalPx * i) / steps), id: 1 }] });
      await sleep(holdMs);
    }
    await S('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  }
  await dragDown();
  await sleep(250);
  const readUp = await geo();
  console.log(`${TAG} B: after read-up ${JSON.stringify(readUp)}`);

  // Grow the transcript the way a reply does, then run the finalize path.
  await ev(`(()=>{
    const inner=document.getElementById('msgInner');
    if(inner){ const d=document.createElement('div'); d.style.height='900px'; d.setAttribute('data-verify-spacer','1'); inner.appendChild(d); }
    return inner?inner.childElementCount:0;
  })()`);
  await sleep(120);
  await ev(`(()=>{
    const shouldFollow=(typeof _shouldFollowMessagesOnDomReplace==='function')?_shouldFollowMessagesOnDomReplace():false;
    window.__v.shouldFollow=shouldFollow;
    renderMessages({preserveScroll:true});
    if(shouldFollow&&typeof scrollToBottom==='function') scrollToBottom();
    if(typeof _settleMessageScrollToBottom==='function') _settleMessageScrollToBottom(false,true);
    return shouldFollow;
  })()`);
  await sleep(400);
  const after1 = await geo();
  await sleep(2200);
  const after2 = await geo();
  const keptPlace = readUp && after2 && Math.abs(after2.top - readUp.top) <= 400 && after2.unpinned === true;
  const pulledToTail = after2 && after2.dist <= 2 && readUp && readUp.dist > 300;
  console.log(`${TAG} B: render+follow -> ${JSON.stringify(after1)} -> ${JSON.stringify(after2)}`);
  console.log(`${TAG} B: shouldFollowOnDone=${await ev('window.__v.shouldFollow')} PULLED_TO_TAIL=${pulledToTail ? 'YES (defect)' : 'no'} KEPT_PLACE=${keptPlace ? 'yes' : 'NO'}`);
  const bOk = !pulledToTail && keptPlace;

  // C. pinned reader: growth + follow must land exactly at the new tail.
  await ev(`(()=>{const el=document.getElementById('messages');el.scrollTop=el.scrollHeight;return 1;})()`);
  await ev(`(()=>{ if(typeof scrollToBottom==='function') scrollToBottom(); return 1; })()`);
  await sleep(300);
  const pinnedState = await geo();
  console.log(`${TAG} C: pinned geometry=${JSON.stringify(pinnedState)} ${pinnedState && pinnedState.dist <= 2 ? 'PASS' : 'FAIL (pinned reader not at tail)'}`);
  const cOk = pinnedState && pinnedState.dist <= 2;

  const writes = await ev('(window.__v.writes||[]).slice(-8)');
  if (Array.isArray(writes) && writes.length) {
    console.log(`${TAG} last writes:`);
    for (const w of writes) console.log(`  t=${w.t} ${w.from}->${w.to} :: ${w.stack}`);
  }

  const allOk = aOk && bOk && cOk;
  console.log(`${TAG} VERDICT=${allOk ? 'PASS' : 'FAIL'} (A=${aOk} B=${bOk} C=${cOk})`);
  browser.close();
  chrome.kill('SIGKILL');
  process.exit(allOk ? 0 : 1);
}

main().catch((e) => { console.log(`${TAG} error: ${e.message}`); process.exit(2); });
