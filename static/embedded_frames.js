// Lazy, failure-aware hosting for the embedded /dashboard/ and /music/ sections.
//
// The desktop runtime pack serves the WebUI without the reverse proxy that
// mounts those sections, so both paths answer with a JSON 404. Loading the
// iframes eagerly on boot meant a broken frame sat behind the rail forever.
// Here the frame is created on the first open, its navigation response is
// checked, and an unavailable section is reported as a first-class state with
// a retry button. A rail/sidebar tab whose section is confirmed missing is
// hidden; a failed probe is never treated as "missing".
(function(){
  'use strict';

  var FRAMES = {
    dashboard: {site: '/dashboard/', host: 'projectsFrameHost', state: 'projectsFrameState', unavailable: 'projectsFrameUnavailable', iframe: 'projectsIframe', panel: 'projects'},
    music:     {site: '/music/',     host: 'musicFrameHost',     state: 'musicFrameState',     unavailable: 'musicFrameUnavailable',     iframe: 'musicIframe',     panel: 'music'}
  };
  // A probe slower than this is reported as a timeout instead of spinning.
  var PROBE_TIMEOUT_MS = 8000;
  // Verdicts are never cached: a section can be mounted later (redeploy, proxy
  // restart), and each panel open is cheap enough to re-probe.
  var _probes = {};

  // `$` is defined by ui.js, which parses after this file; resolve lazily.
  function _byId(id){ return (typeof $ === 'function') ? $(id) : document.getElementById(id); }
  function _frame(kind){ return FRAMES[kind] ? _byId(FRAMES[kind].iframe) : null; }

  function _resolveUrl(value){
    try { return new URL(value, document.baseURI || location.href); } catch(_) { return null; }
  }

  // Same-origin probe: a section that is not mounted answers 404/501, and its
  // JSON body is not HTML. Cross-origin (operator-configured dashboard URL)
  // cannot be inspected, so it is loaded optimistically.
  function _probe(kind){
    var spec = FRAMES[kind];
    if(_probes[kind]) return _probes[kind];
    var url = _resolveUrl(spec.site);
    // Unresolvable or remote target: cannot inspect it, load optimistically.
    if(!url || !_isSameOrigin(url)) return Promise.resolve('ok');
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = null;
    var fetchPromise = fetch(url.href, {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'follow',
      headers: {'Accept': 'text/html,application/xhtml+xml'},
      signal: controller ? controller.signal : undefined
    }).then(function(res){
      var ct = (res.headers.get('content-type') || '').toLowerCase();
      var html = ct.indexOf('text/html') !== -1 || ct.indexOf('application/xhtml') !== -1;
      if(res.ok && html) return 'ok';
      if(res.status === 404 || res.status === 501 || res.status === 502 || res.status === 503) return 'unavailable';
      if(res.ok && !html) return 'unavailable';
      return 'error';
    }).catch(function(err){
      if(err && err.name === 'AbortError') return 'timeout';
      return 'error';
    }).then(function(result){
      clearTimeout(timer);
      delete _probes[kind];
      return result;
    });
    if(controller){
      timer = setTimeout(function(){
        try { controller.abort(); } catch(_) {}
      }, PROBE_TIMEOUT_MS);
    }
    _probes[kind] = fetchPromise;
    return fetchPromise;
  }

  function _isSameOrigin(url){
    var base = _resolveUrl('.');
    if(!base) return false;
    // Compare scheme+host directly: some embedded webviews do not expose the
    // derived `location` origin property at all.
    return url.protocol === base.protocol && url.host === base.host;
  }

  function _showState(kind, mode){
    var spec = FRAMES[kind];
    var host = _byId(spec.host);
    var iframe = _frame(kind);
    if(!host) return;
    // `null` means "leave the host classes alone"; without an explicit ready
    // state to paint we still must not leave a stale spinner on screen.
    if(mode === null && iframe && iframe.getAttribute('src')) mode = 'ready';
    if(mode === null) return;
    host.classList.toggle('is-loading', mode === 'loading');
    host.classList.toggle('is-unavailable', mode === 'unavailable');
    var stateEl = _byId(spec.state);
    var unavailEl = _byId(spec.unavailable);
    if(stateEl) stateEl.hidden = mode !== 'loading';
    if(unavailEl) unavailEl.hidden = mode !== 'unavailable';
    if(iframe) iframe.hidden = mode !== 'ready';
  }

  function _tabLabel(kind){
    var spec = FRAMES[kind];
    var tabs = document.querySelectorAll('[role="tab"][data-panel="' + spec.panel + '"]');
    var label = '';
    for(var i = 0; i < tabs.length; i++){
      label = tabs[i].getAttribute('data-tooltip') || tabs[i].getAttribute('aria-label') || '';
      if(label) break;
    }
    return label;
  }

  // Only a confirmed "not mounted" hides the tab: a timeout or a network error
  // must not silently remove navigation the user could still need.
  function _markTab(kind, unavailable, timedOut){
    var spec = FRAMES[kind];
    var tabs = document.querySelectorAll('[role="tab"][data-panel="' + spec.panel + '"]');
    for(var i = 0; i < tabs.length; i++){
      var tab = tabs[i];
      tab.classList.toggle('nav-tab-unavailable', !!unavailable);
      if(unavailable){
        tab.setAttribute('data-tab-label', _tabLabel(kind) || kind);
        var text = typeof t === 'function' ? t('embedded_frame_tab_unavailable') : 'Недоступно на этом сервере';
        tab.setAttribute('aria-disabled', 'true');
        tab.setAttribute('data-tooltip', tab.getAttribute('data-tab-label') + ' — ' + text);
        tab.setAttribute('title', text);
      } else if(timedOut){
        tab.removeAttribute('aria-disabled');
      } else {
        tab.removeAttribute('aria-disabled');
        tab.removeAttribute('title');
        if(tab.getAttribute('data-tab-label')) tab.setAttribute('data-tooltip', tab.getAttribute('data-tab-label'));
      }
    }
    if(unavailable && typeof _markSidebarTabHidden === 'function') _markSidebarTabHidden(spec.panel, true);
    if(!unavailable && !timedOut && typeof _markSidebarTabHidden === 'function') _markSidebarTabHidden(spec.panel, false);
  }

  // Called by switchPanel() every time the projects/music panel opens.
  function ensureEmbeddedFrame(kind, opts){
    opts = opts || {};
    var spec = FRAMES[kind];
    if(!spec) return Promise.resolve('missing-frame');
    var iframe = _frame(kind);
    if(!iframe) return Promise.resolve('missing-frame');
    var loaded = iframe.dataset.embeddedLoaded === '1';
    if(loaded && !opts.force){
      // Already mounted: just make sure no stale unavailable overlay is shown.
      _showState(kind, iframe.getAttribute('src') ? 'ready' : null);
      return Promise.resolve('ready');
    }
    _showState(kind, 'loading');
    return _probe(kind).then(function(result){
      if(result === 'ok'){
        if(opts.force || iframe.dataset.embeddedLoaded !== '1'){
          // Cache-bust so "Повторить" re-requests a section that just came up.
          iframe.setAttribute('src', opts.force
            ? spec.site + (spec.site.indexOf('?') === -1 ? '?' : '&') + 'retry=' + Date.now()
            : spec.site);
        }
        iframe.dataset.embeddedLoaded = '1';
        _showState(kind, 'ready');
        _markTab(kind, false, false);
        return 'ready';
      }
      if(result === 'unavailable'){
        iframe.removeAttribute('src');
        delete iframe.dataset.embeddedLoaded;
        _showState(kind, 'unavailable');
        _markTab(kind, true, false);
        return 'unavailable';
      }
      // timeout / network / auth: keep the retry affordance, keep the tab.
      iframe.removeAttribute('src');
      delete iframe.dataset.embeddedLoaded;
      _showState(kind, 'unavailable');
      _markTab(kind, false, true);
      var stateEl = _byId(FRAMES[kind].state);
      if(stateEl){
        var textEl = stateEl.querySelector('[data-embedded-frame-text]');
        if(textEl) textEl.textContent = result === 'timeout'
          ? (typeof t === 'function' ? t('embedded_frame_timeout') : 'Сервер не ответил вовремя')
          : (typeof t === 'function' ? t('embedded_frame_error') : 'Не удалось загрузить раздел');
      }
      return result;
    });
  }

  function retryEmbeddedFrame(kind){
    if(!FRAMES[kind]) return false;
    return ensureEmbeddedFrame(kind, {force: true});
  }

  // The frame itself may fail after the probe passed (proxy down mid-session).
  function _onFrameError(kind){
    var iframe = _frame(kind);
    if(!iframe || !iframe.getAttribute('src')) return;
    delete iframe.dataset.embeddedLoaded;
    _showState(kind, 'unavailable');
    setTimeout(function(){ ensureEmbeddedFrame(kind, {force: true}); }, 0);
  }

  Object.keys(FRAMES).forEach(function(kind){
    var iframe = _frame(kind);
    if(!iframe) return;
    iframe.addEventListener('error', function(){ _onFrameError(kind); });
    iframe.addEventListener('load', function(){
      // The load event is the authoritative signal: it fires for the real
      // section and for a fallback error page alike, so never leave the
      // spinner up once the frame has content.
      if(iframe.getAttribute('src')) _showState(kind, 'ready');
    });
    _showState(kind, 'loading');
  });

  // Markup ships the default Russian copy; i18n.js re-applies [data-i18n] on
  // every locale switch, so the static labels need no extra hook here.

  window.ensureEmbeddedFrame = ensureEmbeddedFrame;
  window.retryEmbeddedFrame = retryEmbeddedFrame;

  // Lets panels.js close the panel's own loading state without knowing the
  // internals of this module.
  window._embeddedFrameState = function(kind){
    var iframe = _frame(kind);
    if(!iframe) return 'missing-frame';
    if(iframe.dataset.embeddedLoaded === '1') return 'ready';
    return 'idle';
  };
})();
