/**
 * Turn model attribution — durable record of a fallback on the CURRENT assistant turn.
 *
 * Why this module exists
 * ----------------------
 * The server bridges an agent fallback into a `warning` SSE event
 * (`{type:'fallback', message}`) which `static/messages.js` renders as a 4-second
 * composer status. Once that toast fades the user has no way to learn, from the
 * transcript, that the answer did not come from the lane they asked for.
 *
 * This module turns the facts the client actually knows into one compact marker
 * stored as display metadata on the assistant turn (`msg._turnModelAttribution`),
 * so any render path can show it again without re-deriving anything.
 *
 * Honesty rules (hard requirements, enforced by tests)
 * ---------------------------------------------------
 * - Only facts that are explicitly known are recorded. Nothing is inferred:
 *   an unknown provider stays unknown (`provider` key absent), never defaulted to
 *   the session provider, the model prefix or a guess.
 * - The effective lane is only taken from: explicit structured payload fields,
 *   gateway routing metadata, the documented agent notice text, or the server's
 *   own `_usedModel` stamp. Never from the picker's current value.
 * - The marker is plain data (no HTML, no functions) so it survives JSON
 *   persistence and can never be a script-injection vector. Renderers MUST use
 *   `textContent`; this module never returns HTML.
 *
 * Agent notice shape this parser accepts (agent/chat_completion_helpers.py):
 *   "⚠️ Model fallback: <old model> via <old provider> unavailable (<reason>);
 *    using <new model> via <new provider>. Primary retry eligible in ~N s; ..."
 * A notice that does not match that shape records NO models — only the raw text.
 *
 * Durability boundary (needs backend work — reported to the coordinator)
 * ---------------------------------------------------------------------
 * The marker lives on the client message object, so it survives re-render,
 * reconnect, and the done/refresh transcript replace
 * (`_EPHEMERAL_TURN_FIELDS`). It does NOT survive a page reload, because the
 * server never persists it. Two edits close that gap; both are outside this
 * module's ownership:
 *   1. api/streaming.py, in the turn-settle block that already stamps
 *      `_dm['_usedModel']` / `_dm['_gatewayRouting']` (~line 12238), also stamp
 *      `_dm['_turnModelAttribution'] = {v:1, kind:'fallback',
 *        requested_model, requested_provider, effective_model,
 *        effective_provider, reason, retry_seconds, reasoning_effort, source}`
 *      from facts the server already holds (`old_model`/`old_provider`,
 *      `fb_model`/`fb_provider`, `_fallback_reason_text(reason)`,
 *      `cooldown_seconds`).
 *   2. api/models.py: add `"_turnModelAttribution"` to
 *      `_SESSION_MESSAGE_DISPLAY_METADATA_KEYS` so the transcript merge keeps it.
 * No client change is needed once those exist: the render decorator in
 * messages.js already draws from `msg._turnModelAttribution` on hydrate.
 */
(function(root, factory){
  'use strict';
  var api = factory();
  if(typeof module === 'object' && module && module.exports) module.exports = api;
  if(root) root.TurnModelAttribution = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this), function(){
  'use strict';

  var VERSION = 1;
  var MAX_MESSAGE = 400;
  var MAX_REASON = 160;

  // Documented agent fallback notice. Deliberately strict: model ids and provider
  // slugs are single tokens, the reason is parenthesised. Anything else does not
  // match and therefore records no model identity at all.
  // The final provider is followed by a sentence period ("... via custom:p. "),
  // so tokens are cut at whitespace/`)` and stripped of trailing punctuation —
  // otherwise the provider would be recorded as "custom:p." and never compare
  // equal to the server's routing value.
  var NOTICE_RE = /model fallback:\s*([^\s,;)]+)\s+via\s+([^\s,;)]+)\s+unavailable\s*\(([^)]*)\)\s*;\s*using\s+([^\s,;)]+)\s+via\s+([^\s,;)]+)/i;
  var RETRY_RE = /primary retry eligible in\s*~?\s*(\d+)\s*s/i;

  function _clean(value, max){
    if(value === null || value === undefined) return '';
    var text = String(value).replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim();
    if(max && text.length > max) text = text.slice(0, max);
    return text;
  }

  // Trailing sentence punctuation on a notice token ("custom:p." or "custom:p;")
  // is punctuation, not part of the slug.
  function _token(value){
    return _clean(value).replace(/[.,;:]+$/, '');
  }

  // Provider-qualified ids ("@openai:gpt-5") are split by the shared identity
  // module when it is loaded; otherwise the id is kept whole and the provider
  // stays unknown. Never parse a provider out of an id by ourselves.
  function _splitIdentity(value, provider){
    var raw = _clean(value);
    var lane = _clean(provider);
    if(!raw) return {model:'', provider:lane};
    var shared = (typeof window !== 'undefined' && window.MarsModelIdentity)
      || (typeof globalThis !== 'undefined' && globalThis.MarsModelIdentity)
      || null;
    if(shared && typeof shared.parse === 'function'){
      try{
        var parsed = shared.parse(raw, lane) || {};
        return {model:_clean(parsed.model) || raw, provider:_clean(parsed.provider) || lane};
      }catch(_){ /* fall through to the no-provider degradation */ }
    }
    return {model:raw, provider:lane};
  }

  function _identity(model, provider, source){
    var id = _splitIdentity(model, provider);
    if(!id.model) return null;
    var out = {model:id.model};
    if(id.provider) out.provider = id.provider;
    if(source) out.source = source;
    return out;
  }

  function _normModel(value){
    return _clean(value).toLowerCase().replace(/^@[^:]+:/, '');
  }

  function _sameLane(a, b){
    if(!a || !b) return false;
    var am = _normModel(a.model), bm = _normModel(b.model);
    if(!am || !bm || am !== bm) return false;
    var ap = _clean(a.provider).toLowerCase(), bp = _clean(b.provider).toLowerCase();
    if(ap && bp && ap !== bp) return false;
    return true;
  }

  /**
   * Parse the documented agent fallback notice.
   * @returns {{matched:boolean, requested:?object, effective:?object, reason:string, retry:string}}
   */
  function parseNotice(message){
    var text = _clean(message, MAX_MESSAGE);
    var out = {matched:false, requested:null, effective:null, reason:'', retry:''};
    if(!text) return out;
    var m = NOTICE_RE.exec(text);
    if(!m) return out;
    out.matched = true;
    out.requested = _identity(_token(m[1]), _token(m[2]), 'notice');
    out.effective = _identity(_token(m[4]), _token(m[5]), 'notice');
    out.reason = _clean(m[3], MAX_REASON);
    var retry = RETRY_RE.exec(text);
    if(retry) out.retry = retry[1] + 's';
    return out;
  }

  function _pickIdentity(candidates){
    for(var i = 0; i < candidates.length; i++){
      var cand = candidates[i];
      if(cand && cand.model) return cand;
    }
    return null;
  }

  function _structuredRequested(warning){
    if(!warning || typeof warning !== 'object') return null;
    var model = warning.requested_model || warning.requestedModel;
    var provider = warning.requested_provider || warning.requestedProvider;
    if(!_clean(model) && !_clean(provider)) return null;
    return _identity(model, provider, 'payload');
  }

  function _structuredEffective(warning){
    if(!warning || typeof warning !== 'object') return null;
    var model = warning.effective_model || warning.effectiveModel || warning.model;
    var provider = warning.effective_provider || warning.effectiveProvider || warning.provider;
    if(!_clean(model) && !_clean(provider)) return null;
    return _identity(model, provider, 'payload');
  }

  function _gatewayIdentity(routing, which){
    if(!routing || typeof routing !== 'object') return null;
    var model = which === 'requested' ? routing.requested_model : routing.used_model;
    var provider = which === 'requested' ? routing.requested_provider : routing.used_provider;
    if(!_clean(model) && !_clean(provider)) return null;
    return _identity(model, provider, 'gateway_routing');
  }

  function _reasoning(facts){
    var effort = _clean(facts && (facts.reasoningEffort || facts.reasoning_effort));
    var source = _clean(facts && (facts.reasoningSource || facts.reasoning_source));
    var warning = facts && facts.warning;
    if(!effort && warning && typeof warning === 'object'){
      effort = _clean(warning.reasoning_effort || warning.reasoningEffort);
      source = source || _clean(warning.reasoning_source || warning.reasoningSource);
    }
    if(!effort) return null;
    var out = {effort:effort};
    if(source) out.source = source;
    return out;
  }

  /**
   * Is this warning payload a fallback notice at all? The server emits exactly
   * `{type:'fallback', message}` (api/streaming.py `_agent_status_callback`), so
   * a different `type` is a different warning and must never be labelled as a
   * model fallback. A payload with no `type` is accepted only when it actually
   * carries fallback evidence (the documented notice text or structured lanes).
   */
  function _isFallbackWarning(warning){
    if(!warning) return false;
    var type = _clean(warning.type).toLowerCase();
    if(type) return type === 'fallback';
    return !!(parseNotice(_clean(warning.message || warning.text)).matched
      || _structuredRequested(warning)
      || _structuredEffective(warning));
  }

  /**
   * Build a marker from every known fact. Returns null when nothing at all is
   * known (no notice text and no identity) — callers must not fabricate one.
   *
   * facts: {warning, usedModel, usedProvider, gatewayRouting, sessionModel,
   *         sessionProvider, reasoningEffort, reasoningSource, now}
   */
  function buildMarker(facts){
    facts = facts || {};
    var warning = (facts.warning && typeof facts.warning === 'object') ? facts.warning : null;
    if(!_isFallbackWarning(warning)) return null;
    var message = _clean(warning && (warning.message || warning.text), MAX_MESSAGE);
    var notice = parseNotice(message);

    var requested = _pickIdentity([
      _structuredRequested(warning),
      _gatewayIdentity(facts.gatewayRouting, 'requested'),
      notice.requested,
      _identity(facts.sessionModel, facts.sessionProvider, 'session'),
    ]);
    var effective = _pickIdentity([
      _structuredEffective(warning),
      _gatewayIdentity(facts.gatewayRouting, 'used'),
      notice.effective,
      _identity(facts.usedModel, facts.usedProvider, 'used_model'),
    ]);

    var reason = _clean(warning && (warning.reason || warning.fallback_reason), MAX_REASON) || notice.reason;
    var retry = _clean(warning && warning.retry, 32) || notice.retry;
    var reasoning = _reasoning(facts);

    if(!message && !requested && !effective && !reason) return null;

    var marker = {v:VERSION, kind:'fallback', source:'server_warning'};
    var now = Number(facts.now);
    marker.at = Math.floor(Number.isFinite(now) && now > 0 ? now : Date.now() / 1000);
    if(message) marker.message = message;
    if(requested) marker.requested = requested;
    if(effective) marker.effective = effective;
    if(reason) marker.reason = reason;
    if(retry) marker.retry = retry;
    if(reasoning) marker.reasoning = reasoning;
    marker.substituted = !!(requested && effective && !_sameLane(requested, effective));
    return marker;
  }

  function isMarker(value){
    return !!(value && typeof value === 'object' && value.v === VERSION && value.kind === 'fallback');
  }

  function _mergeLane(prev, next){
    if(!next || !next.model) return prev || null;
    if(!prev || !prev.model) return next;
    var out = {model:next.model};
    var provider = _clean(next.provider) || _clean(prev.provider);
    if(provider) out.provider = provider;
    var source = _clean(next.source) || _clean(prev.source);
    if(source) out.source = source;
    return out;
  }

  /**
   * Merge two markers. Later facts win per field; nothing is dropped, and a
   * differing message text is kept as a bounded list of at most two entries.
   */
  function mergeMarkers(prev, next){
    if(!isMarker(prev)) return isMarker(next) ? next : null;
    if(!isMarker(next)) return prev;
    var out = {v:VERSION, kind:'fallback', source:prev.source || next.source || 'server_warning'};
    out.at = Math.max(Number(prev.at) || 0, Number(next.at) || 0) || Math.floor(Date.now() / 1000);
    var messages = [];
    [prev.message, next.message].forEach(function(text){
      var clean = _clean(text, MAX_MESSAGE);
      if(clean && messages.indexOf(clean) < 0) messages.push(clean);
    });
    if(messages.length === 1) out.message = messages[0];
    else if(messages.length > 1) out.messages = messages.slice(-2);
    var requested = _mergeLane(prev.requested, next.requested);
    if(requested) out.requested = requested;
    var effective = _mergeLane(prev.effective, next.effective);
    if(effective) out.effective = effective;
    var reason = _clean(next.reason, MAX_REASON) || _clean(prev.reason, MAX_REASON);
    if(reason) out.reason = reason;
    var retry = _clean(next.retry, 32) || _clean(prev.retry, 32);
    if(retry) out.retry = retry;
    var reasoning = next.reasoning || prev.reasoning;
    if(reasoning && reasoning.effort) out.reasoning = reasoning;
    out.substituted = !!(out.requested && out.effective && !_sameLane(out.requested, out.effective));
    return out;
  }

  /**
   * Fill/refresh the effective lane from facts learned AFTER the notice (the
   * server stamps the served model on the settled assistant message). Only
   * explicit values are used; an unknown provider is left unknown.
   */
  function enrichMarker(marker, facts){
    if(!isMarker(marker)) return null;
    facts = facts || {};
    var gateway = _gatewayIdentity(facts.gatewayRouting, 'used');
    var served = _identity(facts.usedModel, facts.usedProvider, 'used_model');
    var effective = _pickIdentity([gateway, served, marker.effective]);
    var requested = _pickIdentity([marker.requested, _gatewayIdentity(facts.gatewayRouting, 'requested')]);
    var reasoning = _reasoning(facts) || marker.reasoning || null;

    var out = {v:VERSION, kind:'fallback', source:marker.source || 'server_warning'};
    out.at = Number(marker.at) || Math.floor(Date.now() / 1000);
    if(marker.message) out.message = marker.message;
    if(Array.isArray(marker.messages) && marker.messages.length) out.messages = marker.messages.slice(-2);
    if(requested) out.requested = requested;
    if(effective) out.effective = effective;
    if(marker.reason) out.reason = marker.reason;
    if(marker.retry) out.retry = marker.retry;
    if(reasoning) out.reasoning = reasoning;
    out.substituted = !!(out.requested && out.effective && !_sameLane(out.requested, out.effective));
    return out;
  }

  /** Attach (merge) a marker onto an assistant message's display metadata. */
  function attachToTurn(msg, marker){
    if(!msg || typeof msg !== 'object') return false;
    if(!isMarker(marker)) return false;
    msg._turnModelAttribution = mergeMarkers(msg._turnModelAttribution, marker);
    return isMarker(msg._turnModelAttribution);
  }

  function _translate(opts, key, fallback){
    var t = opts && opts.t;
    if(typeof t !== 'function') return fallback;
    try{
      var value = t(key);
      return (value && value !== key) ? String(value) : fallback;
    }catch(_){ return fallback; }
  }

  /**
   * Compact one-line badge. Never HTML; the caller renders it with textContent.
   * Providers stay out of the badge (too long) and live in `titleText`.
   */
  function badgeText(marker, opts){
    if(!isMarker(marker)) return '';
    var label = _translate(opts, 'turn_attribution_fallback', 'Fallback');
    var req = marker.requested && marker.requested.model ? marker.requested.model : '';
    var eff = marker.effective && marker.effective.model ? marker.effective.model : '';
    // The arrow form is only for a model change. A provider-only switch (same id
    // on another lane) would read as "X → X", so it uses the answered-by form and
    // leaves the provider pair to the hover text.
    var modelChanged = !!(req && eff && _normModel(req) !== _normModel(eff));
    if(modelChanged){
      return label + ': ' + req + ' \u2192 ' + eff;
    }
    if(eff) return label + ' \u00b7 ' + _translate(opts, 'turn_attribution_answered_by', 'answered by') + ' ' + eff;
    if(req) return label + ' \u00b7 ' + req + ' ' + _translate(opts, 'turn_attribution_unavailable', 'unavailable');
    var reason = _clean(marker.reason, MAX_REASON);
    return reason ? label + ' \u00b7 ' + reason : label;
  }

  /** Full hover text: every known fact, one per line. Plain text. */
  function titleText(marker){
    if(!isMarker(marker)) return '';
    var lines = [];
    var raw = marker.message || (Array.isArray(marker.messages) ? marker.messages.join('\n') : '');
    if(raw) lines.push(raw);
    function lane(id){
      if(!id || !id.model) return '';
      return id.provider ? id.model + ' (' + id.provider + ')' : id.model;
    }
    if(marker.requested) lines.push('requested: ' + lane(marker.requested));
    if(marker.effective) lines.push('effective: ' + lane(marker.effective));
    if(marker.reason) lines.push('reason: ' + marker.reason);
    if(marker.retry) lines.push('primary retry eligible in ~' + marker.retry);
    if(marker.reasoning && marker.reasoning.effort){
      lines.push('reasoning: ' + marker.reasoning.effort + (marker.reasoning.source ? ' (' + marker.reasoning.source + ')' : ''));
    }
    return lines.join('\n');
  }

  /** Is there anything worth showing for this marker? */
  function hasVisibleFacts(marker){
    if(!isMarker(marker)) return false;
    return !!(marker.requested || marker.effective || marker.reason);
  }

  return {
    VERSION: VERSION,
    parseNotice: parseNotice,
    buildMarker: buildMarker,
    mergeMarkers: mergeMarkers,
    enrichMarker: enrichMarker,
    attachToTurn: attachToTurn,
    isMarker: isMarker,
    hasVisibleFacts: hasVisibleFacts,
    badgeText: badgeText,
    titleText: titleText,
  };
});
