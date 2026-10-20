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
// confirmed missing). Tab reordering (`_applyTabOrder`, the inline restore
// script) inserts every tab before the `.rail-spacer` anchor, so the dividers
// would otherwise be left behind at the top of the rail and the groups would
// vanish. The separators are therefore re-anchored to their group on every
// pass: the divider that opens a group is placed before that group's first
// visible tab, and a divider whose group (or the group above it) is empty is
// hidden rather than left dangling.
(function(){
  'use strict';

  var RAIL = '.rail';
  var TAB = '.nav-tab[data-panel]';
  var HIDDEN_CLASS = 'rail-divider-hidden';

  // Group contract, mirrored from the markup comment in index.html. Order is
  // the visual order; `settings` is not a group (it sits below `.rail-spacer`).
  var GROUPS = [
    ['chat', 'tasks', 'kanban', 'todos'],
    ['projects', 'workspaces', 'memory', 'skills', 'profiles'],
    ['music'],
    ['insights', 'logs']
  ];

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

  function _groupOf(panel){
    for(var g = 0; g < GROUPS.length; g++){
      if(GROUPS[g].indexOf(panel) !== -1) return g;
    }
    return -1;
  }

  function _hasVisibleGroup(kids){
    for(var i = 0; i < kids.length; i++){
      var k = kids[i];
      if(k.matches && k.matches(TAB) && _isUsable(k) && _groupOf(k.dataset.panel) !== -1) return true;
    }
    return false;
  }

  // Tab reordering (`_applyTabOrder` and the inline restore script) inserts
  // every tab before `.rail-spacer`, which leaves the dividers behind at the
  // top of the rail. Re-anchor each divider to the group transition it
  // separates instead of trusting its previous position: walk the visible tabs
  // in DOM order and drop a separator wherever the group index rises. That
  // keeps a user's custom order intact while the separators stay between
  // groups, and it degrades to just hiding when a group is empty.
  function syncRailDividers(){
    var rail = _rail();
    if(!rail) return;
    var kids = Array.prototype.slice.call(rail.children);
    var dividers = kids.filter(function(k){
      return k.classList && k.classList.contains('rail-divider');
    });
    if(!dividers.length) return;

    var spacer = null;
    for(var s = 0; s < kids.length; s++){
      if(kids[s].classList && kids[s].classList.contains('rail-spacer')){ spacer = kids[s]; break; }
    }

    // Insertion points, in DOM order: one before the first tab of each new
    // group, then one before the bottom slot that closes the last group.
    var anchors = [];
    var previousGroup = -1;
    for(var t = 0; t < kids.length; t++){
      var tab = kids[t];
      if(!tab.matches || !tab.matches(TAB) || !_isUsable(tab)) continue;
      var group = _groupOf(tab.dataset.panel);
      if(group === -1) continue;
      if(previousGroup !== -1 && group > previousGroup) anchors.push(tab);
      previousGroup = group;
    }
    if(spacer && _hasVisibleGroup(kids)) anchors.push(spacer);

    for(var n = 0; n < dividers.length; n++){
      var divider = dividers[n];
      var anchor = anchors[n] || null;
      divider.classList.toggle(HIDDEN_CLASS, !anchor);
      // insertBefore on a node that already sits right before the anchor still
      // detaches and re-attaches it, which emits another childList record and
      // would drive the observer in circles. Compare positions first so a
      // settled rail produces no mutations at all.
      if(anchor && anchor.parentNode === rail && divider.nextElementSibling !== anchor){
        rail.insertBefore(divider, anchor);
      }
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
