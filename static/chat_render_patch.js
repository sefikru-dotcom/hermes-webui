/* chat_render_patch.js — keyed, in-place reconciliation for the transcript rows.
 *
 * Why this module exists
 * ----------------------
 * `renderMessages()` used to start with `#msgInner.innerHTML=''` on EVERY call
 * (33 call sites, several of them mid-stream). Emptying the transcript collapses its
 * height for the duration of the rebuild, so the browser is forced to clamp
 * `#messages.scrollTop` — and that clamp fires a `scroll` event that is
 * indistinguishable from a reader gesture. The whole `_programmaticScroll` /
 * settle-pass / anchor-snapshot apparatus existed to paper over that clamp, and was
 * itself a second source of jumps.
 *
 * This module replaces the wipe with a cursor-based keyed patch:
 *
 *   var ctx = ChatRenderPatch.begin(inner);      // snapshot old children by key
 *   ...build rows and call ChatRenderPatch.place(ctx, row) in document order...
 *   ChatRenderPatch.finish(ctx);                 // drop rows that no longer exist
 *
 * Properties the transcript depends on:
 *   - the container is never emptied, so `scrollHeight` never collapses and the
 *     browser never clamps `scrollTop`;
 *   - a row whose freshly built node is IDENTICAL (`isEqualNode`) to the live node
 *     is kept as-is: no re-parse, no Prism/KaTeX re-run, no <img> reload, no
 *     disclosure state loss, no layout invalidation;
 *   - a row whose content changed is replaced in place (same slot) instead of being
 *     re-appended, so rows above/below keep their DOM identity;
 *   - a row that disappeared is removed at `finish()`;
 *   - `place()` accepts an explicit `{reuse:false}` for nodes that will keep growing
 *     after placement (the live assistant turn appends segments post-placement), so
 *     a stale-but-equal shell is never reused for a subtree that is still mutating.
 *
 * Keys: `data-render-key` on the row, falling back to `data-recycle-key` and `id`.
 * Unkeyed nodes are always replaced (never reused) — they cannot be matched safely.
 *
 * The module is DOM-agnostic (works against any object implementing the small
 * Element surface: children/appendChild/insertBefore/replaceChild/remove/
 * isEqualNode/getAttribute/hasAttribute) so it is unit-testable in node.
 */
(function(root, factory){
  var api=factory();
  if(typeof module!=='undefined'&&module.exports) module.exports=api;
  if(root) root.ChatRenderPatch = api;
})(typeof window!=='undefined'?window:null, function(){
  'use strict';

  var KEY_ATTR='data-render-key';
  var NO_REUSE_ATTR='data-render-no-reuse';

  function keyOf(node){
    if(!node||node.nodeType!==1) return '';
    try{
      return node.getAttribute(KEY_ATTR)||node.getAttribute('data-recycle-key')||node.id||'';
    }catch(_){ return ''; }
  }

  function children(container){
    var out=[];
    if(!container||!container.childNodes) return out;
    var list=container.childNodes;
    for(var i=0;i<list.length;i++){
      if(list[i]&&list[i].nodeType===1) out.push(list[i]);
    }
    return out;
  }

  function equal(a,b){
    if(a===b) return true;
    if(!a||!b) return false;
    if(typeof a.isEqualNode==='function'){
      try{ return a.isEqualNode(b); }catch(_){ /* fall through */ }
    }
    try{ return String(a.outerHTML||'')===String(b.outerHTML||''); }catch(_){ return false; }
  }

  function begin(container){
    var old=children(container);
    var byKey=new Map();
    var used=new Set();
    for(var i=0;i<old.length;i++){
      var k=keyOf(old[i]);
      if(k&&!byKey.has(k)) byKey.set(k,old[i]);
    }
    return {
      container:container,
      old:old,
      byKey:byKey,
      used:used,
      last:null,
      placed:0,
      stats:{reused:0, replaced:0, inserted:0, moved:0, removed:0, clears:0, unkeyed:0}
    };
  }

  function positionOf(ctx,node){
    // True when `node` already sits in the slot right after ctx.last.
    var anchor=ctx.last;
    if(!anchor) return ctx.container && ctx.container.firstChild===node;
    return anchor.nextSibling===node;
  }

  function insertAtCursor(ctx,node){
    var anchor=ctx.last;
    if(!ctx.container) return;
    if(!anchor){ ctx.container.insertBefore(node,ctx.container.firstChild); return; }
    ctx.container.insertBefore(node,anchor.nextSibling);
  }

  function place(ctx, node, opts){
    if(!ctx||!node) return node;
    opts=opts||{};
    var k=keyOf(node);
    var existing=k?ctx.byKey.get(k):null;
    if(existing&&ctx.used.has(existing)) existing=null;
    if(!k) ctx.stats.unkeyed++;

    // Same node re-appended (virtualization recycle stash hands back the live node).
    if(existing===node){
      ctx.used.add(node);
      if(!positionOf(ctx,node)){ insertAtCursor(ctx,node); ctx.stats.moved++; }
      ctx.last=node;
      ctx.placed++;
      return node;
    }

    var reuseAllowed=opts.reuse!==false && !(node.hasAttribute&&node.hasAttribute(NO_REUSE_ATTR));
    if(existing&&reuseAllowed&&equal(existing,node)){
      // Byte-identical row: keep the live node, skip the whole subtree.
      ctx.used.add(existing);
      if(!positionOf(ctx,existing)){ insertAtCursor(ctx,existing); ctx.stats.moved++; }
      ctx.last=existing;
      ctx.placed++;
      ctx.stats.reused++;
      return existing;
    }

    if(existing&&positionOf(ctx,existing)){
      // Changed row in its original slot: swap in place so neighbours keep identity.
      ctx.container.replaceChild(node,existing);
      ctx.stats.replaced++;
    }else{
      insertAtCursor(ctx,node);
      if(existing) ctx.stats.replaced++;
      else ctx.stats.inserted++;
    }
    ctx.last=node;
    ctx.placed++;
    return node;
  }

  function finish(ctx){
    if(!ctx) return {reused:0,replaced:0,inserted:0,moved:0,removed:0,clears:0,unkeyed:0,placed:0};
    var removed=0;
    for(var i=0;i<ctx.old.length;i++){
      var node=ctx.old[i];
      if(ctx.used.has(node)) continue;
      if(node.parentNode===ctx.container||(node.parentElement&&node.parentElement===ctx.container)){
        ctx.container.removeChild(node);
        removed++;
      }
    }
    ctx.stats.removed=removed;
    var out={};
    for(var k in ctx.stats) if(Object.prototype.hasOwnProperty.call(ctx.stats,k)) out[k]=ctx.stats[k];
    out.placed=ctx.placed;
    ctx.container=null; // release
    return out;
  }

  function changed(stats){
    if(!stats) return false;
    return !!(stats.inserted||stats.removed||stats.moved||stats.replaced);
  }

  return {
    begin:begin,
    place:place,
    finish:finish,
    keyOf:keyOf,
    changed:changed,
    KEY_ATTR:KEY_ATTR,
    NO_REUSE_ATTR:NO_REUSE_ATTR
  };
});
