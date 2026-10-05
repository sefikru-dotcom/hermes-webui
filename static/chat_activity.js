/* Chat ActivityWindow: stable live-turn activity surface.
 * This module is dependency-free and can be loaded before ui/messages.
 * It owns one live window per session+turn, reconnect-safe event application,
 * disclosure persistence, and dev-only render telemetry.
 */
(function(root){
  'use strict';
  const hasDOM=!!(root&&root.document);
  const esc=(v)=>String(v==null?'':v).replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const keyPart=(v)=>String(v==null?'':v).replace(/[^a-zA-Z0-9_.:-]/g,'_');
  const storage=()=>{ try{return root&&root.sessionStorage||null;}catch(_){return null;} };
  const now=()=>Date.now();
  const telemetryEnabled=()=>!!(root&&root.__HERMES_DEV__||root&&root.__DEV__||root&&root.HERMES_DEV);
  const safeJson=(v)=>{try{return JSON.parse(v);}catch(_){return null;}};

  class ActivityWindow {
    constructor(opts){
      opts=opts||{};
      this.document=opts.document||root.document;
      this.sessionId=String(opts.sessionId||'');
      this.turnId=String(opts.turnId||opts.streamId||'');
      this.root=opts.root||null;
      this.storage=opts.storage||storage();
      this.mode=opts.mode||'stream_window';
      this.startedAt=Number(opts.startedAt)||now();
      this.events=new Map();
      this.eventOrder=[];
      this.disclosureKey=`hermes:activity:disclosure:${keyPart(this.sessionId)}:${keyPart(this.turnId)}`;
      this._lastRenderAt=0;
      this._telemetry=[];
    }
    static key(sessionId,turnId){return `${String(sessionId||'')}:${String(turnId||'')}`;}
    _loadDisclosure(){
      const raw=this.storage&&this.storage.getItem(this.disclosureKey);
      const parsed=safeJson(raw);
      return parsed&&typeof parsed==='object'?parsed:{};
    }
    _saveDisclosure(value){try{if(this.storage)this.storage.setItem(this.disclosureKey,JSON.stringify({open:!!value}));}catch(_){} }
    _findWindow(){
      if(!this.root||!this.root.querySelector)return null;
      const windows=this.root.querySelectorAll?Array.from(this.root.querySelectorAll('[data-activity-window="1"]')):[];
      if(windows.length) return windows.find((node)=>String(node.getAttribute('data-session-id')||'')===this.sessionId&&String(node.getAttribute('data-turn-id')||'')===this.turnId)||null;
      const candidate=this.root.querySelector('[data-activity-window="1"]');
      return candidate&&String(candidate.getAttribute('data-session-id')||'')===this.sessionId&&String(candidate.getAttribute('data-turn-id')||'')===this.turnId?candidate:null;
    }
    ensure(){
      if(this.mode==='stream_quiet'||this.mode==='hide_all_activity') return null;
      let win=this._findWindow();
      if(!win){
        win=this.document.createElement('section');
        win.className='mars-process-window activity-window';
        win.setAttribute('data-activity-window','1');
        win.setAttribute('data-session-id',this.sessionId);
        win.setAttribute('data-turn-id',this.turnId);
        win.innerHTML='<div class="mars-process-window-header" role="button" tabindex="0"><span class="mars-process-window-title">Activity</span><span class="mars-process-window-state">Working</span></div><div class="mars-process-window-stream activity-window-events" aria-live="polite"></div>';
        const header=win.firstElementChild;
        const toggle=()=>this.toggleDisclosure();
        if(header&&header.addEventListener){header.addEventListener('click',toggle);header.addEventListener('keydown',(e)=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}});}
        if(this.root)this.root.appendChild(win);
      }
      this._applyDisclosure(win); return win;
    }
    _applyDisclosure(win){
      const open=!!this._loadDisclosure();
      win.classList.toggle('is-collapsed',!open);
      win.setAttribute('data-disclosure-open',String(open));
    }
    toggleDisclosure(open){
      const win=this._findWindow(); if(!win)return false;
      const next=typeof open==='boolean'?open:win.getAttribute('data-disclosure-open')!=='true';
      this._saveDisclosure(next); this._applyDisclosure(win); return next;
    }
    _eventId(event){return String(event&& (event.id||event.event_id||event.tid||event.tool_call_id||event.row_id)||'');}
    apply(event){
      event=event||{}; const id=this._eventId(event);
      if(!id||this.events.has(id)) return false;
      this.events.set(id,event); this.eventOrder.push(id); this.render(); return true;
    }
    reconnect(snapshot){
      const list=Array.isArray(snapshot)?snapshot:(snapshot&&snapshot.events)||[];
      let added=0,duplicates=0; for(const event of list){if(this.apply(event))added++;else duplicates++;}
      if(telemetryEnabled())this._telemetry.push({duration_ms:0,dom_events:0,scroll_correction:0,reconnect_dedupe:duplicates});
      return added;
    }
    updateThinking(text){
      const id='thinking:'+this.turnId;
      if(!String(text||'').trim())return false;
      return this.apply({id,type:'thinking',text:String(text)});
    }
    addTool(tool){
      tool=tool||{}; return this.apply({id:tool.id||tool.tid||tool.tool_call_id,type:'tool',name:tool.name||'tool',status:tool.status||'running',duration:tool.duration,text:tool.text||''});
    }
    complete(summary){
      const win=this._findWindow(); if(!win)return false;
      const state=win.querySelector('.mars-process-window-state'); if(state)state.textContent='Done';
      win.setAttribute('data-complete','1');
      if(summary){const node=this.document.createElement('div');node.className='activity-summary';node.textContent=String(summary);const events=win.querySelector('.activity-window-events');if(events)events.appendChild(node);}
      this.render(); return true;
    }
    render(){
      const started=now(); const win=this.ensure(); if(!win)return false;
      const list=win.querySelector('.activity-window-events'); if(!list)return false;
      const atBottom=list.scrollHeight-list.scrollTop-list.clientHeight<24;
      const before=list.scrollHeight;
      list.innerHTML='';
      for(const id of this.eventOrder){const e=this.events.get(id);const row=this.document.createElement('div');row.className=`activity-event activity-event-${esc(e.type||'event')}`;row.setAttribute('data-event-id',id);row.textContent=e.type==='tool'?`${e.status==='done'?'✓':'•'} ${e.name}`:e.text||e.type||'Activity';list.appendChild(row);}
      if(atBottom)list.scrollTop=list.scrollHeight;
      if(telemetryEnabled())this._telemetry.push({duration_ms:now()-started,dom_events:list.children.length,scroll_correction:atBottom?1:0,reconnect_dedupe:0});
      this._lastRenderAt=now(); return true;
    }
    telemetry(){return telemetryEnabled()?this._telemetry.slice():[];}
  }
  if(root){root.ActivityWindow=ActivityWindow;root.HermesActivity={ActivityWindow};}
  if(typeof module!=='undefined'&&module.exports)module.exports={ActivityWindow};
})(typeof window!=='undefined'?window:globalThis);
