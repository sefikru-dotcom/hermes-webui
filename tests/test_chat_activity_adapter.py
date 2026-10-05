import os
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def test_activity_adapter_runtime_lifecycle_and_bounds():
    script = r'''
const assert=require('assert');const {ActivityWindow}=require('./static/chat_activity.js');const events={};
const header={attrs:{'aria-expanded':'true'},getAttribute(k){return this.attrs[k]||null},setAttribute(k,v){this.attrs[k]=v},addEventListener(k,fn){events[k]=fn}};
const frame={classList:{toggle(){}},querySelector(){return header},setAttribute(){},appendChild(){},ownerDocument:{createElement(){return {setAttribute(){}}}}};
const list={querySelector(){return null},ownerDocument:{createElement(){return {setAttribute(){},className:''}}},appendChild(){}};
const win={getAttribute(k){return k==='data-live-worklog-shell'?'1':k==='data-tool-worklog-key'?'live:stream':null},closest(){return frame},parentElement:frame,querySelector(s){return s==='.tool-worklog-list'?list:null}};
const root={querySelectorAll(){return [win]}};const values=new Map();const store={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)};
const a=new ActivityWindow({sessionId:'sid',streamId:'stream',root,storage:store});assert.strictEqual(a.bind(),win);
assert.strictEqual(a.thinkingUpdate('thinking'),true);assert.strictEqual(a.thinkingUpdate('thinking'),false);
assert(a.toolUpdate({tid:'tool'}));assert.strictEqual(a.toolUpdate({tid:'tool'}),false);
for(let i=0;i<300;i++)a._record('x');assert(a.telemetry().length<=128);
header.attrs['aria-expanded']='false';events.click();assert.strictEqual(values.get(a.key),'true');
assert(a.complete('summary one'));assert(a.complete('summary two'));
'''
    env=dict(os.environ,HOME='/tmp/mars-activity-home',HERMES_HOME='/tmp/mars-activity-hermes',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    # The updated runtime matrix exercises the renderer-owned frame contract.
    import runpy
    runpy.run_path(str(ROOT/'tests/test_activity_window_runtime.py'))['test_activity_window_modes_and_reconnect_runtime']()
def test_attach_live_stream_consumes_adapter_lifecycle():
    body=(ROOT/'static/messages.js').read_text().split('function attachLiveStream(',1)[1].split('\nfunction ',1)[0]
    for token in ('new ActivityWindow','_activityAdapter.bind()','_activityAdapter.thinkingUpdate','_activityAdapter.toolUpdate','_activityAdapter.complete'):
        assert token in body
