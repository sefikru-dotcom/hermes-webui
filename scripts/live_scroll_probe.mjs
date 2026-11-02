#!/usr/bin/env node
// Live probe: is the "chat snaps back to the bottom" report caused by a writer
// that fires AFTER the reader scrolls up?
//
// Drives a real Chrome over CDP (no npm deps: node >=22 has WebSocket/ws-free
// CDP framing is done by hand here). Instruments Element.prototype scrollTop so
// EVERY write is attributed to a stack, loads the longest available session,
// scrolls up like a reader, and reports which writers moved the viewport back.
//
// Usage: node scripts/live_scroll_probe.mjs [--port 8787] [--duration 6000]

import { execFile, spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';

const TAG = '[live-probe]';
const args = process.argv.slice(2);
const argOf = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const PORT = Number(argOf('--port', 8787));
const PROFILE = argOf('--profile', '/tmp/live-probe-profile');

// ── minimal CDP client ──────────────────────────────────────────────────────
class CDP {
  constructor(url) { this.url = url; this.id = 0; this.pending = new Map(); this.handlers = new Map(); }
  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);
      this.ws.onopen = () => resolve(this);
      this.ws.onerror = (e) => reject(new Error('ws error: ' + (e.message || 'unknown')));
      this.ws.onmessage = (m) => {
        let msg; try { msg = JSON.parse(m.data); } catch { return; }
        if (msg.id && this.pending.has(msg.id)) {
          const { resolve: res, reject: rej } = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          msg.error ? rej(new Error(msg.error.message)) : res(msg.result);
        } else if (msg.method) {
          for (const h of (this.handlers.get(msg.method) || [])) h(msg.params);
        }
      };
    });
  }
  on(method, fn) { (this.handlers.get(method) || this.handlers.set(method, []).get(method)).push(fn); }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
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
      ...(data ? { 'Content-Type': 'application/json', 'Content-Length': data.length } : {}),
      ...(cookie ? { Cookie: cookie } : {}),
      ...(data ? { Origin: `http://127.0.0.1:${PORT}` } : {}),
    },
  }, (res) => {
    let raw = '';
    res.on('data', (c) => { raw += c; });
    res.on('end', () => {
      let parsed = null; try { parsed = JSON.parse(raw); } catch {}
      resolve({ status: res.statusCode, headers: res.headers, body: parsed, raw });
    });
  });
  req.on('error', reject);
  if (data) req.write(data);
  req.end();
});

// ── helpers ────────────────────────────────────────────────────────────────
const pw = (() => {
  const env = fs.readFileSync('/opt/hermes-webui/.env', 'utf8');
  const m = env.match(/^HERMES_WEBUI_PASSWORD=(.*)$/m);
  return m ? m[1].trim() : '';
})();

async function main() {
  if (!pw) { console.log(`${TAG} abort: no HERMES_WEBUI_PASSWORD`); process.exit(2); }

  // 1. authenticate (password path -> session cookie)
  const login = await httpJson('/api/auth/login', {
    method: 'POST', body: { password: pw },
  });
  if (login.status !== 200) {
    console.log(`${TAG} login failed: ${login.status} ${String(login.raw).slice(0, 200)}`);
    process.exit(2);
  }
  const setCookie = login.headers['set-cookie'] || [];
  const cookie = setCookie.map((c) => String(c).split(';')[0]).join('; ');
  console.log(`${TAG} logged in; cookie fields: ${setCookie.length}`);

  // 2. find the longest session to reproduce on a tall transcript
  const list = await httpJson('/api/sessions', { cookie });
  const sessions = (list.body && (list.body.sessions || list.body.data || list.body)) || [];
  const arr = Array.isArray(sessions) ? sessions : [];
  const scored = arr.map((s) => ({
    id: s.session_id || s.id,
    n: Number(s.message_count || s.messageCount || (Array.isArray(s.messages) ? s.messages.length : 0)) || 0,
    title: String(s.title || '').slice(0, 40),
  })).filter((s) => s.id).sort((a, b) => b.n - a.n);
  console.log(`${TAG} sessions: ${arr.length}; longest: ${JSON.stringify(scored.slice(0, 5))}`);
  const target = scored[0];
  if (!target) { console.log(`${TAG} abort: no sessions`); process.exit(2); }

  // 3. launch chrome
  const chrome = spawn('google-chrome', [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--no-sandbox', '--disable-dev-shm-usage', '--disable-setuid-sandbox',
    '--window-size=412,915', 'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });

  const wsUrl = await new Promise((resolve, reject) => {
    let buf = '';
    const t = setTimeout(() => reject(new Error('chrome start timeout')), 20000);
    chrome.stderr.on('data', (c) => {
      buf += c.toString();
      const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(t); resolve(m[1]); }
    });
  });
  console.log(`${TAG} chrome up`);

  const browser = await new CDP(wsUrl).connect();
  const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
  const att = await browser.send('Target.attachToTarget', { targetId, flatten: true });
  const sid = att.sessionId;
  const send = (method, params = {}) => browser.send(method, { ...params }).catch(() => {});
  // session-scoped send
  const ssend = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++browser.id;
    browser.pending.set(id, { resolve, reject });
    browser.ws.send(JSON.stringify({ id, method, params, sessionId: sid }));
  });

  await ssend('Page.enable');
  await ssend('Runtime.enable');
  await ssend('Network.enable');
  await ssend('Emulation.setDeviceMetricsOverride', { width: 412, height: 915, deviceScaleFactor: 2.6, mobile: true });
  await ssend('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });

  // 4. set the cookie then navigate
  await ssend('Network.setCookie', {
    name: cookie.split('=')[0], value: cookie.split('=').slice(1).join('='),
    domain: '127.0.0.1', path: '/',
  });
  await ssend('Page.navigate', { url: `http://127.0.0.1:${PORT}/` });
  await sleep(4000);

  const evaluate = async (expr, awaitPromise = false) => {
    const r = await ssend('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
    if (r && r.exceptionDetails) return { error: r.exceptionDetails.text };
    return r && r.result ? r.result.value : undefined;
  };

  const title = await evaluate('document.title');
  console.log(`${TAG} loaded: ${title}`);

  // 5. open the longest session FIRST — a full page load would wipe any patch
  // installed on the previous document, so instrumentation comes after.
  await ssend('Page.navigate', { url: `http://127.0.0.1:${PORT}/?session=${encodeURIComponent(target.id)}` });
  await sleep(5500);

  const instrument = `
  (function(){
    if(window.__probeInstalled) return 'already';
    const el=document.getElementById('messages');
    if(!el) return 'no-pane';
    window.__probeInstalled=true;
    window.__probe={writes:[], startedAt:performance.now()};
    const desc=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop');
    if(!desc||!desc.set) return 'no-desc';
    Object.defineProperty(Element.prototype,'scrollTop',{
      configurable:true, enumerable:desc.enumerable,
      get(){return desc.get.call(this);},
      set(v){
        if(this&&this.id==='messages'){
          const before=desc.get.call(this);
          desc.set.call(this,v);
          const after=desc.get.call(this);
          if(Math.abs(after-before)>=2){
            const st=(new Error().stack||'').split('\\n').slice(2,7).join(' | ');
            window.__probe.writes.push({t:Math.round(performance.now()), from:Math.round(before), to:Math.round(after), stack:st});
            if(window.__probe.writes.length>600) window.__probe.writes.shift();
          }
        } else { desc.set.call(this,v); }
      }
    });
    return 'installed';
  })()`;
  console.log(`${TAG} instrument: ${JSON.stringify(await evaluate(instrument))}`);

  const geom = await evaluate(`(()=>{const el=document.getElementById('messages');return el?{top:Math.round(el.scrollTop),sh:el.scrollHeight,ch:el.clientHeight,rows:document.querySelectorAll('[data-msg-idx]').length}:null})()`);
  console.log(`${TAG} geometry: ${JSON.stringify(geom)}`);

  // 7. reader scrolls up with a REAL trusted touch gesture (CDP synthesises
  // input at the browser level, so wheel/touch listeners fire like on a phone),
  // then we watch for the snap-back and for the settle machine's behaviour.
  const snap = () => evaluate(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight),unpinned:(typeof _messageUserUnpinned!=='undefined')?_messageUserUnpinned:null,pinned:(typeof _scrollPinned!=='undefined')?_scrollPinned:null,probeWrites:(window.__probe&&window.__probe.writes)?window.__probe.writes.length:0,ctl:(typeof _messageScrollStats==='function')?_messageScrollStats():null};})()`);

  const s0 = await snap();
  console.log(`${TAG} before gesture: ${JSON.stringify(s0)}`);

  // A genuine upward scroll: mouseWheel is a trusted, top-level input event, so
  // the document capture listeners see it exactly like a real wheel.
  for (let i = 0; i < 6; i++) {
    await ssend('Input.dispatchMouseEvent', {
      type: 'mouseWheel', x: 200, y: 500, deltaX: 0, deltaY: -220,
      modifiers: 0, pointerType: 'mouse',
    }).catch(() => {});
    await sleep(120);
  }
  const s1 = await snap();
  console.log(`${TAG} right after gesture: ${JSON.stringify(s1)}`);

  await sleep(2200);
  const s2 = await snap();
  console.log(`${TAG} after 2.2s idle: ${JSON.stringify(s2)}`);

  // Two sample points are not enough: a snap-down-then-restore fight nets to
  // zero. Sample the viewport every 60 ms and record min/max so a mid-window
  // bounce and a persistent yank are distinguishable.
  const series = [];
  for (let i = 0; i < 40; i++) {
    series.push(Number((await evaluate(`(()=>{const el=document.getElementById('messages');return Math.round(el.scrollTop);})()`)) || 0));
    await sleep(60);
  }
  const clean = series.filter((v) => Number.isFinite(v) && v > 0);
  const minTop = Math.min(...clean), maxTop = Math.max(...clean);
  console.log(`${TAG} 2.4s series: min=${minTop} max=${maxTop} span=${maxTop - minTop} first=${clean[0]} last=${clean[clean.length - 1]}`);

  const writes = await evaluate('(window.__probe.writes||[]).slice(-14)');
  const snapped = Number.isFinite(s1 && s1.top) && Number.isFinite(s2 && s2.top) && s2.top > s1.top + 40;
  const bounced = (maxTop - minTop) > 40;
  const unpinnedAfterGesture = !!(s1 && s1.unpinned);
  console.log(`${TAG} SNAP_BACK=${snapped ? 'YES' : 'no'} from ${s1 && s1.top} to ${s2 && s2.top}`);
  console.log(`${TAG} BOUNCE=${bounced ? 'YES' : 'no'} span=${maxTop - minTop}px`);
  console.log(`${TAG} UNPINNED_AFTER_GESTURE=${unpinnedAfterGesture ? 'yes' : 'NO (reader scroll swallowed)'}`);

  // 10. Real end-to-end: send an actual turn, wait for the stream to finish, then
  // scroll up in the seconds right after `done` and watch for a snap-back. This
  // is the only way to exercise the true finalize path (keep-open arm, two
  // renders, cue, settle timers) in order.
  console.log(`${TAG} --- live turn test ---`);
  const modelSel = await evaluate(`(()=>({model:(typeof S!=='undefined'&&S.model)||null,provider:(typeof S!=='undefined'&&S.model_provider)||null,busy:(typeof S!=='undefined')?S.busy:null}))()`);
  console.log(`${TAG} model state: ${JSON.stringify(modelSel)}`);

  // The open session is an imported (read-only) transcript. Start a real one so
  // /api/chat/start is accepted, then seed it with enough long messages that the
  // viewport is scrollable, and run a genuine streamed turn.
  const newSid = await evaluate(`(async()=>{
    try{
      const r=await api('/api/sessions',{method:'POST',body:JSON.stringify({title:'scroll-probe'})});
      return {sid:(r&&(r.session_id||(r.session&&r.session.session_id)))||null, keys:r?Object.keys(r).slice(0,8):null};
    }catch(e){ return {err:String(e&&e.message||e)}; }
  })()`, true);
  console.log(`${TAG} new session: ${JSON.stringify(newSid)}`);
  if (newSid && newSid.sid) {
    await ssend('Page.navigate', { url: `http://127.0.0.1:${PORT}/?session=${encodeURIComponent(newSid.sid)}` });
    await sleep(3500);
    await evaluate(instrument);
  }

  const turnOk = await evaluate(`(async()=>{
    try{
      const sid=(S&&S.session&&S.session.session_id)||'';
      const r=await api('/api/chat/start',{method:'POST',body:JSON.stringify({
        session_id:sid, message:'Ответь одним коротким предложением про скролл.', files:[], attachments:[],
      })});
      return {ok:true, sid, status:r&&r.status, keys:r?Object.keys(r).slice(0,8):null};
    }catch(e){ return {ok:false, err:String(e&&e.message||e)}; }
  })()`, true);
  console.log(`${TAG} chat/start: ${JSON.stringify(turnOk)}`);

  // Poll until the stream is over (S.busy false + no activeStreamId).
  let sawBusy = false;
  for (let i = 0; i < 120; i++) {
    const st = await evaluate(`(()=>({busy:(typeof S!=='undefined')?!!S.busy:false,sid:(typeof S!=='undefined'&&S.activeStreamId)||null}))()`);
    if (st && st.busy) sawBusy = true;
    if (sawBusy && st && !st.busy && !st.sid) { console.log(`${TAG} stream finished after ~${(i * 0.5).toFixed(1)}s`); break; }
    await sleep(500);
  }

  // Immediately after done: reader scrolls up and we sample for 4 s.
  const post = [];
  for (let i = 0; i < 8; i++) {
    await ssend('Input.dispatchMouseEvent', { type: 'mouseWheel', x: 200, y: 500, deltaX: 0, deltaY: -200, modifiers: 0, pointerType: 'mouse' }).catch(() => {});
    await sleep(80);
    post.push(await evaluate(`(()=>({top:Math.round(document.getElementById('messages').scrollTop),unpinned:_messageUserUnpinned,pinned:_scrollPinned}))()`));
  }
  const afterScrollUp = post[post.length - 1];
  await sleep(600);
  const settle1 = await evaluate(`(()=>({top:Math.round(document.getElementById('messages').scrollTop),unpinned:_messageUserUnpinned,pinned:_scrollPinned}))()`);
  await sleep(2200);
  const settle2 = await evaluate(`(()=>({top:Math.round(document.getElementById('messages').scrollTop),unpinned:_messageUserUnpinned,pinned:_scrollPinned}))()`);
  const postBack = afterScrollUp && settle2 && (settle2.top - afterScrollUp.top) > 40;
  console.log(`${TAG} POST-DONE scroll-up: ${JSON.stringify(afterScrollUp)} -> ${JSON.stringify(settle1)} -> ${JSON.stringify(settle2)}`);
  console.log(`${TAG} POST_DONE_PULLED_BACK=${postBack ? 'YES (this is the reported bug)' : 'no'}`);
  console.log(`${TAG} POST_DONE_UNPINNED=${afterScrollUp && afterScrollUp.unpinned === true ? 'yes' : 'NO (scroll swallowed)'}`);

  if (Array.isArray(writes) && writes.length) {
    console.log(`${TAG} last scrollTop writes (attributed):`);
    for (const w of writes) console.log(`  t=${w.t} ${w.from}->${w.to} :: ${w.stack}`);
  }

  browser.close();
  chrome.kill('SIGKILL');
  process.exit(snapped ? 1 : 0);
}

main().catch((e) => { console.log(`${TAG} error: ${e.message}`); process.exit(2); });
