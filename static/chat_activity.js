/* Renderer-owned activity shell adapter; never renders reasoning or tools. */
(function(root){
  'use strict';
  const LIMIT=128;
  const storage=()=>{try{return root.sessionStorage||null;}catch(_){return null;}};
  class ActivityWindow {
    constructor({sessionId='',streamId='',turnId=streamId,root:container=null,storage:store=null,mode='stream_window'}={}){
      this.sessionId=String(sessionId);this.streamId=String(streamId||turnId);
      this.root=container;this.storage=store||storage();this.mode=mode;
      this.key=`mars:activity:disclosure:${encodeURIComponent(this.sessionId)}:${encodeURIComponent(this.streamId)}`;
      this.metrics=[];this.seen=new Map();this.thinking='';
    }
    _record(kind,value=0){
      if(!root.__DEV__&&!root.__HERMES_DEV__&&!root.HERMES_DEV)return;
      this.metrics.push({kind,value:Number(value)||0,at:Date.now()});
      if(this.metrics.length>LIMIT)this.metrics.shift();
    }
    telemetry(){return this.metrics.map(x=>({...x}));}
    _find(){
      if(!this.root||!this.root.querySelectorAll)return null;
      return Array.from(this.root.querySelectorAll('.mars-process-window'))
        .find(n=>n.getAttribute('data-tool-worklog-key')==='live:'+this.streamId)||null;
    }
    bind(){
      if(this.mode!=='stream_window')return null;
      const frame=this._find();if(!frame)return null;
      const header=frame.querySelector('.mars-process-window-header');if(!header)return frame;
      if(header.getAttribute('data-activity-adapter-bound')!==this.key){
        header.setAttribute('data-activity-adapter-bound',this.key);
        // The existing inline handler owns toggling. Observe its final state.
        header.addEventListener('click',()=>{
          try{if(this.storage)this.storage.setItem(this.key,header.getAttribute('aria-expanded')==='true'?'true':'false');}catch(_){}
        });
        try{
          const saved=this.storage&&this.storage.getItem(this.key);
          if(saved==='true'||saved==='false'){
            header.setAttribute('aria-expanded',saved);
            frame.classList.toggle('is-collapsed',saved==='false');
          }
        }catch(_){}
      }
      return frame;
    }
    thinkingUpdate(text){
      // Keep only a fingerprint, never duplicate private reasoning in another DOM.
      const s=String(text||'');let hash=0;
      for(let i=Math.max(0,s.length-240);i<s.length;i++)hash=(Math.imul(hash,31)+s.charCodeAt(i))|0;
      const value=s.length+':'+hash;if(value===this.thinking)return false;
      this.thinking=value;this.bind();this._record('thinking');return true;
    }
    toolUpdate(tool){
      tool=tool||{};const id=tool.id||tool.tid||tool.tool_call_id;if(!id)return false;
      const key=String(id);const signature=JSON.stringify([tool.status,tool.result,tool.is_error,tool.duration]);
      if(this.seen.get(key)===signature){this._record('dedupe');return false;}
      this.seen.set(key,signature);
      if(this.seen.size>LIMIT)this.seen.delete(this.seen.keys().next().value);
      this.bind();this._record('tool');return true;
    }
    complete(summary){
      const frame=this.bind();if(!frame)return false;
      frame.setAttribute('data-complete','1');
      // Settled rendering owns the persisted summary; no ephemeral duplicate row.
      this._record('complete');return true;
    }
  }
  if(root)root.ActivityWindow=ActivityWindow;
  if(typeof module!=='undefined'&&module.exports)module.exports={ActivityWindow};
})(typeof window!=='undefined'?window:globalThis);
