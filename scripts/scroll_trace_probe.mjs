#!/usr/bin/env node
// Frame-accurate probe for the reported "chat springs back to the bottom" defect.
//
// Loads a long session on a PHONE viewport (393x873, dpr 2.75, touch enabled) and
// samples #messages.scrollTop at ~16 ms for several seconds, both at load and
// right after a genuine upward read gesture (finger moves DOWN, content moves
// down, viewport travels up the transcript — the direction the reader uses).
//
// Reports, per phase: min/max/span of scrollTop, number of direction reversals
// (an oscillation = two writers fighting), and the last attributed scrollTop
// writes with their stacks so the guilty call site is named.

import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';

const TAG = '[trace]';
const PORT = 8787;
const PROFILE = '/tmp/trace-probe-profile';

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
  if (!pw) { console.log(`${TAG} no password`); process.exit(2); }

  const login = await httpJson('/api/auth/login', { method: 'POST', body: { password: pw } });
  if (login.status !== 200) { console.log(`${TAG} login ${login.status}`); process.exit(2); }
  const cookie = (login.headers['set-cookie'] || []).map((c) => String(c).split(';')[0]).join('; ');

  const list = await httpJson('/api/sessions', { cookie });
  const arr = (list.body && (list.body.sessions || list.body.data || list.body)) || [];
  const target = (Array.isArray(arr) ? arr : []).map((s) => ({
    id: s.session_id || s.id, n: Number(s.message_count || s.messageCount || 0) || 0,
  })).filter((s) => s.id).sort((a, b) => b.n - a.n)[0];
  if (!target) { console.log(`${TAG} no sessions`); process.exit(2); }
  console.log(`${TAG} session ${target.id} (${target.n} msgs)`);

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

  // Recorder installed BEFORE navigation so it captures the very first load.
  await S('Page.addScriptToEvaluateOnNewDocument', {
    source: `
      (function(){
        window.__trace={writes:[], samples:[], on:false};
        function hookProto(){
          const d=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop');
          if(!d||!d.set||d.set.__hooked) return;
          const wrapped=function(v){
            if(this&&this.id==='messages'){
              const b=d.get.call(this); d.set.call(this,v); const a=d.get.call(this);
              if(Math.abs(a-b)>=2 && window.__trace.writes.length<800){
                window.__trace.writes.push({t:Math.round(performance.now()),from:Math.round(b),to:Math.round(a),stack:(new Error().stack||'').split('\\n').slice(2,7).join(' | ')});
              }
            } else d.set.call(this,v);
          };
          wrapped.__hooked=true;
          Object.defineProperty(Element.prototype,'scrollTop',{configurable:true,enumerable:d.enumerable,get(){return d.get.call(this);},set:wrapped});
        }
        const t0=setInterval(()=>{ if(document.getElementById('messages')){ hookProto(); clearInterval(t0);} },50);
        // frame sampler: run only while __trace.on
        (function loop(){
          if(window.__trace.on){
            const el=document.getElementById('messages');
            if(el) window.__trace.samples.push({t:Math.round(performance.now()),top:Math.round(el.scrollTop),sh:el.scrollHeight,ch:el.clientHeight,unpinned:(typeof _messageUserUnpinned!=='undefined')?_messageUserUnpinned:null,pinned:(typeof _scrollPinned!=='undefined')?_scrollPinned:null});
          }
          requestAnimationFrame(loop);
        })();
      })();
    `,
  });

  await S('Page.navigate', { url: `http://127.0.0.1:${PORT}/?session=${encodeURIComponent(target.id)}` });
  await sleep(1500);
  const ev = async (expr, awaitPromise = false) => {
    const r = await S('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
    if (r && r.exceptionDetails) return { __err: r.exceptionDetails.text };
    return r && r.result ? r.result.value : undefined;
  };

  const sample = async (label, ms) => {
    await ev('window.__trace.samples=[];window.__trace.on=true');
    await sleep(ms);
    const samples = await ev('(window.__trace.samples||[]).filter(s=>s.top>0)');
    await ev('window.__trace.on=false');
    if (!Array.isArray(samples) || !samples.length) { console.log(`${TAG} ${label}: no samples`); return null; }
    const tops = samples.map((s) => s.top);
    const min = Math.min(...tops), max = Math.max(...tops);
    let rev = 0, prevDir = 0;
    for (let i = 1; i < tops.length; i++) {
      const d = tops[i] - tops[i - 1];
      const dir = d > 2 ? 1 : d < -2 ? -1 : 0;
      if (dir && prevDir && dir !== prevDir) rev++;
      if (dir) prevDir = dir;
    }
    const last = samples[samples.length - 1];
    console.log(`${TAG} ${label}: frames=${samples.length} min=${min} max=${max} span=${max - min} reversals=${rev} last=${JSON.stringify({ top: last.top, sh: last.sh, ch: last.ch, unpinned: last.unpinned, pinned: last.pinned })}`);
    return { min, max, span: max - min, rev, last };
  };

  // Phase A: right after load (this is where the oscillation showed up).
  await sleep(1200);
  await sample('A_load_idle', 2500);

  const geo = () => ev(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),max:Math.round(el.scrollHeight-el.clientHeight),dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight),unpinned:_messageUserUnpinned,pinned:_scrollPinned,rows:document.querySelectorAll('[data-msg-idx]').length};})()`);
  console.log(`${TAG} geometry: ${JSON.stringify(await geo())}`);

  // Phase B: genuine upward read — finger travels DOWN the screen, so the
  // transcript scrolls up (towards older messages).
  async function dragDown(steps = 14, totalPx = 560, holdMs = 18) {
    const x = 196, yStart = 200;
    await S('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: yStart, id: 1 }] });
    for (let i = 1; i <= steps; i++) {
      const y = yStart + Math.round((totalPx * i) / steps);
      await S('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y, id: 1 }] });
      await sleep(holdMs);
    }
    await S('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  }

  const preRead = await geo();
  await dragDown();
  await sleep(200);
  const postRead = await geo();
  console.log(`${TAG} read gesture: ${preRead.top} -> ${postRead.top} (dist ${preRead.dist} -> ${postRead.dist}) unpinned=${postRead.unpinned} pinned=${postRead.pinned}`);
  const movedUp = Number.isFinite(preRead.top) && Number.isFinite(postRead.top) && postRead.top < preRead.top - 30;
  console.log(`${TAG} GESTURE_MOVED_UP=${movedUp ? 'yes' : 'NO'}`);

  const phaseB = await sample('B_after_read_gesture', 3000);
  const phaseC = await sample('C_steady', 2500);

  const backB = phaseB && Number.isFinite(postRead.top) ? phaseB.max - postRead.top : null;
  const pulledBack = backB !== null && backB > 60;
  console.log(`${TAG} PULLED_BACK=${pulledBack ? 'YES (' + backB + 'px towards the bottom)' : 'no'}`);
  console.log(`${TAG} OSCILLATION=${(phaseB && phaseB.rev > 4) || (phaseC && phaseC.rev > 4) ? 'YES' : 'no'} (B rev=${phaseB && phaseB.rev}, C rev=${phaseC && phaseC.rev})`);

  const writes = await ev('(window.__trace.writes||[]).slice(-16)');
  if (Array.isArray(writes) && writes.length) {
    console.log(`${TAG} attributed scrollTop writes (last ${writes.length}):`);
    for (const w of writes) console.log(`  t=${w.t} ${w.from}->${w.to} :: ${w.stack}`);
  } else console.log(`${TAG} writes: none`);

  browser.close();
  chrome.kill('SIGKILL');
  process.exit(0);
}

main().catch((e) => { console.log(`${TAG} error: ${e.message}`); process.exit(2); });
