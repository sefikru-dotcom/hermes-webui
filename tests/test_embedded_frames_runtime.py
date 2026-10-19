"""Behaviour of static/embedded_frames.js under a stubbed DOM.

These run the real module (not a copy of its text) so the probe outcomes map to
the right UI state: a missing mount hides the frame and marks the tab, while a
timeout/network failure keeps the retry path and never hides navigation.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE = REPO / "static" / "embedded_frames.js"

HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');

function makeEl(id){
  return {
    id, hidden:false, dataset:{}, attrs:{}, listeners:{},
    getAttribute(k){ return Object.prototype.hasOwnProperty.call(this.attrs,k) ? this.attrs[k] : null; },
    setAttribute(k,v){ this.attrs[k]=String(v); },
    removeAttribute(k){ delete this.attrs[k]; },
    hasAttribute(k){ return Object.prototype.hasOwnProperty.call(this.attrs,k); },
    querySelector(){ return null; },
    addEventListener(t,fn){ (this.listeners[t]=this.listeners[t]||[]).push(fn); },
    fire(t){ (this.listeners[t]||[]).forEach(fn=>fn({})); },
    classList:{ add(){}, remove(){}, toggle(){} }
  };
}

const IDS = ['projectsIframe','musicIframe','projectsFrameHost','musicFrameHost',
             'projectsFrameState','musicFrameState','projectsFrameUnavailable','musicFrameUnavailable'];

function setup(fetchImpl){
  const els = {};
  IDS.forEach(id => { els[id] = makeEl(id); });
  // The real markup ships both frames hidden and without a src.
  els.projectsIframe.hidden = true;
  els.musicIframe.hidden = true;

  const tabs = ['projects','music'].map(panel => {
    const tab = makeEl('tab-'+panel);
    tab.dataset.panel = panel;
    tab.setAttribute('data-tooltip', panel === 'projects' ? 'Projects' : 'Music');
    return tab;
  });

  const doc = {
    baseURI:'http://localhost:8787/',
    readyState:'complete',
    hidden:false,
    getElementById(id){ return els[id] || null; },
    querySelectorAll(sel){
      const m = /data-panel="([^"]+)"/.exec(sel);
      if(sel.indexOf('role="tab"') !== -1) return m ? tabs.filter(t => t.dataset.panel === m[1]) : tabs;
      return [];
    },
    addEventListener(){}
  };

  const ctx = {
    console, Promise, setTimeout, clearTimeout, URL, Date, AbortController,
    document: doc,
    location:{ href:'http://localhost:8787/' },
    fetch: fetchImpl,
    t: (k) => k,
  };
  ctx.window = ctx;
  const vm = require('vm');
  vm.createContext(ctx);
  new vm.Script(source, {filename:'embedded_frames.js'}).runInContext(ctx);
  return { ctx, els, tabs };
}

async function run(){
  const out = {};
  const okRes = { ok:true, status:200, headers:{ get: () => 'text/html; charset=utf-8' } };
  const missingRes = { ok:false, status:404, headers:{ get: () => 'application/json' } };
  const jsonOk = { ok:true, status:200, headers:{ get: () => 'application/json' } };

  // 1. Mounted section: the frame gets a src, the tab stays usable, and the
  //    second frame is still untouched (nothing is fetched at boot).
  let h = setup(() => Promise.resolve(okRes));
  out.mounted = await h.ctx.ensureEmbeddedFrame('dashboard');
  out.mounted_src = h.els.projectsIframe.getAttribute('src');
  out.mounted_hidden = h.els.projectsIframe.hidden;
  out.mounted_tab_disabled = h.tabs[0].getAttribute('aria-disabled');
  out.mounted_boot_src = h.els.musicIframe.getAttribute('src');

  // 2. 404 -> unavailable state, frame has no src, tab is marked unavailable.
  h = setup(() => Promise.resolve(missingRes));
  out.missing = await h.ctx.ensureEmbeddedFrame('dashboard');
  out.missing_src = h.els.projectsIframe.getAttribute('src');
  out.missing_unavailable_hidden = h.els.projectsFrameUnavailable.hidden;
  out.missing_frame_hidden = h.els.projectsIframe.hidden;
  out.missing_tab_disabled = h.tabs[0].getAttribute('aria-disabled');

  // 3. A JSON 200 at the mount path is still not a section.
  h = setup(() => Promise.resolve(jsonOk));
  out.json = await h.ctx.ensureEmbeddedFrame('dashboard');

  // 4. Network failure -> retry path, navigation is never hidden.
  h = setup(() => Promise.reject(new TypeError('Failed to fetch')));
  out.network = await h.ctx.ensureEmbeddedFrame('dashboard');
  out.network_tab_disabled = h.tabs[0].getAttribute('aria-disabled');
  out.network_unavailable_hidden = h.els.projectsFrameUnavailable.hidden;

  // 5. Retry after a 404 re-probes and recovers.
  let calls = 0;
  h = setup(() => { calls++; return Promise.resolve(calls === 1 ? missingRes : okRes); });
  out.retry_first = await h.ctx.ensureEmbeddedFrame('dashboard');
  out.retry_second = await h.ctx.retryEmbeddedFrame('dashboard');
  out.retry_calls = calls;
  out.retry_src = h.els.projectsIframe.getAttribute('src');

  // 6. Reopening an already loaded panel does not re-probe.
  calls = 0;
  h = setup(() => { calls++; return Promise.resolve(okRes); });
  await h.ctx.ensureEmbeddedFrame('dashboard');
  await h.ctx.ensureEmbeddedFrame('dashboard');
  out.reopen_calls = calls;

  process.stdout.write(JSON.stringify(out));
}
run().catch(e => { process.stdout.write(JSON.stringify({error:String((e && e.stack) || e)})); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the JS harness")
def test_embedded_frame_probe_states():
    script = REPO / "tests" / "_embedded_frames_harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    try:
        raw = subprocess.check_output(
            ["node", str(script), str(MODULE)], cwd=REPO, text=True, timeout=60
        )
    finally:
        script.unlink(missing_ok=True)
    out = json.loads(raw)
    assert "error" not in out, out.get("error")

    assert out["mounted"] == "ready"
    assert out["mounted_src"] == "/dashboard/"
    assert out["mounted_hidden"] is False
    assert out["mounted_tab_disabled"] is None
    # Nothing is fetched at boot: the second frame has no src until it is opened.
    assert out["mounted_boot_src"] is None

    assert out["missing"] == "unavailable"
    assert out["missing_src"] is None
    assert out["missing_unavailable_hidden"] is False
    assert out["missing_frame_hidden"] is True
    assert out["missing_tab_disabled"] == "true"

    assert out["json"] == "unavailable"

    assert out["network"] == "error"
    assert out["network_tab_disabled"] is None, "a network error must not hide navigation"
    assert out["network_unavailable_hidden"] is False

    assert out["retry_first"] == "unavailable"
    assert out["retry_second"] == "ready"
    assert out["retry_calls"] == 2, "retry must re-probe instead of replaying the cached verdict"
    assert out["reopen_calls"] == 1, "reopening a loaded panel must not re-probe"
    assert out["retry_src"].startswith("/dashboard/?retry=")
