"""Runtime matrix for the renderer-owned activity adapter."""
import subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def test_activity_window_modes_and_reconnect_runtime():
    script = r'''
const assert=require('assert');global.__DEV__=true;
const {ActivityWindow}=require('./static/chat_activity.js');
const values=new Map(),handlers={};let writes=0;
const store={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)};
const header={attrs:{'aria-expanded':'true'},getAttribute(k){return this.attrs[k]||null},setAttribute(k,v){this.attrs[k]=v},addEventListener(k,f){handlers[k]=f}};
const frame={attrs:{'data-tool-worklog-key':'live:stream'},getAttribute(k){return this.attrs[k]||null},setAttribute(k,v){this.attrs[k]=v},querySelector(){return header},classList:{toggle(k,v){frame.collapsed=v}}};
const container={querySelectorAll(){return [frame]},appendChild(){writes++}};
for(const mode of ['stream_full','stream_quiet'])assert.strictEqual(new ActivityWindow({root:container,streamId:'stream',mode}).bind(),null);
const a=new ActivityWindow({root:container,sessionId:'sid',streamId:'stream',storage:store});
assert.strictEqual(a.bind(),frame);assert.strictEqual(a.thinkingUpdate('secret'),true);assert.strictEqual(a.thinkingUpdate('secret'),false);
assert.notStrictEqual(a.thinking,'secret');assert.strictEqual(writes,0);
assert(a.toolUpdate({tid:'t',status:'running'}));assert(!a.toolUpdate({tid:'t',status:'running'}));assert(a.toolUpdate({tid:'t',status:'done'}));
header.attrs['aria-expanded']='false';handlers.click();assert.strictEqual(values.get(a.key),'false');
delete header.attrs['data-activity-adapter-bound'];const b=new ActivityWindow({root:container,sessionId:'sid',streamId:'stream',storage:store});b.bind();assert(frame.collapsed);
for(let i=0;i<300;i++){a.toolUpdate({tid:String(i)});a._record('x')};assert(a.seen.size<=128);assert(a.telemetry().length<=128);
assert(a.complete('done'));assert.strictEqual(frame.attrs['data-complete'],'1');assert.strictEqual(writes,0);
global.__DEV__=false;const c=new ActivityWindow();c._record('x');assert.strictEqual(c.telemetry().length,0);
console.log('PASS');
'''
    result=subprocess.run(['node','-e',script],cwd=ROOT,text=True,capture_output=True)
    assert result.returncode == 0,result.stderr
    assert 'PASS' in result.stdout
