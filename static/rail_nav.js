// Left rail: tablist keyboard navigation + group separator upkeep.
//
// The rail is a vertical `role="tablist"` (index.html). The ARIA tabs pattern
// expects ArrowUp/ArrowDown to move focus between tabs and Home/End to jump to
// the edges, with hidden tabs skipped. Tabs are plain buttons here, so focus
// only moves — the panel changes on Enter/Space through the button's own
// onclick, never on arrow navigation.
//
// The same module re-evaluates the `.rail-divider` separators after every
// visibility/order change (localStorage restore, settings chips, a section
// confirmed missing). Dividers are never moved: reordering inserts tab buttons
// before the `.rail-spacer` anchor, and a separator that ends up with no
// visible tab on a side is hidden rather than relocated.
(function(){
  'use strict';

  var RAIL = '.rail';
  var TAB = '.nav-tab[data-panel]';
  var HIDDEN_CLASS = 'rail-divider-hidden';

  // Resolve lazily: `switchPanel`/`$` come from panels.js/ui.js, which parse
  // after this file. Nothing here depends on load order.
  function _rail(){
    return document.querySelector('nav.rail[role="tablist"]') || document.querySelector(RAIL);
  }

  function _tabs(rail){
    return Array.prototype.slice.call((rail || _rail() || document).querySelectorAll(TAB));
  }

  function _isUsable(tab){
    if(!tab || tab.disabled) return false;
    if(tab.getAttribute('aria-disabled') === 'true') return false;
    if(tab.hidden) return false;
    if(tab.classList.contains('nav-tab-hidden')) return false;
    // Generated tabs can be display:none'd by a stylesheet without a class.
    if(typeof tab.getClientRects === 'function' && tab.getClientRects().length === 0) return false;
    return true;
  }

  function _visibleTabs(rail){
    return _tabs(rail).filter(_isUsable);
  }

  // A divider separates two groups, so it is only useful while the group on
  // each side has a visible tab. Hidden tabs stay in the DOM, so an emptied
  // group is a real state; a divider left with an empty side is hidden rather
  // than relocated.
  //
  // The group above a divider is the one the previous divider (or the rail top)
  // opens; the group below is checked against the next divider. `settings` is
  // not a group: it sits in the bottom slot past `.rail-spacer`, so the last
  // divider only needs a group above it.
  function _groupTabAbove(kids, index){
    for(var j = index - 1; j >= 0; j--){
      var k = kids[j];
      if(k.matches && k.matches(TAB) && _isUsable(k)){
        return k.dataset.panel === 'settings' ? null : k;
      }
    }
    return null;
  }

  // A divider is visible only while the group it borders below has a tab. The
  // group above it is implied by the previous divider (or the top of the rail),
  // so one direction is enough and the pass stays idempotent.
  function syncRailDividers(){
    var rail = _rail();
    if(!rail) return;
    var kids = Array.prototype.slice.call(rail.children);
    var dividers = [];
    for(var d = 0; d < kids.length; d++){
      if(kids[d].classList && kids[d].classList.contains('rail-divider')) dividers.push(d);
    }
    for(var n = 0; n < dividers.length; n++){
      var i = dividers[n];
      var isLast = n === dividers.length - 1;
      // Group above: any usable non-settings tab before this divider.
      var hasAbove = _groupTabAbove(kids, i) !== null;
      // Group below: any usable non-settings tab before the next divider (or
      // the spacer for the last one).
      var limit = isLast ? kids.length : dividers[n + 1];
      var hasBelow = false;
      if(!isLast){
        for(var j = i + 1; j < limit; j++){
          var k = kids[j];
          if(k.matches && k.matches(TAB) && _isUsable(k) && k.dataset.panel !== 'settings'){ hasBelow = true; break; }
        }
      } else {
        hasBelow = true;
      }
      kids[i].classList.toggle(HIDDEN_CLASS, !(hasAbove && hasBelow));
    }
  }

  function _focusTab(tab){
    if(!tab || !tab.focus) return false;
    try { tab.focus({preventScroll: true}); } catch(_) { tab.focus(); }
    return true;
  }

  function _onKeyDown(e){
    if(!e || e.altKey || e.ctrlKey || e.metaKey) return;
    var rail = _rail();
    if(!rail) return;
    var target = e.target;
    var current = target && target.closest ? target.closest(TAB) : null;
    if(!current || !rail.contains(current)) return;
    // Never steal keys aimed at a field inside a tab (none today, but the
    // switcher must not break if one is added).
    if(target !== current && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName))) return;

    var tabs = _visibleTabs(rail);
    if(tabs.length < 2) return;
    var index = tabs.indexOf(current);
    if(index === -1) return;

    var next = null;
    if(e.key === 'ArrowDown' || e.key === 'Down') next = tabs[(index + 1) % tabs.length];
    else if(e.key === 'ArrowUp' || e.key === 'Up') next = tabs[(index - 1 + tabs.length) % tabs.length];
    else if(e.key === 'Home') next = tabs[0];
    else if(e.key === 'End') next = tabs[tabs.length - 1];
    if(!next || next === current) return;

    e.preventDefault();
    _focusTab(next);
  }

  // Bubble phase: a control inside the rail gets to handle the key first.
  document.addEventListener('keydown', _onKeyDown);

  // Each mutation below is the point where tab visibility/order is decided:
  // the inline boot script, panels.js _applyTabVisibility/_applyTabOrder and
  // _markSidebarTabHidden, and embedded_frames.js _markTab.
  function _observe(){
    var rail = _rail();
    if(!rail || typeof MutationObserver !== 'function') return;
    // The callback toggles classes on rail children, which re-enters this
    // observer. Ignore those records while the sync itself is running; the
    // first pass after a real visibility change still runs.
    var syncing = false;
    function run(){
      if(syncing) return;
      syncing = true;
      try { syncRailDividers(); } finally { syncing = false; }
    }
    var mo = new MutationObserver(function(){ run(); });
    mo.observe(rail, {childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden']});
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', function(){ _observe(); syncRailDividers(); });
  } else {
    _observe();
    syncRailDividers();
  }

  window.syncRailDividers = syncRailDividers;
})();
