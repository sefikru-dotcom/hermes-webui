"""Runtime coverage for the dependency-free ActivityWindow component."""
import pathlib
import shutil
import subprocess

REPO = pathlib.Path(__file__).parent.parent
JS = REPO / "static" / "chat_activity.js"
NODE = shutil.which("node")


def test_activity_window_modes_and_reconnect_runtime():
    assert NODE, "node is required"
    source = JS.read_text(encoding="utf-8")
    script = r'''
const assert=require('assert');
class ClassList { constructor(){this.s=new Set()} toggle(k,v){if(v===undefined?v=!this.s.has(k):v){this.s.add(k)}else this.s.delete(k)} }
class E { constructor(tag='div'){this.tagName=tag;this.children=[];this.parentNode=null;this.attrs={};this.classList=new ClassList();this.style={};this.scrollTop=0;this.scrollHeight=0;this.clientHeight=100;this.textContent='';this.innerHTML=''}
 appendChild(x){x.parentNode=this;this.children.push(x);this.scrollHeight=this.children.length*20;return x}
 querySelector(sel){if(sel.includes('[data-activity-window="1"]')) return this.children.find(x=>x.attrs['data-activity-window']==='1')||null; if(sel.includes('.activity-window-events')) return this.children.find(x=>x.attrs._events)||null; if(sel.includes('.mars-process-window-state')) return this.children.find(x=>x.attrs._state)||null; return null}
 setAttribute(k,v){this.attrs[k]=String(v)} getAttribute(k){return this.attrs[k]||null}
 addEventListener(){}
 querySelectorAll(sel){return sel.includes('[data-activity-window="1"]')?this.children.filter(x=>x.attrs['data-activity-window']==='1'):[]}
 }
const doc={createElement:()=>new E(),};
const store=new Map(); const storage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)};
const ctx={document:doc,sessionStorage:storage,__DEV__:true}; ctx.window=ctx; global.window=ctx; global.document=doc;
const {ActivityWindow}=require(process.argv[1]);
for(const mode of ['stream_full','stream_window','stream_quiet']){
 const root=new E(); const w=new ActivityWindow({document:doc,root,sessionId:'s',turnId:'t-'+mode,mode,storage});
 if(mode==='stream_quiet'){assert.strictEqual(w.ensure(),null);continue}
 assert(w.ensure()); assert.strictEqual(w.apply({id:'a',type:'thinking',text:'preview'}),true); assert.strictEqual(w.apply({id:'a',type:'thinking',text:'duplicate'}),false); assert.strictEqual(w.addTool({id:'tool-1',name:'read_file'}),true); assert.strictEqual(w.reconnect({events:[{id:'tool-1',type:'tool'},{id:'tool-2',type:'tool',name:'write'}]}),1); assert.strictEqual(w.eventOrder.length,3);
 w.toggleDisclosure(true); const restored=new ActivityWindow({document:doc,root,sessionId:'s',turnId:'t-'+mode,mode,storage}); restored.ensure(); assert.strictEqual(restored._findWindow().getAttribute('data-disclosure-open'),'true'); restored.complete('done');
}
console.log('[activity-window] matrix/reconnect PASS');
'''
    result = subprocess.run([NODE, "-e", script, str(JS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout
