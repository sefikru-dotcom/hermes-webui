// ── Command palette (Ctrl/Cmd+Shift+P) ───────────────────────────────────────
//
// A VS Code / Raycast style command launcher: the overlay, its rows and its
// keyboard model are owned here, the *actions* are not. Every command calls an
// existing global (switchPanel, newSession, _pickTheme, …) instead of
// re-implementing panel or chat behaviour, so there is exactly one code path
// per action.
(function(){
  'use strict';

  // Top-level `function` declarations of the classic scripts (switchPanel,
  // newSession, _pickTheme, …) land on window; `typeof` guards cover the case
  // where this file somehow evaluates before them.
  function _fn(name){
    return typeof window[name] === 'function' ? window[name] : null;
  }

  // There is no shared focusComposer() helper; #msg is the composer on every
  // panel, and boot.js focuses it directly (btnNewChat, Ctrl+K).
  function _focusComposer(){
    var composer = document.getElementById('msg');
    if(composer) composer.focus();
  }

  function _t(key, ...args){
    var translate = _fn('t');
    if(translate) return translate(key, ...args);
    return key;
  }

  // Lets keyboard handling win over the composer/global shortcuts while the
  // palette is modal. boot.js owns the global key listeners; this flag is what
  // its Escape handling and Ctrl/Cmd+K path check.
  window._paletteOwnsKeyboard = false;

  var GROUP_ORDER = ['navigation', 'actions', 'view'];
  var GROUPS = {
    navigation: {title: 'palette_group_navigation', badge: 'NAV'},
    actions:    {title: 'palette_group_actions',    badge: 'ACT'},
    view:       {title: 'palette_group_view',       badge: 'VIEW'}
  };

  // Panel names → locale keys. Keyed by the `data-panel` value so the list can
  // be checked against the actual rail markup (tests/test_command_palette.py).
  var PANEL_TITLE_KEYS = {
    chat: 'tab_chat',
    tasks: 'tab_tasks',
    kanban: 'tab_kanban',
    projects: 'tab_projects',
    music: 'tab_music',
    skills: 'tab_skills',
    memory: 'tab_memory',
    workspaces: 'tab_workspaces',
    profiles: 'tab_profiles',
    todos: 'tab_todos',
    insights: 'tab_insights',
    logs: 'tab_logs',
    settings: 'tab_settings'
  };
  // The daily-driver panels are pinned to the top of the empty-query list;
  // everything else keeps the rail order (read from the DOM, see _panelCommands).
  var FEATURED_PANELS = ['chat'];
  var PANEL_KEYWORDS = {
    chat: 'conversation dialog messages',
    tasks: 'cron schedule automation jobs',
    kanban: 'board cards lane',
    projects: 'dashboard web apps',
    music: 'player audio tracks',
    skills: 'plugins extensions capabilities',
    memory: 'notes recall context',
    workspaces: 'spaces folders directories',
    profiles: 'agents personas',
    todos: 'checklist checklist tasks',
    insights: 'analytics metrics usage',
    logs: 'activity history events',
    settings: 'preferences configuration options'
  };

  function _panelCommands(){
    // Read the rail rather than hardcoding: hidden or reordered tabs must not
    // resurrect commands for surfaces the user cannot open.
    var seen = [];
    var nodes = document.querySelectorAll('.rail .nav-tab[data-panel]');
    Array.prototype.forEach.call(nodes, function(node){
      var panel = node.getAttribute('data-panel');
      if(!panel || seen.indexOf(panel) !== -1) return;
      if(!PANEL_TITLE_KEYS[panel]) return; // extension panel with no locale name yet
      seen.push(panel);
    });
    var ordered = FEATURED_PANELS.filter(function(p){ return seen.indexOf(p) !== -1; });
    seen.forEach(function(p){ if(ordered.indexOf(p) === -1) ordered.push(p); });
    return ordered.map(function(panel){
      var label = _t(PANEL_TITLE_KEYS[panel]);
      return {
        id: 'panel:' + panel,
        title: _t('palette_open_panel', label),
        group: 'navigation',
        keywords: (PANEL_KEYWORDS[panel] || '') + ' ' + label + ' ' + panel,
        run: function(){
          var switchPanel = _fn('switchPanel');
          if(switchPanel) switchPanel(panel, {fromRailClick: true});
        }
      };
    });
  }

  // Settings is both a rail panel and the Ctrl+, destination. toggleSettings()
  // is the canonical toggle (close when open, open when not), so the command
  // uses it when present.
  function _settingsCommand(){
    return {
      id: 'settings',
      title: _t('palette_open_settings'),
      group: 'navigation',
      keywords: 'preferences configuration options ctrl+,',
      hotkey: 'Ctrl+,',
      run: function(){
        var toggle = _fn('toggleSettings');
        if(toggle){ toggle(); return; }
        var switchPanel = _fn('switchPanel');
        if(switchPanel) switchPanel('settings');
      }
    };
  }

  function _newChatCommand(){
    return {
      id: 'new-chat',
      title: _t('palette_new_chat'),
      group: 'actions',
      keywords: 'new conversation ctrl+k',
      hotkey: 'Ctrl+K',
      // Same path as #btnNewChat / Ctrl+K minus the "+ means go to chat"
      // prelude: the palette itself is what the user reached for, so sending
      // them to a fresh composer afterwards is the whole point.
      run: function(){
        var ensure = _fn('_ensureChatPanelVisible');
        if(ensure) ensure();
        var create = _fn('newSession');
        if(create) return Promise.resolve(create(false)).then(function(){
          var render = _fn('renderSessionList');
          if(render) render();
          _focusComposer();
        });
      }
    };
  }

  function _searchCommand(){
    return {
      id: 'search-sessions',
      title: _t('palette_search_sessions'),
      group: 'actions',
      keywords: 'find filter conversations sidebar',
      run: function(){
        // The sidebar search is only visible on desktop and with the sidebar
        // expanded; asking for it should surface it instead of focusing a
        // hidden input.
        var expand = _fn('expandSidebar');
        if(expand) expand();
        var input = document.getElementById('sessionSearch');
        if(!input) return;
        input.focus();
        if(typeof input.select === 'function') input.select();
      }
    };
  }

  // Resolves the label of the theme currently in effect (light/dark/system).
  function _currentTheme(){
    try{
      var stored = String(localStorage.getItem('hermes-theme') || '').toLowerCase();
      if(stored === 'light' || stored === 'dark' || stored === 'system') return stored;
    }catch(_){}
    return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
  }

  function _themeCommands(){
    function pick(value){
      return function(){
        var pickTheme = _fn('_pickTheme');
        if(pickTheme) pickTheme(value);
      };
    }
    return [
      {
        id: 'theme-toggle',
        title: _t('palette_toggle_theme'),
        group: 'view',
        keywords: 'dark light appearance mode switch',
        run: function(){
          // A toggle from "system" has no meaningful opposite: resolve the
          // scheme that is on screen and flip it.
          var next = _currentTheme() === 'dark' ? 'light' : 'dark';
          pick(next)();
        }
      },
      {id: 'theme-light', title: _t('palette_theme_light'), group: 'view',
       keywords: 'light theme appearance', run: pick('light')},
      {id: 'theme-dark', title: _t('palette_theme_dark'), group: 'view',
       keywords: 'dark theme appearance', run: pick('dark')},
      {id: 'theme-system', title: _t('palette_theme_system'), group: 'view',
       keywords: 'auto system theme appearance', run: pick('system')}
    ];
  }

  function _sidebarCommand(){
    return {
      id: 'toggle-sidebar',
      title: _t('palette_toggle_sidebar'),
      group: 'view',
      keywords: 'collapse expand layout sidebar',
      run: function(){
        var toggle = _fn('toggleSidebar');
        if(toggle) toggle();
      }
    };
  }

  function _closeCommand(){
    return {
      id: 'palette-close',
      title: _t('palette_close'),
      group: 'view',
      keywords: 'dismiss escape',
      hotkey: 'Esc',
      run: close
    };
  }

  function _allCommands(){
    return _panelCommands()
      .concat([_settingsCommand(), _newChatCommand(), _searchCommand()])
      .concat(_themeCommands())
      .concat([_sidebarCommand(), _closeCommand()]);
  }

  function _fold(text){
    return String(text == null ? '' : text).toLowerCase();
  }

  // Every whitespace-separated token must match the haystack: "new convo"
  // finds the new-chat entry, "chat xyz" finds nothing.
  function _matches(command, tokens){
    var haystack = _fold(command.title + ' ' + (command.keywords || '') + ' ' + command.id);
    return tokens.every(function(token){ return haystack.indexOf(token) !== -1; });
  }

  function _filterCommands(tokens){
    var all = _allCommands();
    if(!tokens.length) return all;
    return all.filter(function(command){ return _matches(command, tokens); });
  }

  // ── DOM ────────────────────────────────────────────────────────────────────
  var _root = null;      // backdrop + dialog container
  var _input = null;
  var _list = null;
  var _empty = null;
  var _results = [];     // commands matching the current query
  var _active = 0;       // index into _results
  var _open = false;
  var _previousFocus = null;

  function _el(tag, attrs, text){
    var node = document.createElement(tag);
    if(attrs) Object.keys(attrs).forEach(function(name){ node.setAttribute(name, attrs[name]); });
    if(text != null) node.textContent = text;
    return node;
  }

  function _build(){
    _root = _el('div', {id: 'commandPalette', class: 'command-palette', hidden: ''});
    _root.addEventListener('mousedown', function(event){
      // Only a press that starts on the backdrop dismisses: pressing inside
      // the dialog and releasing on the backdrop must not close it.
      if(event.target === _root) close();
    });

    var dialog = _el('div', {
      id: 'commandPaletteDialog',
      class: 'command-palette-dialog',
      role: 'dialog',
      'aria-modal': 'true',
      'aria-label': _t('palette_aria_label')
    });

    var header = _el('div', {class: 'command-palette-header'});
    _input = _el('input', {
      id: 'commandPaletteInput',
      class: 'command-palette-input',
      type: 'text',
      role: 'combobox',
      'aria-expanded': 'true',
      'aria-controls': 'commandPaletteList',
      'aria-autocomplete': 'list',
      autocomplete: 'off',
      spellcheck: 'false',
      placeholder: _t('palette_placeholder')
    });
    _input.addEventListener('input', function(){
      _render(_input.value);
    });
    _input.addEventListener('keydown', _onKeydown);
    header.appendChild(_input);
    dialog.appendChild(header);

    _list = _el('div', {
      id: 'commandPaletteList',
      class: 'command-palette-list',
      role: 'listbox',
      'aria-label': _t('palette_aria_label')
    });
    _list.addEventListener('mousemove', function(event){
      var row = event.target.closest ? event.target.closest('[role="option"]') : null;
      if(!row) return;
      var index = Number(row.getAttribute('data-index'));
      // Pointer hover moves the cursor, but only when it really moves on to
      // another row — otherwise a moving list under a still pointer would
      // fight the keyboard.
      if(!isNaN(index) && index !== _active) _setActive(index, false);
    });
    _list.addEventListener('click', function(event){
      var row = event.target.closest ? event.target.closest('[role="option"]') : null;
      if(!row) return;
      var index = Number(row.getAttribute('data-index'));
      if(!isNaN(index)) _activate(index);
    });
    dialog.appendChild(_list);

    _empty = _el('div', {class: 'command-palette-empty', role: 'status'}, _t('palette_empty'));
    _empty.hidden = true;
    dialog.appendChild(_empty);

    dialog.addEventListener('keydown', function(event){
      if(event.key !== 'Tab') return;
      // Simple focus trap: the input is the only tab stop, so Tab and
      // Shift+Tab both stay in it instead of walking into the page behind.
      event.preventDefault();
      _input.focus();
    });

    _root.appendChild(dialog);
    document.body.appendChild(_root);
  }

  function _render(query){
    if(!_list) return;
    var tokens = _fold(query).split(/\s+/).filter(Boolean);
    _results = _filterCommands(tokens);
    _active = 0;
    _list.textContent = '';
    var renderedGroup = null;
    _results.forEach(function(command, index){
      var group = GROUPS[command.group];
      if(command.group !== renderedGroup){
        renderedGroup = command.group;
        var heading = _el('div', {
          class: 'command-palette-group',
          role: 'presentation',
          'aria-hidden': 'true'
        }, _t(group.title));
        heading.setAttribute('data-group', command.group);
        _list.appendChild(heading);
      }
      var option = _el('div', {class: 'command-palette-row', role: 'option'});
      option.setAttribute('data-index', String(index));
      option.setAttribute('data-command-id', command.id);
      option.setAttribute('aria-selected', index === 0 ? 'true' : 'false');
      option.id = 'commandPaletteOption' + index;
      option.appendChild(_el('span', {class: 'command-palette-badge', 'aria-hidden': 'true'}, group.badge));
      // The highlight map is computed against the command title only, so the
      // badge and hotkey (which may contain matching characters) stay intact.
      var title = _el('span', {class: 'command-palette-title'});
      _appendHighlighted(title, command.title, tokens);
      option.appendChild(title);
      if(command.hotkey){
        option.appendChild(_el('span', {class: 'command-palette-hotkey', 'aria-hidden': 'true'}, command.hotkey));
      }
      option.title = command.title;
      _list.appendChild(option);
    });
    _empty.hidden = _results.length !== 0;
    if(_results.length){
      _setActive(0, true);
      _input.setAttribute('aria-activedescendant', 'commandPaletteOption0');
    } else {
      _input.removeAttribute('aria-activedescendant');
    }
  }

  function _appendHighlighted(container, title, tokens){
    var lower = _fold(title);
    var hits = [];
    tokens.forEach(function(token){
      var from = 0;
      var at;
      while((at = lower.indexOf(token, from)) !== -1){
        hits.push([at, at + token.length]);
        from = at + token.length;
      }
    });
    if(!hits.length){ container.textContent = title; return; }
    hits.sort(function(a, b){ return a[0] - b[0]; });
    var merged = [];
    hits.forEach(function(range){
      var last = merged[merged.length - 1];
      if(last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
      else merged.push([range[0], range[1]]);
    });
    var cursor = 0;
    merged.forEach(function(range){
      if(range[0] > cursor) container.appendChild(document.createTextNode(title.slice(cursor, range[0])));
      container.appendChild(_el('mark', {class: 'command-palette-hit'}, title.slice(range[0], range[1])));
      cursor = range[1];
    });
    if(cursor < title.length) container.appendChild(document.createTextNode(title.slice(cursor)));
  }

  function _rows(){
    return _list ? _list.querySelectorAll('[role="option"]') : [];
  }

  function _setActive(index, scroll){
    var rows = _rows();
    if(!rows.length){ _active = 0; return; }
    _active = ((index % rows.length) + rows.length) % rows.length;
    Array.prototype.forEach.call(rows, function(row, i){
      row.setAttribute('aria-selected', i === _active ? 'true' : 'false');
    });
    var current = rows[_active];
    _input.setAttribute('aria-activedescendant', current.id);
    if(scroll && current.scrollIntoView) current.scrollIntoView({block: 'nearest'});
  }

  function _activate(index){
    var command = _results[index];
    if(!command) return;
    close();
    try{
      command.run();
    }catch(err){
      // A failing action must not strand the user behind a half-closed
      // overlay; the page console keeps the trace.
      console.error('command palette:', command.id, err);
    }
  }

  function _onKeydown(event){
    if(event.isComposing) return;
    switch(event.key){
      case 'ArrowDown':
        event.preventDefault();
        _setActive(_active + 1, true);
        return;
      case 'ArrowUp':
        event.preventDefault();
        _setActive(_active - 1, true);
        return;
      case 'Enter':
        event.preventDefault();
        _activate(_active);
        return;
      case 'Escape':
        // Swallow it so the composer's global Escape handler does not also
        // blur/close things behind the palette.
        event.preventDefault();
        event.stopPropagation();
        close();
        return;
    }
  }

  // ── Open / close ───────────────────────────────────────────────────────────
  function open(){
    if(_open) return;
    if(!_root) _build();
    _previousFocus = document.activeElement;
    _open = true;
    window._paletteOwnsKeyboard = true;
    _root.hidden = false;
    document.documentElement.classList.add('command-palette-open');
    _input.value = '';
    _render('');
    _input.focus();
  }

  function close(){
    if(!_open) return;
    _open = false;
    window._paletteOwnsKeyboard = false;
    if(_root) _root.hidden = true;
    document.documentElement.classList.remove('command-palette-open');
    // "Close" means give the user back what they were doing; the composer is only
    // the fallback when the element they opened the palette from is gone.
    var restoreFocus = _previousFocus && _previousFocus.focus && document.contains(_previousFocus);
    if(restoreFocus) _previousFocus.focus();
    else _focusComposer();
    _previousFocus = null;
  }

  function toggle(){
    if(_open) close();
    else open();
  }

  // Ctrl/Cmd+Shift+P is the palette itself. The chord is accepted on every
  // layout through `event.code`, and it fires in text fields too — Shift+P
  // is not a character anyone types into a message on purpose.
  function _isMod(event){
    return event.ctrlKey || event.metaKey;
  }

  document.addEventListener('keydown', function(event){
    if(!_isMod(event) || event.altKey) return;
    if(!event.shiftKey) return;
    var key = event.key;
    // `event.code` keeps the shortcut working when the layout makes Shift+P
    // produce a different character (Cyrillic layouts).
    if(key !== 'p' && key !== 'P' && event.code !== 'KeyP') return;
    event.preventDefault();
    event.stopPropagation();
    toggle();
  }, true);

  // Ctrl/Cmd+K is left to boot.js, which has always made it "new chat". Taking the
  // chord here would change a documented shortcut into a different gesture, and a
  // palette is additive: it gets its own chord rather than repurposing an existing one.

  // Exposed for the integration tests and for any future ribbon button.
  window.openCommandPalette = open;
  window.closeCommandPalette = close;
  window.toggleCommandPalette = toggle;
})();
