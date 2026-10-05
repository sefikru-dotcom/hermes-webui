/* Bounded adapter for the renderer-owned mars-process-window. */
(function(root){
  'use strict';
  const LIMIT=128, MAX_TEXT=240;
  const storage=()=>{try{return root.sessionStorage||null;}catch(_){return null;}};
  class ActivityWindow {
    constructor({sessionId='',streamId='',turnId=streamId,root:container=null,storage:store=null,mode='stream_window'}={}) {
      this.sessionId=String(sessionId); this.streamId=String(streamId||turnId); this.root=container; this.storage=store||storage(); this.mode=mode; this.metrics=[];
      this.key=`hermes:activity:disclosure:${encodeURIComponent(this.sessionId)}:${encodeURIComponent(this.streamId)}`;
      this.seen=new Map(); this.order=[]; this.thinking='';
    }
    _record(kind, value=0) { this.metrics.push({kind,value:Number(value)||0,at:Date.now()}); if(this.metrics.length>LIMIT)this.metrics.splice(0,this.metrics.length-LIMIT); }
    telemetry(){return this.metrics.map(x=>({...x}));}
    _find(){const key='live:'+this.streamId;return this.root&&this.root.querySelectorAll?Array.from(this.root.querySelectorAll('.mars-process-window')).find(n=>n.getAttribute('data-live-worklog-shell')==='1'&&n.getAttribute('data-tool-worklog-key')===key)||null:null;}
    bind(){const win=this._find();if(!win||this.mode==='stream_quiet'||this.mode==='hide_all_activity')return null;
      const frame=win.closest('.mars-process-window')||win.parentElement;const header=frame&&frame.querySelector('.mars-process-window-header');if(!header)return win;
      if(header.getAttribute('data-activity-adapter-bound')!=='1'){
        header.setAttribute('data-activity-adapter-bound','1');
        header.addEventListener('click',()=>{const open=header.getAttribute('aria-expanded')!=='true';header.setAttribute('aria-expanded',String(open));try{this.storage&&this.storage.setItem(this.key,String(open));}catch(_){}});
      }
      const saved=this.storage&&this.storage.getItem(this.key);if(saved==='true'||saved==='false'){const open=saved==='true';header.setAttribute('aria-expanded',String(open));frame.classList.toggle('is-collapsed',!open);}
      const list=win.querySelector('.tool-worklog-list');if(list&&!list.querySelector('[data-activity-live-region]')){const region=list.ownerDocument.createElement('div');region.setAttribute('data-activity-live-region','1');region.setAttribute('aria-live','polite');region.setAttribute('aria-atomic','true');region.className='activity-token-region';list.appendChild(region);}return win;
    }
    _update(id,render){if(!id)return false;const key=String(id);if(this.seen.has(key)){this.seen.get(key)();this._record('dedupe');return false;}this.seen.set(key,render);this.order.push(key);if(this.order.length>LIMIT){const old=this.order.shift();this.seen.delete(old);}render();return true;}
    thinkingUpdate(text){const value=String(text||'').slice(-MAX_TEXT);if(value===this.thinking)return false;this.thinking=value;this._record('thinking');const win=this.bind();if(win&&win.getAttribute('data-live-worklog-shell')==='1'){const region=win.querySelector('.activity-token-region')||win.querySelector('.tool-worklog-list');if(region)region.textContent=value;}else if(typeof root._updateLiveThinkingCard==='function')root._updateLiveThinkingCard(value,{sessionId:this.sessionId,streamId:this.streamId});return true;}
    toolUpdate(tool){tool=tool||{};return this._update(tool.id||tool.tid||tool.tool_call_id,()=>{this._record('tool');});}
    complete(summary){const win=this.bind();if(!win)return false;const parent=win.closest('.mars-process-window')||win;const old=parent.querySelector('.activity-summary');if(old&&old.remove)old.remove();if(summary){const node=parent.ownerDocument.createElement('div');node.className='activity-summary';node.textContent=String(summary).slice(0,MAX_TEXT);parent.appendChild(node);}parent.setAttribute('data-complete','1');this._record('complete');return true;}
  }
  if(root)root.ActivityWindow=ActivityWindow;if(typeof module!=='undefined'&&module.exports)module.exports={ActivityWindow};
})(typeof window!=='undefined'?window:globalThis);
