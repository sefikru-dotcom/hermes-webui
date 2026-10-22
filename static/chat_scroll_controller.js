/* chat_scroll_controller.js — the single owner of every programmatic scrollTop write.
 *
 * Why this module exists
 * ----------------------
 * The chat pane used to have seven independent writers for `#messages.scrollTop`
 * (`_setMessageScrollToBottom`, `_settleMessageScrollToBottom`, `_settleFinalScroll`,
 * `scrollToBottom`/`_scrollToBottomSync`, the virtualization measurement compensation,
 * the render-snapshot restore and the scroll listener's own rAF). Each writer had its
 * own idea of which "is the reader still following?" guards to apply, so a gentle
 * trackpad wheel could be honoured by one path and overridden by another in the same
 * frame — the reader got yanked back to the bottom mid-read.
 *
 * This controller gives the transcript ONE writer with ONE guard and ONE frame budget:
 *
 *   - at most one read (`scrollHeight`) + one write (`scrollTop`) per animation frame;
 *     any further request inside that frame is coalesced into the next frame instead
 *     of forcing another layout;
 *   - a write is skipped entirely when the viewport is already at the bottom
 *     (`distance <= slop`), so an unchanged re-render performs ZERO scroll writes;
 *   - every deferred settle callback (ResizeObserver / quiet timer / final fallback)
 *     is bound to a monotonic generation. `cancel()` and a newer `settle()` bump the
 *     generation, so a stale callback can never write again — it only records itself
 *     in `stats().staleCancelled` and disconnects;
 *   - a single ResizeObserver instance is created once and reused for every settle
 *     (no observer-per-settle churn, which caused visible double-repaint on mobile);
 *   - the follow decision is delegated to one injected predicate (`canFollow`), so
 *     wheel / keyboard / touch / scrollbar / non-message intent and the sticky-unpin
 *     state are evaluated identically by every path.
 *
 * The module is DOM-agnostic: everything it touches comes from the injected `env`,
 * which makes it testable in node with a plain object as the scroller.
 *
 * Contract (see tests/test_upgrade_render_scroll.py + scripts/test_upgrade_render_scroll.mjs)
 * ------------------------------------------------------------------------------------------
 *   create(env) -> controller
 *   env: {
 *     getScroller()  -> element with scrollTop/scrollHeight/clientHeight
 *     getContent()   -> the growing content node (defaults to the scroller)
 *     canFollow({explicit, reason}) -> boolean   // the ONE guard
 *     onWrite({reason, explicit, scrollTop, max}) -> void
 *     now(), raf(cb), caf(id), setTimeout(cb,ms), clearTimeout(id), makeObserver(cb)
 *     slop, quietMs, finalMs
 *   }
 *   controller.write(reason, {immediate, explicit, force}) -> boolean (wrote now)
 *   controller.settle({force, explicit, reason}) -> void
 *   controller.cancel(reason) -> void
 *   controller.stats() -> {writes, reads, noop, refused, coalesced, staleCancelled,
 *                          settles, cancels, generation, observing, pendingWrite}
 */
(function(root, factory){
  var api=factory();
  if(typeof module!=='undefined'&&module.exports) module.exports=api;
  if(root) root.ChatScrollController=api;
})(typeof window!=='undefined'?window:null, function(){
  'use strict';

  var DEFAULTS={slop:1, quietMs:300, finalMs:2000};

  function create(env){
    var e=env||{};
    var slop=typeof e.slop==='number'?e.slop:DEFAULTS.slop;
    var quietMs=typeof e.quietMs==='number'?e.quietMs:DEFAULTS.quietMs;
    var finalMs=typeof e.finalMs==='number'?e.finalMs:DEFAULTS.finalMs;
    var now=e.now||function(){ return Date.now(); };
    var raf=e.raf||function(cb){ return setTimeout(function(){ cb(now()); },16); };
    var caf=e.caf||function(id){ clearTimeout(id); };
    var setT=e.setTimeout||function(cb,ms){ return setTimeout(cb,ms); };
    var clrT=e.clearTimeout||function(id){ clearTimeout(id); };
    var makeRO=e.makeObserver||function(cb){ return new ResizeObserver(cb); };

    var st={writes:0, reads:0, noop:0, refused:0, coalesced:0, staleCancelled:0,
            settles:0, cancels:0, resets:0, frames:0};

    var gen=0;              // monotonic settle generation
    var attemptedFrame=false; // a read+write attempt already happened this frame
    var frameRaf=0;         // re-opens the frame budget
    var pendingRaf=0;       // coalesced write scheduled for the next frame
    var pendingReason='';
    var pendingExplicit=false;
    var pendingForce=false;  // sticky inside the frame; survives coalescing
    var ro=null;            // ONE reused observer
    var roObserved=false;
    var roGen=0;            // generation the active observation belongs to
    var quietTimer=0;
    var finalTimer=0;
    var lastWriteAt=0;

    function scroller(){ try{ return e.getScroller?e.getScroller():null; }catch(_){ return null; } }
    function content(){ try{ return e.getContent?e.getContent():scroller(); }catch(_){ return scroller(); } }

    function markAttempt(){
      attemptedFrame=true;
      if(!frameRaf){
        frameRaf=raf(function(){
          frameRaf=0;
          attemptedFrame=false;
          st.frames++;
        });
      }
    }

    function allowed(explicit, reason){
      if(!e.canFollow) return true;
      try{ return !!e.canFollow({explicit:!!explicit, reason:reason||''}); }
      catch(_){ return false; }
    }

    /* One read + at most one write. Returns true when a write happened. */
    function attempt(reason, opts){
      opts=opts||{};
      var el=scroller();
      if(!el) return false;
      if(!opts.force && !allowed(opts.explicit, reason)){ st.refused++; return false; }
      st.reads++;
      var sh=Number(el.scrollHeight)||0;
      var ch=Number(el.clientHeight)||0;
      var max=sh-ch;
      if(!(max>0)) max=0;
      var cur=Number(el.scrollTop)||0;
      if(max-cur<=slop){ st.noop++; return false; }
      el.scrollTop=max;
      st.writes++;
      lastWriteAt=now();
      if(e.onWrite){ try{ e.onWrite({reason:reason||'', explicit:!!opts.explicit, scrollTop:max, max:max}); }catch(_){ } }
      return true;
    }

    function cancelPending(){
      if(!pendingRaf) return;
      caf(pendingRaf);
      pendingRaf=0;
      pendingReason='';
      pendingExplicit=false;
      pendingForce=false;
      st.staleCancelled++;
    }

    function schedule(reason, explicit, force){
      pendingReason=reason||'coalesced';
      pendingExplicit=!!explicit;
      // `force` is STICKY inside the frame: a coalesced forced request must not be
      // downgraded by a later unforced one, and the deferred attempt must keep it —
      // otherwise the reader-unpinned guard refuses a scroll the caller explicitly
      // demanded (explicit return to the bottom).
      if(force) pendingForce=true;
      if(pendingRaf){ st.coalesced++; return; }
      pendingRaf=raf(function(){
        pendingRaf=0;
        var r=pendingReason, ex=pendingExplicit, fo=pendingForce;
        pendingReason=''; pendingExplicit=false; pendingForce=false;
        if(attemptedFrame){ st.coalesced++; return; }
        markAttempt();
        attempt(r,{explicit:ex, force:fo});
      });
    }

    function write(reason, opts){
      opts=opts||{};
      if(attemptedFrame){
        // The frame budget is spent: collapse into the next frame instead of
        // forcing a second layout pass in the same frame.
        st.coalesced++;
        schedule(reason, opts.explicit, opts.force);
        return false;
      }
      if(opts.immediate){
        markAttempt();
        return attempt(reason, opts);
      }
      schedule(reason, opts.explicit, opts.force);
      return false;
    }

    function clearTimers(){
      if(quietTimer){ clrT(quietTimer); quietTimer=0; }
      if(finalTimer){ clrT(finalTimer); finalTimer=0; }
    }

    function ensureObserver(){
      if(ro) return ro;
      ro=makeRO(function(){
        onResize();
      });
      return ro;
    }

    function disconnectObserver(){
      if(ro&&roObserved){
        try{ ro.disconnect(); }catch(_){ }
      }
      roObserved=false;
    }

    function observe(target, extra){
      if(!target||!target.nodeType&&!target.addEventListener&&!target.tagName) return false;
      var obs=ensureObserver();
      if(!obs) return false;
      try{
        obs.observe(target);
        if(extra) obs.observe(extra);
      }catch(_){ return false; }
      roObserved=true;
      return true;
    }

    function onResize(){
      if(roGen!==gen){
        // A superseded settle: never write, just stop observing.
        st.staleCancelled++;
        disconnectObserver();
        return;
      }
      if(!allowed(currentExplicit,'resize')){
        st.refused++;
        disconnectObserver();
        return;
      }
      write('resize',{explicit:currentExplicit});
      armQuiet(roGen);
    }

    var currentExplicit=false;

    function armQuiet(myGen){
      if(quietTimer){ clrT(quietTimer); }
      quietTimer=setT(function(){
        quietTimer=0;
        if(myGen!==gen){ st.staleCancelled++; return; }
        disconnectObserver();
        write('quiet',{immediate:true, explicit:currentExplicit});
      },quietMs);
    }

    function armFinal(myGen){
      if(finalTimer){ clrT(finalTimer); }
      finalTimer=setT(function(){
        finalTimer=0;
        if(myGen!==gen){ st.staleCancelled++; return; }
        disconnectObserver();
        write('final',{immediate:true, explicit:currentExplicit});
      },finalMs);
    }

    function settle(opts){
      opts=opts||{};
      var explicit=!!opts.explicit;
      var force=!!opts.force;
      gen++;
      var myGen=gen;
      roGen=myGen;
      currentExplicit=explicit;
      clearTimers();
      cancelPending();
      st.settles++;

      // Immediate anchor: the caller just re-rendered / started following and wants
      // the viewport at the bottom before the browser paints.
      write(opts.reason||'settle',{immediate:true, explicit:explicit, force:force?true:undefined});
      if(force) return;

      var target=content();
      var el=scroller();
      if(!target) return;
      if(!observe(target, explicit&&el&&el!==target?el:null)) return;
      armQuiet(myGen);
      armFinal(myGen);
    }

    function cancel(reason){
      gen++;
      roGen=gen;
      clearTimers();
      cancelPending();
      disconnectObserver();
      st.cancels++;
      if(reason==='reset') st.resets++;
    }

    function stats(){
      var out={};
      for(var k in st) if(Object.prototype.hasOwnProperty.call(st,k)) out[k]=st[k];
      out.generation=gen;
      out.observing=!!roObserved;
      out.pendingWrite=!!pendingRaf;
      out.lastWriteAt=lastWriteAt;
      return out;
    }

    return {
      write:write,
      settle:settle,
      cancel:cancel,
      stats:stats,
      generation:function(){ return gen; },
      isObserving:function(){ return !!roObserved; },
      // Test/DI seam: lets a harness swap the scroller or guard without rebuilding.
      _env:e
    };
  }

  return {create:create, DEFAULTS:DEFAULTS};
});
