#!/usr/bin/env node
// Minimal diagnostic: which URL form actually hydrates a session in the SPA, and
// is the client state object readable from CDP?
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';

const TAG = '[diag]';
const PORT = 8787;
const PROFILE = '/tmp/diag-probe-profile';

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
  const req = http.request({ host: '127.0.0.1', port: PORT, path, method, headers: {
    ...(data ? { 'Content-Type': 'application/json', 'Content-Length': data.length, Origin: `http://127.0.0.1:${PORT}` } : {}),
    ...(cookie ? { Cookie: cookie } : {}),
  } }, (res) => { let raw = ''; res.on('data', (c) => { raw += c; }); res.on('end', () => { let p = null; try { p = JSON.parse(raw); } catch {} resolve({ status: res.statusCode, headers: res.headers, body: p }); }); });
  req.on('error', reject); if (data) req.write(data); req.end();
});

async function main() {
  const envFile = fs.readFileSync('/opt/hermes-webui/.env', 'utf8');
  const pw = (envFile.match(/^HERMES_WEBUI_PASSWORD=(.*)$/m) || [])[1]?.trim();
  const login = await httpJson('/api/auth/login', { method: 'POST', body: { password: pw } });
  const cookie = (login.headers['set-cookie'] || []).map((c) => String(c).split(';')[0]).join('; ');
  const list = await httpJson('/api/sessions', { cookie });
  const arr = (list.body && (list.body.sessions || list.body.data || list.body)) || [];
  const target = (Array.isArray(arr) ? arr : []).map((s) => ({ id: s.session_id || s.id, n: Number(s.message_count || 0) || 0 }))
    .filter((s) => s.id).sort((a, b) => b.n - a.n)[0];
  console.log(`${TAG} target ${target.id} (${target.n})`);

  const chrome = spawn('google-chrome', ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${PROFILE}`,
    '--no-first-run', '--no-default-browser-check', '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage', '--disable-setuid-sandbox',
    '--window-size=393,873', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const wsUrl = await new Promise((resolve, reject) => {
    let buf = ''; const t = setTimeout(() => reject(new Error('chrome timeout')), 20000);
    chrome.stderr.on('data', (c) => { buf += c.toString(); const m = buf.match(/DevTools listening on (ws:\/\/\S+)/); if (m) { clearTimeout(t); resolve(m[1]); } });
  });
  const browser = await new CDP(wsUrl).connect();
  const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
  const { sessionId: sid } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
  const S = (m, p = {}) => browser.send(m, p, sid);
  await S('Page.enable'); await S('Runtime.enable'); await S('Network.enable');
  await S('Network.setCookie', { name: cookie.split('=')[0], value: cookie.split('=').slice(1).join('='), domain: '127.0.0.1', path: '/' });

  const ev = async (expr) => {
    const r = await S('Runtime.evaluate', { expression: expr, returnByValue: true });
    if (r && r.exceptionDetails) return { __err: r.exceptionDetails.text, desc: r.exceptionDetails.exception && r.exceptionDetails.exception.description };
    return r && r.result ? r.result.value : undefined;
  };

  for (const url of [`http://127.0.0.1:${PORT}/?session=${target.id}`, `http://127.0.0.1:${PORT}/session/${target.id}`]) {
    await S('Page.navigate', { url });
    await sleep(5000);
    const probe = await ev(`(()=>{
      const el=document.getElementById('messages');
      let sState='n/a';
      try{ sState = (typeof S!=='undefined' && S.session) ? S.session.session_id : (typeof S!=='undefined' ? 'S.session=null' : 'S undefined'); }catch(e){ sState='throw:'+e.message; }
      return { path:location.pathname, search:location.search, rows:el?el.querySelectorAll('[data-msg-idx]').length:-1,
               top:el?Math.round(el.scrollTop):-1, sh:el?el.scrollHeight:-1, sState, title:document.title };
    })()`);
    console.log(`${TAG} ${url}\n      -> ${JSON.stringify(probe)}`);
  }

  browser.close(); chrome.kill('SIGKILL'); process.exit(0);
}
main().catch((e) => { console.log(`${TAG} error: ${e.message}`); process.exit(2); });
