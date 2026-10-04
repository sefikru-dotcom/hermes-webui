"""Behavioral checks for the touch terminal controls, without a PTY dependency."""
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_touch_terminal_keys_and_resize():
    source = ROOT / "static" / "terminal.js"
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const listeners={},requests=[],classes=new Set();
const handle={setPointerCapture(){},releasePointerCapture(){},setAttribute(){}};
const inner={style:{setProperty(){}},classList:{add(){},remove(){}},getBoundingClientRect(){return {height:190}}};
const window={innerHeight:915,visualViewport:{height:420,addEventListener(name,fn){listeners[name]=fn}},matchMedia(){return {matches:true}},addEventListener(){}};
const context={window,document:{documentElement:{},getElementById(){return null}},S:{session:{session_id:'touch-session'}},$:id=>id==='terminalResizeHandle'?handle:id==='composerTerminalPanel'?{querySelector:()=>inner}:null,api:(url,opts)=>{requests.push([url,JSON.parse(opts.body)]);return Promise.resolve({})},showToast(){},t:k=>k,setTimeout(){},clearTimeout(){},requestAnimationFrame(){},getComputedStyle(){return {getPropertyValue(){return ''}}},console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext(`
TERMINAL_UI.open=true;TERMINAL_UI.sessionId='touch-session';
TERMINAL_UI.term={input(data){window.keyData=data},focus(){window.focused=true}};
sendComposerTerminalKey('interrupt');
if(window.keyData!=='\\u0003'||!window.focused)throw Error('interrupt not delivered');
window.keyData=null;sendComposerTerminalKey('unknown');
if(window.keyData!==null)throw Error('unknown key accepted');
TERMINAL_UI.term=null;TERMINAL_UI.height=190;
_startTerminalHeightResize({pointerType:'touch',pointerId:1,clientY:300,preventDefault(){}});
if(!TERMINAL_UI.resizing)throw Error('touch resize ignored');
_moveTerminalHeightResize({clientY:270,preventDefault(){}});
if(TERMINAL_UI.height>Math.floor(window.visualViewport.height*.44))throw Error('height ignores keyboard viewport');
_endTerminalHeightResize({pointerId:1});
if(TERMINAL_UI.resizing)throw Error('resize not released');
`,context);
"""
    result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
