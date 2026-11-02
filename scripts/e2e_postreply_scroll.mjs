#!/usr/bin/env node
// End-to-end verification of the "chat springs back to the bottom" fix on a
// REAL streamed reply.
//
// Flow: log in -> open the WebUI -> create a fresh session through the client's
// own api() helper (so CSRF is handled exactly as the UI does it) -> send a short
// prompt that forces a tool-using turn -> while the reply streams, the reader
// scrolls UP into history -> when `done` lands, the finalize path runs
// (keep-open arm, preserveScroll renders, tail follow). Measure whether the
// reader's position survives and whether the pane ever ping-pongs.
//
// Exit 0 = the reader keeps their place after the reply.

import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';

const TAG = '[e2e]';
const PORT = 8787;
const PROFILE = '/tmp/e2e-probe-profile';
const PROMPT = process.argv[2] || 'Скажи одно короткое предложение: проверка автоскролла.';

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

  await S('Page.addScriptToEvaluateOnNewDocument', {
    source: `(function(){
      window.__e={writes:[],samples:[],on:false};
      function hook(){ const d=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop'); if(!d||!d.set||d.set.__h) return;
        const w=function(v){ if(this&&this.id==='messages'){const b=d.get.call(this); d.set.call(this,v); const a=d.get.call(this);
          if(Math.abs(a-b)>=2 && window.__e.writes.length<600) window.__e.writes.push({t:Math.round(performance.now()),from:Math.round(b),to:Math.round(a),stack:(new Error().stack||'').split('\\n').slice(2,6).join(' | ')});
        } else d.set.call(this,v); };
        w.__h=true; Object.defineProperty(Element.prototype,'scrollTop',{configurable:true,enumerable:d.enumerable,get(){return d.get.call(this);},set:w});
      }
      const iv=setInterval(()=>{ if(document.getElementById('messages')){hook();clearInterval(iv);} },50);
      (function loop(){ if(window.__e.on){ const el=document.getElementById('messages');
        if(el) window.__e.samples.push({t:Math.round(performance.now()),top:Math.round(el.scrollTop)}); } requestAnimationFrame(loop); })();
    })();`,
  });

  await S('Page.navigate', { url: `http://127.0.0.1:${PORT}/` });
  await sleep(4500);

  const ev = async (expr, awaitPromise = false) => {
    const r = await S('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
    if (r && r.exceptionDetails) return { __err: r.exceptionDetails.text };
    return r && r.result ? r.result.value : undefined;
  };

  const created = await ev(`(async()=>{ try{
      const d=await api('/api/session/new',{method:'POST',body:JSON.stringify({})});
      return { sid: d && d.session && d.session.session_id };
    }catch(e){ return {err:String(e&&e.message||e)}; } })()`, true);
  console.log(`${TAG} new session: ${JSON.stringify(created)}`);
  if (!created || !created.sid) { browser.close(); chrome.kill('SIGKILL'); process.exit(2); }

  // SPA route is /session/<sid> — a ?session= query is NOT the session URL.
  await S('Page.navigate', { url: `http://127.0.0.1:${PORT}/session/${encodeURIComponent(created.sid)}` });
  await sleep(3500);
  const landed = await ev(`(()=>({url:location.pathname,sid:(typeof S!=='undefined'&&S.session&&S.session.session_id)||null,msgs:(typeof S!=='undefined'&&Array.isArray(S.messages))?S.messages.length:0}))()`);
  console.log(`${TAG} landed: ${JSON.stringify(landed)}`);
  // The SPA hydrates asynchronously; the authoritative signal is the rendered
  // transcript container, not the URL or a private variable.
  let hydrated = false;
  for (let i = 0; i < 40; i++) {
    const st = await ev(`(()=>{const el=document.getElementById('messages');return {sid:(typeof S!=='undefined'&&S.session&&S.session.session_id)||null,hasPane:!!el,msgs:(typeof S!=='undefined'&&Array.isArray(S.messages))?S.messages.length:0,title:document.title,bodyText:document.body?String(document.body.innerText||'').slice(0,120):null};})()`);
    if (st && st.sid === created.sid) { hydrated = true; console.log(`${TAG} hydrated after ${((i + 1) * 0.25).toFixed(1)}s: ${JSON.stringify(st)}`); break; }
    await sleep(250);
  }
  if (!hydrated) { console.log(`${TAG} abort: session never hydrated`); browser.close(); chrome.kill('SIGKILL'); process.exit(2); }

  // Give the transcript something to scroll: the reply alone is short, so seed
  // tall filler rows on top, exactly like a long history would present.
  await ev(`(()=>{const inner=document.getElementById('msgInner'); if(!inner) return 0;
    for(let i=0;i<6;i++){const d=document.createElement('div'); d.style.height='700px'; d.setAttribute('data-e2e-filler',String(i)); inner.insertBefore(d, inner.firstChild);} return inner.childElementCount;})()`);
  await sleep(200);
  await ev(`(()=>{ if(typeof scrollToBottom==='function') scrollToBottom(); return 1; })()`);
  await sleep(600);

  const start = await ev(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),max:Math.round(el.scrollHeight-el.clientHeight),dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight)};})()`);
  console.log(`${TAG} before send: ${JSON.stringify(start)}`);

  const sent = await ev(`(async()=>{ try{
      const sid=(S&&S.session&&S.session.session_id)||'';
      const d=await api('/api/chat/start',{method:'POST',body:JSON.stringify({session_id:sid,message:${JSON.stringify(PROMPT)},files:[],attachments:[]})});
      return {ok:true, sid, keys:d?Object.keys(d).slice(0,8):null};
    }catch(e){ return {ok:false, err:String(e&&e.message||e)}; } })()`, true);
  console.log(`${TAG} chat/start: ${JSON.stringify(sent)}`);
  if (!sent || !sent.ok) { browser.close(); chrome.kill('SIGKILL'); process.exit(2); }

  // While the reply streams, the reader scrolls up into history (this is the
  // moment the report describes: they start reading back during/after the answer).
  await sleep(2500);
  async function dragDown(steps = 12, totalPx = 480, holdMs = 18) {
    const x = 196, yStart = 200;
    await S('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: yStart, id: 1 }] });
    for (let i = 1; i <= steps; i++) {
      await S('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: yStart + Math.round((totalPx * i) / steps), id: 1 }] });
      await sleep(holdMs);
    }
    await S('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  }
  await dragDown();
  await sleep(200);
  const midStream = await ev(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight),unpinned:_messageUserUnpinned,busy:!!S.busy};})()`);
  console.log(`${TAG} after read-up mid-stream: ${JSON.stringify(midStream)}`);

  // Wait for done.
  let finished = false;
  for (let i = 0; i < 240; i++) {
    const st = await ev(`(()=>({busy:!!S.busy,sid:S.activeStreamId||null}))()`);
    if (st && !st.busy && !st.sid) { finished = true; console.log(`${TAG} stream finished after ~${((i + 1) * 0.5).toFixed(1)}s`); break; }
    await sleep(500);
  }
  if (!finished) console.log(`${TAG} WARN: stream did not report done within the window`);

  await sleep(400);
  const postDoneA = await ev(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight),unpinned:_messageUserUnpinned,pinned:_scrollPinned};})()`);
  await sleep(2500);
  const postDoneB = await ev(`(()=>{const el=document.getElementById('messages');return {top:Math.round(el.scrollTop),dist:Math.round(el.scrollHeight-el.scrollTop-el.clientHeight),unpinned:_messageUserUnpinned,pinned:_scrollPinned};})()`);
  console.log(`${TAG} post-done +0.4s: ${JSON.stringify(postDoneA)}`);
  console.log(`${TAG} post-done +2.9s: ${JSON.stringify(postDoneB)}`);

  const samples = await ev('(window.__e.samples||[])');
  const readsFrames = [];
  await ev('window.__e.samples=[];window.__e.on=true');
  await sleep(2500);
  const trace = await ev('(window.__e.samples||[]).filter(s=>s.top>0)');
  await ev('window.__e.on=false');
  let rev = 0, pd = 0;
  for (const s of (trace || [])) { const d = s.top - pd; const dir = d > 2 ? 1 : d < -2 ? -1 : 0; if (dir && pd && dir !== pd && pd !== 0) rev++; if (dir) pd = s.top; }
  const tops = (trace || []).map((s) => s.top);
  const span = tops.length ? Math.max(...tops) - Math.min(...tops) : 0;
  console.log(`${TAG} post-done trace: frames=${tops.length} span=${span} reversals=${rev}`);

  const pulled = midStream && postDoneB && midStream.unpinned === true
    ? (postDoneB.unpinned !== true || postDoneB.top > midStream.top + 40)
    : false;
  const oscillated = rev > 4 || span > 400;
  console.log(`${TAG} READER_PULLED_BACK=${pulled ? 'YES (defect)' : 'no'}`);
  console.log(`${TAG} POST_DONE_OSCILLATION=${oscillated ? 'YES (defect)' : 'no'} (span=${span} rev=${rev})`);

  const writes = await ev('(window.__e.writes||[]).slice(-10)');
  if (Array.isArray(writes) && writes.length) {
    console.log(`${TAG} last writes:`);
    for (const w of writes) console.log(`  t=${w.t} ${w.from}->${w.to} :: ${w.stack}`);
  }

  const ok = !pulled && !oscillated;
  console.log(`${TAG} VERDICT=${ok ? 'PASS' : 'FAIL'}`);
  browser.close();
  chrome.kill('SIGKILL');
  process.exit(ok ? 0 : 1);
}

main().catch((e) => { console.log(`${TAG} error: ${e.message}`); process.exit(2); });
