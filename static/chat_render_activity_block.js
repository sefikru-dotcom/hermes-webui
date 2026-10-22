/* chat_activity_block.js — ONE activity block model for the live turn and for settled
 * turns, in every display mode (full / window / quiet).
 *
 * Why this module exists
 * ----------------------
 * Activity (thinking + tool worklog) was rendered by five different code paths:
 *   - the live turn built `.mars-process-window` chrome ONLY in `stream_window`;
 *     in `stream_full` it built a bare `.live-worklog` with no header at all;
 *   - the settled turn built `.agent-activity-group` with yet another header;
 *   - `stream_quiet` DESTROYED the live rows (`_hideLiveActivityForFinalAnswerOnly`
 *     calls `.remove()` on them), so switching quiet → full mid-stream showed nothing
 *     until the next SSE event re-created the rows;
 *   - disclosure (open/collapsed) was stored under `live:<streamId>` for the live turn
 *     and under `anchor-scene:<rawIdx>` for the settled turn, so the panel snapped shut
 *     at the live → settled boundary and after every re-render.
 *
 * This module gives both sides the same three things:
 *   1. `turnKey(...)` — one stable identity per turn (`<sid>:t<turn ordinal>`), derived
 *      identically for the live turn and for its settled form, so disclosure and mode
 *      survive the boundary;
 *   2. `ensureChrome(host, ...)` — one chrome builder (same classes, same header, same
 *      disclosure button) used by live and settled in full AND window mode. Mode only
 *      changes attributes/limits, never the presence of the shell;
 *   3. `setMode()` / `applyDisclosure()` / `setState()` / `syncVisibility()` — pure
 *      attribute-level updates. They NEVER remove rows: hiding is a display concern,
 *      so quiet → full restores instantly with no data loss and no new SSE event.
 *
 * Mode normalisation lives here too (`normalizeMode`) and is the single canonical
 * mapping — ui.js `chatActivityMode()` delegates to it, so legacy aliases
 * (`transparent_stream`, `hide_all_activity`, `compact_worklog`) are translated once.
 *
 * DOM-agnostic: the document is injected (`create({document, storage})`), so the module
 * is unit-testable in node against a minimal DOM.
 */
(function(root, factory){
  var api=factory();
  if(typeof module!=='undefined'&&module.exports) module.exports=api;
  if(root) root.ChatActivityBlock=api;
})(typeof window!=='undefined'?window:null, function(){
  'use strict';

  var MODES=['stream_full','stream_window','stream_quiet'];
  var LEGACY_MODES={
    transparent_stream:'stream_full',
    hide_all_activity:'stream_quiet',
    compact_worklog:'stream_window',
    full:'stream_full',
    window:'stream_window',
    quiet:'stream_quiet'
  };
  var DISCLOSURE_PREFIX='mars-activity-block:';
  var LEGACY_DISCLOSURE_PREFIX='hermes-activity-disclosure:';
  var WINDOW_BODY_MAX_PX=260;

  function normalizeMode(mode, fallback){
    var m=String(mode||'');
    if(MODES.indexOf(m)>=0) return m;
    if(LEGACY_MODES[m]) return LEGACY_MODES[m];
    return fallback||'stream_window';
  }

  /* Stable turn identity. Prefer the turn ordinal (index of the user message that
   * opened the turn): it is identical for the live turn and its settled form, and it
   * survives message-index shifts from virtualisation. */
  function turnKey(opts){
    opts=opts||{};
    var sid=opts.sessionId||'';
    var ordinal=opts.ordinal;
    if(ordinal!==undefined&&ordinal!==null&&ordinal!==''&&isFinite(Number(ordinal))&&Number(ordinal)>=0){
      return sid+':t'+Number(ordinal);
    }
    if(opts.rawIdx!==undefined&&opts.rawIdx!==null&&opts.rawIdx!==''&&isFinite(Number(opts.rawIdx))&&Number(opts.rawIdx)>=0){
      return sid+':r'+Number(opts.rawIdx);
    }
    if(opts.streamId) return sid+':s'+opts.streamId;
    return '';
  }

  function memoryStorage(){
    var map=new Map();
    return {
      getItem:function(k){ return map.has(k)?map.get(k):null; },
      setItem:function(k,v){ map.set(String(k),String(v)); },
      removeItem:function(k){ map.delete(k); }
    };
  }

  function create(env){
    env=env||{};
    var doc=env.document||(typeof document!=='undefined'?document:null);
    var store=env.storage||null;
    if(!store){
      try{ store=(typeof localStorage!=='undefined')?localStorage:memoryStorage(); }
      catch(_){ store=memoryStorage(); }
    }
    var legacyStore=env.legacyStorage||null;
    if(!legacyStore){
      try{ legacyStore=(typeof localStorage!=='undefined')?localStorage:null; }
      catch(_){ legacyStore=null; }
    }
    var mem=memoryStorage();

    function readStore(storage,key){
      try{
        if(!storage) return null;
        var v=storage.getItem(key);
        return v==='open'||v==='closed'?v:null;
      }catch(_){ return null; }
    }
    function writeStore(storage,key,open){
      try{ if(storage) storage.setItem(key,open?'open':'closed'); }catch(_){ }
    }

    // ── disclosure ────────────────────────────────────────────────────────
    var Disclosure={
      key:function(k){ return DISCLOSURE_PREFIX+k; },
      get:function(k){
        if(!k) return null;
        var v=readStore(store,DISCLOSURE_PREFIX+k);
        if(v) return v==='open';
        return mem.getItem(DISCLOSURE_PREFIX+k)==='1'?true:null;
      },
      set:function(k,open){
        if(!k) return;
        writeStore(store,DISCLOSURE_PREFIX+k,!!open);
        mem.setItem(DISCLOSURE_PREFIX+k,open?'1':'0');
      },
      toggle:function(k){
        var next=!Disclosure.get(k);
        Disclosure.set(k,next);
        return next;
      },
      clear:function(k){
        if(!k) return;
        try{ if(store) store.removeItem(DISCLOSURE_PREFIX+k); }catch(_){ }
        mem.removeItem(DISCLOSURE_PREFIX+k);
      },
      /* Copy a disclosure decision from a legacy key (`live:<streamId>`,
       * `anchor-scene:<rawIdx>`) onto the stable turn key, but only when the stable key
       * has no decision of its own — the user's newest click always wins. */
      adopt:function(k,legacyKeys){
        if(!k||!legacyKeys) return null;
        var own=Disclosure.get(k);
        if(own!==null) return own;
        var list=Array.isArray(legacyKeys)?legacyKeys:[legacyKeys];
        for(var i=0;i<list.length;i++){
          var lk=list[i];
          if(!lk) continue;
          var v=readStore(legacyStore,LEGACY_DISCLOSURE_PREFIX+lk);
          if(v){ var open=v==='open'; Disclosure.set(k,open); return open; }
        }
        return null;
      }
    };

    // ── chrome ────────────────────────────────────────────────────────────
    function findBlock(host,key){
      if(!host||!host.querySelectorAll) return null;
      var nodes=host.querySelectorAll('[data-activity-block="1"]');
      for(var i=0;i<nodes.length;i++){
        if(nodes[i].getAttribute('data-turn-key')===(key||'')) return nodes[i];
      }
      return null;
    }

    function ensureChrome(host, opts){
      opts=opts||{};
      if(!host) return null;
      var key=opts.turnKey||'';
      var mode=normalizeMode(opts.mode,'stream_window');
      var state=opts.state==='settled'?'settled':'live';
      var existing=findBlock(host,key);
      if(existing){
        var ebody=existing.querySelector('[data-activity-body="1"]')||existing.querySelector('.mars-process-window-stream');
        var eheader=existing.querySelector('.mars-process-window-header');
        var elist=(ebody&&ebody.querySelector('[data-activity-list="1"]'))||existing.querySelector('.tool-worklog-list');
        setMode(existing,mode);
        setState(existing,{state:state,label:opts.label,duration:opts.duration,count:opts.count});
        return {root:existing, header:eheader, body:ebody, list:elist, created:false};
      }
      var root=doc.createElement('div');
      root.className='mars-process-window';
      root.setAttribute('data-activity-block','1');
      root.setAttribute('data-turn-key',key);
      root.setAttribute('data-mode',mode);
      root.setAttribute('data-state',state);
      root.setAttribute('data-activity-expanded','1');
      root.innerHTML=
        '<button type="button" class="mars-process-window-header" aria-expanded="true" data-activity-toggle="1">'+
          '<span class="mars-process-window-title">'+
            '<span class="mars-quiet-status-spinner" data-activity-spinner="1"></span>'+
            '<span data-activity-label="1"></span>'+
          '</span>'+
          '<span class="mars-process-window-actions"><span class="mars-process-window-btn" data-activity-chevron="1">\u2304</span></span>'+
        '</button>'+
        '<div class="mars-process-window-stream live-worklog worklog" data-activity-body="1" data-live-worklog-shell="1">'+
          '<div class="tool-worklog-list" data-activity-list="1"></div>'+
        '</div>';
      // The legacy selectors the worklog builders look for must exist on the shell so
      // the existing row builders keep appending into the same list.
      var body=root.querySelector('[data-activity-body="1"]');
      body.setAttribute('data-live-tool-worklog-group','1');
      body.setAttribute('data-live-tool-call-group','1');
      body.setAttribute('data-live-activity-current','1');
      body.setAttribute('data-tool-worklog-group','1');
      body.setAttribute('data-tool-worklog-key',key);
      var anchor=opts.anchor||null;
      if(anchor&&anchor.parentNode===host) host.insertBefore(root,anchor.nextSibling);
      else host.appendChild(root);
      var header=root.querySelector('.mars-process-window-header');
      var list=root.querySelector('[data-activity-list="1"]');
      setMode(root,mode);
      setState(root,{state:state,label:opts.label,duration:opts.duration,count:opts.count});
      return {root:root, header:header, body:body, list:list, created:true};
    }

    function _applyDisplay(root){
      if(!root||!root.style) return;
      var quiet=root.getAttribute('data-mode')==='stream_quiet';
      var empty=root.getAttribute('data-activity-empty')==='1';
      var hidden=quiet||empty;
      root.setAttribute('data-activity-hidden',hidden?'1':'0');
      // Inline display is the JS-owned hide switch: it works even when the mode CSS
      // layer (`html.mars-mode-quiet`) was not applied yet, and it is the only way to
      // keep quiet -> full restoration instant without rebuilding rows.
      root.style.display=hidden?'none':'';
    }

    function setMode(root, mode){
      if(!root) return null;
      var m=normalizeMode(mode,'stream_window');
      var changed=root.getAttribute('data-mode')!==m;
      root.setAttribute('data-mode',m);
      if(root.classList){
        root.classList.remove('mars-activity-full');
        root.classList.remove('mars-activity-window');
        root.classList.remove('mars-activity-quiet');
        root.classList.add('mars-activity-'+m.replace('stream_',''));
      }
      var body=root.querySelector?root.querySelector('[data-activity-body="1"]'):null;
      if(body&&body.style){
        // window = bounded, scrollable preview; full = unbounded flow. Same shell, so
        // switching modes never detaches the rows the parser is writing into.
        body.style.maxHeight=m==='stream_window'?(WINDOW_BODY_MAX_PX+'px'):'';
        body.style.overflowY=m==='stream_window'?'auto':'';
      }
      _applyDisplay(root);
      return {mode:m, changed:changed};
    }

    function setState(root, opts){
      opts=opts||{};
      if(!root) return null;
      var state=opts.state==='settled'?'settled':'live';
      root.setAttribute('data-state',state);
      var spinner=root.querySelector?root.querySelector('[data-activity-spinner="1"]'):null;
      if(spinner&&spinner.style) spinner.style.display=state==='live'?'':'none';
      var label=root.querySelector?root.querySelector('[data-activity-label="1"]'):null;
      if(label&&opts.label!==undefined&&opts.label!==null) label.textContent=String(opts.label);
      var duration=opts.duration===undefined||opts.duration===null?'':String(opts.duration);
      if(duration) root.setAttribute('data-activity-duration',duration);
      if(opts.count!==undefined&&opts.count!==null) root.setAttribute('data-activity-count',String(opts.count));
      return {state:state};
    }

    function applyDisclosure(root, open){
      if(!root) return;
      root.setAttribute('data-activity-expanded',open?'1':'0');
      if(root.classList) root.classList.toggle('is-collapsed',!open);
      var header=root.querySelector?root.querySelector('.mars-process-window-header'):null;
      if(header) header.setAttribute('aria-expanded',open?'true':'false');
      var chevron=root.querySelector?root.querySelector('[data-activity-chevron="1"]'):null;
      if(chevron) chevron.textContent=open?'\u2304':'\u203a';
    }

    /* Disclosure resolution for a block: explicit user decision for the stable turn key
     * first, then legacy keys, then the mode default (full = open, window = open,
     * quiet = open so switching back shows the same rows). */
    function disclosureFor(root, opts){
      opts=opts||{};
      var key=opts.turnKey||(root&&root.getAttribute?root.getAttribute('data-turn-key'):'')||'';
      var saved=Disclosure.adopt(key, opts.legacyKeys);
      var open=saved===null?true:!!saved;
      applyDisclosure(root, open);
      return open;
    }

    function setDisclosure(root, open, opts){
      opts=opts||{};
      var key=opts.turnKey||(root&&root.getAttribute?root.getAttribute('data-turn-key'):'')||'';
      if(key&&opts.persist!==false) Disclosure.set(key,!!open);
      applyDisclosure(root,!!open);
      return !!open;
    }

    function visibleRowCount(root){
      if(!root||!root.querySelectorAll) return 0;
      var list=root.querySelector('[data-activity-list="1"]')||root;
      var rows=list.querySelectorAll('[data-activity-row],.tool-card-row,.agent-activity-thinking,.wl-reason,.transparent-event-row,.tool-worklog-group');
      var n=0;
      for(var i=0;i<rows.length;i++){
        if(rows[i].getAttribute&&rows[i].getAttribute('data-activity-empty')==='1') continue;
        n++;
      }
      return n;
    }

    /* A settled block with zero rows is a dead frame (audit P3): hide it entirely
     * instead of rendering an empty shell with a header. */
    function syncVisibility(root, opts){
      opts=opts||{};
      if(!root) return 0;
      var count=(opts.count===undefined||opts.count===null)?visibleRowCount(root):Number(opts.count);
      var empty=count<=0&&root.getAttribute('data-state')==='settled';
      root.setAttribute('data-activity-empty',empty?'1':'0');
      root.setAttribute('data-activity-count',String(count));
      _applyDisplay(root);
      return count;
    }

    return {
      normalizeMode:normalizeMode,
      turnKey:turnKey,
      Disclosure:Disclosure,
      ensureChrome:ensureChrome,
      setMode:setMode,
      setState:setState,
      applyDisclosure:applyDisclosure,
      disclosureFor:disclosureFor,
      setDisclosure:setDisclosure,
      syncVisibility:syncVisibility,
      visibleRowCount:visibleRowCount,
      WINDOW_BODY_MAX_PX:WINDOW_BODY_MAX_PX,
      MODES:MODES
    };
  }

  return {
    create:create,
    normalizeMode:normalizeMode,
    turnKey:turnKey,
    MODES:MODES,
    WINDOW_BODY_MAX_PX:WINDOW_BODY_MAX_PX
  };
});
