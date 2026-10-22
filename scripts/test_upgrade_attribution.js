#!/usr/bin/env node
/**
 * Behavioural tests for durable fallback attribution.
 *
 * Two layers are exercised with real code (no re-implementation):
 *
 *  1. `static/turn_model_attribution.js` — required as a module and called
 *     directly: notice parsing, honest identity selection, merge/enrich,
 *     badge/title text.
 *  2. `static/messages.js` — the attribution helpers are extracted from the
 *     shipped file (brace-matched, exactly like tests/test_issue6068_* do for
 *     ui.js) and executed against a minimal DOM stub, so the wiring that the
 *     browser actually runs (warning -> marker -> settle attach -> footer badge)
 *     is what gets asserted here.
 *
 * Run: node scripts/test_upgrade_attribution.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const MESSAGES_JS = fs.readFileSync(path.join(REPO, 'static', 'messages.js'), 'utf8');

let checks = 0;
const failures = [];

function ok(condition, label, detail) {
  checks += 1;
  if (!condition) failures.push(label + (detail ? ' :: ' + detail : ''));
}

function eq(actual, expected, label) {
  ok(actual === expected, label, 'expected ' + JSON.stringify(expected) + ' got ' + JSON.stringify(actual));
}

function section(name) {
  process.stdout.write('\n# ' + name + '\n');
}

// ── module under test ────────────────────────────────────────────────────────
const attributionPath = path.join(REPO, 'static', 'turn_model_attribution.js');
const attr = require(attributionPath);

// The documented agent notice (agent/chat_completion_helpers.py:1914).
const NOTICE = '\u26a0\ufe0f Model fallback: :kiro via custom unavailable ' +
  '(provider overloaded); using deepseek-v4-flash-0731:free via custom:plusvibe-api.';
const NOTICE_RETRY = NOTICE + ' Primary retry eligible in ~42 s; recovery is not guaranteed.';

// ── minimal DOM stub (only what the decorator touches) ───────────────────────
function makeNode(tag) {
  const node = {
    tag: tag,
    id: '',
    className: '',
    textContent: '',
    title: '',
    attrs: {},
    children: [],
    parentNode: null,
    setAttribute(k, v) {
      node.attrs[k] = String(v);
      if (k === 'class') node.className = String(v);
    },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(node.attrs, k) ? node.attrs[k] : null;
    },
    appendChild(child) {
      node.children.push(child);
      child.parentNode = node;
      return child;
    },
    insertBefore(child, ref) {
      const idx = node.children.indexOf(ref);
      if (idx < 0) node.children.push(child);
      else node.children.splice(idx, 0, child);
      child.parentNode = node;
      return child;
    },
  };
  Object.defineProperty(node, 'firstChild', { get() { return node.children[0] || null; } });
  node.closest = (sel) => {
    let cur = node;
    while (cur) {
      if (matches(cur, sel)) return cur;
      cur = cur.parentNode;
    }
    return null;
  };
  node.querySelector = (sel) => descendants(node).find((c) => matches(c, sel)) || null;
  node.querySelectorAll = (sel) => descendants(node).filter((c) => matches(c, sel));
  return node;
}

function descendants(node, out) {
  out = out || [];
  for (const child of node.children) {
    out.push(child);
    descendants(child, out);
  }
  return out;
}

function matches(node, sel) {
  const attrMatch = /\[([^\]]+)\]/.exec(sel);
  const base = sel.replace(/\[[^\]]+\]/, '');
  const cls = (base.match(/\.([A-Za-z0-9_-]+)/) || [])[1] || '';
  const tag = (base.match(/^([A-Za-z0-9]+)/) || [])[1] || '';
  if (tag && node.tag !== tag) return false;
  if (cls && String(node.className || '').split(/\s+/).indexOf(cls) < 0) return false;
  if (attrMatch && !Object.prototype.hasOwnProperty.call(node.attrs, attrMatch[1])) return false;
  return true;
}

function makeDocument() {
  const root = makeNode('div');
  root.id = 'msgInner';
  return {
    _root: root,
    getElementById(id) {
      if (root.id === id) return root;
      return descendants(root).find((n) => n.id === id) || null;
    },
    createElement(tag) { return makeNode(tag); },
  };
}

// ── extract real helpers out of static/messages.js ───────────────────────────
function extractFunction(source, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = source.search(re);
  if (start < 0) throw new Error('function not found in messages.js: ' + name);
  const open = source.indexOf('{', start);
  let depth = 1;
  let i = open + 1;
  while (depth > 0 && i < source.length) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') depth -= 1;
    i += 1;
  }
  if (depth !== 0) throw new Error('unbalanced braces for ' + name);
  return source.slice(start, i);
}

function extractConstArray(source, name) {
  const start = source.indexOf('const ' + name + '=');
  if (start < 0) throw new Error('const not found: ' + name);
  const end = source.indexOf(';', start);
  return source.slice(start, end + 1);
}

function buildMessagesSandbox(options) {
  const opts = options || {};
  const document = opts.document || makeDocument();
  const S = opts.S || { session: null, messages: [] };
  const INFLIGHT = opts.INFLIGHT || {};
  const window = opts.window || { TurnModelAttribution: attr };
  const code = [
    extractConstArray(MESSAGES_JS, '_EPHEMERAL_TURN_FIELDS'),
    extractFunction(MESSAGES_JS, '_isHistoricalAnchorActivityScene'),
    extractFunction(MESSAGES_JS, '_messageIdentityKey'),
    extractFunction(MESSAGES_JS, '_carryForwardEphemeralTurnFields'),
    extractFunction(MESSAGES_JS, '_turnAttributionApi'),
    extractFunction(MESSAGES_JS, '_turnAttributionSessionFacts'),
    extractFunction(MESSAGES_JS, '_buildTurnAttributionMarker'),
    extractFunction(MESSAGES_JS, '_turnAttributionTurnFacts'),
    extractFunction(MESSAGES_JS, '_finalizeTurnAttribution'),
    extractFunction(MESSAGES_JS, '_errorModelAttributionMarker'),
    extractFunction(MESSAGES_JS, '_decorateTurnModelAttribution'),
    'return {_EPHEMERAL_TURN_FIELDS,_carryForwardEphemeralTurnFields,_turnAttributionApi,' +
      '_buildTurnAttributionMarker,_finalizeTurnAttribution,_errorModelAttributionMarker,' +
      '_decorateTurnModelAttribution};',
  ].join('\n');
  // eslint-disable-next-line no-new-func
  const factory = new Function('window', 'S', 'document', 'INFLIGHT', 't', '$', code);
  return factory(window, S, document, INFLIGHT, opts.t, opts.$ || document.getElementById.bind(document));
}

// ── 1. module: notice parsing ────────────────────────────────────────────────
section('turn_model_attribution: notice parsing');

{
  const parsed = attr.parseNotice(NOTICE);
  ok(parsed.matched, 'documented agent notice is recognised');
  eq(parsed.requested && parsed.requested.model, ':kiro', 'requested model parsed');
  eq(parsed.requested && parsed.requested.provider, 'custom', 'requested provider parsed');
  eq(parsed.effective && parsed.effective.model, 'deepseek-v4-flash-0731:free', 'effective model parsed');
  eq(parsed.effective && parsed.effective.provider, 'custom:plusvibe-api', 'effective provider parsed');
  eq(parsed.reason, 'provider overloaded', 'reason parsed');
  eq(parsed.requested && parsed.requested.source, 'notice', 'identity provenance recorded');

  const withRetry = attr.parseNotice(NOTICE_RETRY);
  eq(withRetry.retry, '42s', 'retry hint parsed');

  const rateLimited = attr.parseNotice('\u26a0\ufe0f Rate limited \u2014 switching to fallback provider...');
  ok(!rateLimited.matched, 'model-less rate-limit notice matches nothing');
  eq(rateLimited.requested, null, 'no requested identity invented for model-less notice');

  const unrelated = attr.parseNotice('Compressing context');
  ok(!unrelated.matched, 'unrelated lifecycle text matches nothing');
}

// ── 2. module: buildMarker honesty ───────────────────────────────────────────
section('turn_model_attribution: buildMarker only records known facts');

{
  const marker = attr.buildMarker({ warning: { type: 'fallback', message: NOTICE }, now: 1000 });
  ok(attr.isMarker(marker), 'marker built from notice');
  eq(marker.at, 1000, 'timestamp taken from facts');
  eq(marker.requested.model, ':kiro', 'requested model on marker');
  eq(marker.effective.model, 'deepseek-v4-flash-0731:free', 'effective model on marker');
  eq(marker.substituted, true, 'substitution flagged when lanes differ');
  eq(marker.kind, 'fallback', 'marker kind');

  const modelLess = attr.buildMarker({
    warning: { type: 'fallback', message: '\u26a0\ufe0f Rate limited \u2014 switching to fallback provider...' },
    sessionModel: 'gpt-6.1-sol',
    sessionProvider: '',
    now: 1,
  });
  ok(attr.isMarker(modelLess), 'model-less notice still yields a marker');
  eq(modelLess.requested.model, 'gpt-6.1-sol', 'requested falls back to the session model (known fact)');
  eq(modelLess.requested.provider, undefined, 'empty session provider is NOT invented');
  eq(modelLess.effective, undefined, 'effective lane stays unknown before the turn settles');
  eq(modelLess.substituted, false, 'no substitution claim without two lanes');

  const nothing = attr.buildMarker({ warning: { type: 'fallback', message: '' }, now: 5 });
  eq(nothing, null, 'no facts at all -> no marker');

  const structured = attr.buildMarker({
    warning: {
      type: 'fallback',
      message: NOTICE,
      requested_model: 'gpt-6.1-sol',
      requested_provider: 'openai',
      model: 'gpt-6.1-mini',
      provider: 'openai',
    },
    now: 7,
  });
  eq(structured.requested.model, 'gpt-6.1-sol', 'structured payload wins for requested model');
  eq(structured.effective.model, 'gpt-6.1-mini', 'structured payload wins for effective model');
  eq(structured.requested.source, 'payload', 'payload provenance recorded');

  const reasoning = attr.buildMarker({
    warning: { type: 'fallback', message: NOTICE },
    reasoningEffort: 'high',
    reasoningSource: 'session',
    now: 8,
  });
  eq(reasoning.reasoning && reasoning.reasoning.effort, 'high', 'reasoning effort recorded when known');
  eq(reasoning.reasoning && reasoning.reasoning.source, 'session', 'reasoning provenance recorded');

  const noReasoning = attr.buildMarker({ warning: { type: 'fallback', message: NOTICE }, now: 9 });
  eq(noReasoning.reasoning, undefined, 'reasoning omitted when the client does not know it');

  const controlChars = attr.buildMarker({
    warning: { type: 'fallback', message: 'Model fallback: a\u0000b via p unavailable (x); using c via q' },
    now: 10,
  });
  ok(!/\u0000/.test(controlChars.message), 'control characters stripped from the stored message');
}

// ── 3. module: provider is never guessed from an id ──────────────────────────
section('turn_model_attribution: provider is never guessed');

{
  delete global.MarsModelIdentity;
  // Model-less notice: the only effective fact is the server's `_usedModel` stamp.
  const MODEL_LESS = '\u26a0\ufe0f Rate limited \u2014 switching to fallback provider...';
  const bare = attr.buildMarker({
    warning: { type: 'fallback', message: MODEL_LESS },
    usedModel: '@openai:gpt-5',
    now: 1,
  });
  eq(bare.effective.model, '@openai:gpt-5', 'id kept whole when the shared identity module is absent');
  eq(bare.effective.provider, undefined, 'no provider split guessed locally');

  global.MarsModelIdentity = {
    parse(value, provider) {
      const raw = String(value || '');
      if (raw.indexOf('@') === 0 && raw.indexOf(':') > 1) {
        return { model: raw.slice(raw.indexOf(':') + 1), provider: raw.slice(1, raw.indexOf(':')) };
      }
      return { model: raw, provider: String(provider || '') };
    },
  };
  const split = attr.buildMarker({
    warning: { type: 'fallback', message: MODEL_LESS },
    usedModel: '@openai:gpt-5',
    now: 1,
  });
  eq(split.effective.model, 'gpt-5', 'shared identity module splits the qualified id');
  eq(split.effective.provider, 'openai', 'provider comes from the shared identity module');
  delete global.MarsModelIdentity;

  // Precedence: a notice that names the effective lane beats a stale stamp.
  const noticeWins = attr.buildMarker({
    warning: { type: 'fallback', message: NOTICE },
    usedModel: 'something-else',
    now: 1,
  });
  eq(noticeWins.effective.model, 'deepseek-v4-flash-0731:free', 'notice identity beats the picker/stamp guess');
}

// ── 4. module: merge / enrich / attach ──────────────────────────────────────
section('turn_model_attribution: merge, enrich, attach');

{
  const first = attr.buildMarker({ warning: { type: 'fallback', message: NOTICE }, now: 100 });
  const second = attr.buildMarker({ warning: { type: 'fallback', message: NOTICE }, now: 200 });
  const merged = attr.mergeMarkers(first, second);
  eq(merged.message, first.message, 'identical notice is not duplicated');
  eq(merged.messages, undefined, 'single message stays a single field');
  eq(merged.at, 200, 'latest timestamp wins');

  const secondNotice = attr.buildMarker({
    warning: { type: 'fallback', message: 'Model fallback: m1 via p1 unavailable (x); using m2 via p2' },
    now: 300,
  });
  const chained = attr.mergeMarkers(merged, secondNotice);
  ok(Array.isArray(chained.messages) && chained.messages.length === 2, 'two distinct notices kept (bounded)');

  const enriched = attr.enrichMarker(first, {
    usedModel: 'deepseek-v4-flash-1017',
    gatewayRouting: { used_model: 'deepseek-v4-flash-1017', used_provider: 'custom:plusvibe-api' },
  });
  eq(enriched.effective.model, 'deepseek-v4-flash-1017', 'served model overrides the notice on settle');
  eq(enriched.effective.provider, 'custom:plusvibe-api', 'gateway provider used when explicitly present');

  const noProvider = attr.enrichMarker(first, { usedModel: 'deepseek-v4-flash-1017' });
  eq(noProvider.effective.provider, undefined, 'settle enrichment never invents a provider');

  const msg = { role: 'assistant', content: 'answer' };
  eq(attr.attachToTurn(msg, first), true, 'attach returns true for a valid marker');
  eq(attr.attachToTurn(msg, first), true, 'attach is idempotent');
  eq(msg._turnModelAttribution.message, first.message, 'marker stored on the assistant message');
  eq(attr.attachToTurn(null, first), false, 'attach refuses a missing message');
  eq(attr.attachToTurn(msg, { nope: true }), false, 'attach refuses a non-marker');
}

// ── 5. module: badge / title text ───────────────────────────────────────────
section('turn_model_attribution: badge and title text');

{
  const switched = attr.buildMarker({ warning: { type: 'fallback', message: NOTICE }, now: 1 });
  eq(attr.badgeText(switched), 'Fallback: :kiro \u2192 deepseek-v4-flash-0731:free', 'arrow form for a model change');

  const answeredBy = attr.buildMarker({
    warning: { type: 'fallback', message: '\u26a0\ufe0f Rate limited \u2014 switching to fallback provider...' },
    usedModel: 'gpt-5-mini',
    now: 1,
  });
  eq(attr.badgeText(answeredBy), 'Fallback \u00b7 answered by gpt-5-mini', 'answered-by form when only the served model is known');

  const unavailable = attr.buildMarker({
    warning: { type: 'fallback', message: '\u26a0\ufe0f Rate limited \u2014 switching to fallback provider...' },
    sessionModel: 'gpt-6.1-sol',
    now: 1,
  });
  eq(attr.badgeText(unavailable), 'Fallback \u00b7 gpt-6.1-sol unavailable', 'unavailable form when only the requested model is known');

  const reasonOnly = attr.buildMarker({ warning: { type: 'fallback', message: 'Rate limited', reason: 'quota exhausted' }, now: 1 });
  eq(attr.badgeText(reasonOnly), 'Fallback \u00b7 quota exhausted', 'reason is shown when no identity is known');

  const translated = attr.badgeText(switched, { t: (key) => (key === 'turn_attribution_fallback' ? '\u0424\u043e\u043b\u043b\u0431\u0435\u043a' : key) });
  ok(translated.indexOf('\u0424\u043e\u043b\u043b\u0431\u0435\u043a') === 0, 'i18n hook overrides the label');
  eq(attr.badgeText(switched, { t: () => '' }), 'Fallback: :kiro \u2192 deepseek-v4-flash-0731:free', 'missing translation falls back to the default label');

  const html = attr.buildMarker({
    warning: { type: 'fallback', message: 'Model fallback: a via b unavailable (<img src=x onerror=alert(1)>); using c via d' },
    now: 1,
  });
  ok(attr.badgeText(html).indexOf('<') < 0, 'badge never contains markup from the server text');
  ok(attr.titleText(html).indexOf('<img') >= 0, 'hover text keeps the raw notice as plain text');
  eq(attr.badgeText(null), '', 'no badge text for a missing marker');
}

// ── 6. messages.js: warning -> marker -> settle attach ──────────────────────
section('messages.js: warning event becomes a durable turn marker');

{
  const document = makeDocument();
  const S = {
    session: { session_id: 's1', model: 'gpt-6.1-sol', model_provider: 'openai', reasoning_effort: 'high' },
    messages: [
      { role: 'user', content: 'question', _ts: 10 },
      { role: 'assistant', content: 'partial answer', _ts: 11 },
    ],
  };
  const sandbox = buildMessagesSandbox({ document, S });

  const marker = sandbox._buildTurnAttributionMarker({ type: 'fallback', message: NOTICE });
  ok(attr.isMarker(marker), 'warning payload builds a marker through messages.js');
  eq(marker.reasoning && marker.reasoning.effort, 'high', 'session reasoning effort is carried onto the marker');

  // Mid-stream attach (warning arrives after the first token).
  attr.attachToTurn(S.messages[1], marker);

  // Settle: the server replaces S.messages with the persisted transcript that
  // carries `_usedModel` but not our client-only key.
  const settled = [
    { role: 'user', content: 'question', _ts: 10 },
    { role: 'assistant', content: 'partial answer', _ts: 11, _usedModel: 'deepseek-v4-flash-0731:free' },
  ];
  const carried = sandbox._carryForwardEphemeralTurnFields(S.messages, settled);
  ok(attr.isMarker(carried[1]._turnModelAttribution), 'marker survives the done/refresh wholesale replace');

  sandbox._finalizeTurnAttribution(carried, carried[1], marker);
  eq(carried[1]._turnModelAttribution.effective.model, 'deepseek-v4-flash-0731:free', 'settle enrichs the effective model from _usedModel');
  eq(carried[1]._turnModelAttribution.effective.provider, undefined, 'settle does not guess a provider without routing metadata');
  eq(carried[0]._turnModelAttribution, undefined, 'user turn is untouched');

  const withRouting = [
    { role: 'assistant', content: 'answer', _ts: 20, _usedModel: 'x', _gatewayRouting: { used_model: 'x', used_provider: 'openai' } },
  ];
  sandbox._finalizeTurnAttribution(withRouting, withRouting[0], marker);
  eq(withRouting[0]._turnModelAttribution.effective.provider, 'openai', 'gateway routing provider is used when present');
}

// ── 7. messages.js: no module loaded / no facts -> no-op ────────────────────
section('messages.js: graceful without the module or without facts');

{
  const sandbox = buildMessagesSandbox({ window: {}, S: { session: null, messages: [] } });
  eq(sandbox._buildTurnAttributionMarker({ type: 'fallback', message: NOTICE }), null,
    'no TurnModelAttribution module (coordinator has not wired index.html yet) -> null, no crash');
  sandbox._finalizeTurnAttribution([{ role: 'assistant', content: 'a' }], { role: 'assistant' }, null);
  sandbox._decorateTurnModelAttribution();
  ok(true, 'finalize/decorate are safe no-ops without the module');

  const live = buildMessagesSandbox({ S: { session: null, messages: [] } });
  eq(live._buildTurnAttributionMarker({ type: 'approval_gateway_offline', message: 'x' }), null,
    'non-fallback warning never produces a marker');
  eq(live._buildTurnAttributionMarker({ type: 'fallback', message: '' }), null,
    'empty fallback warning never produces a marker');
}

// ── 8. messages.js: error payloads only when substitution is proven ─────────
section('messages.js: error payload attribution');

{
  const sandbox = buildMessagesSandbox({ S: { session: null, messages: [] } });
  eq(sandbox._errorModelAttributionMarker({ type: 'auth_mismatch', model_identity: { model: 'gpt-5', provider: 'openai' } }), null,
    'plain provider error is not labelled a fallback');
  eq(sandbox._errorModelAttributionMarker({ type: 'auth_mismatch' }), null,
    'payload without model_identity is ignored');
  const substituted = sandbox._errorModelAttributionMarker({
    type: 'auth_mismatch',
    model_identity: {
      model: 'gpt-5', provider: 'openai',
      requested_model: 'gpt-6.1-sol', requested_provider: 'anthropic',
      fallback: 'activated', model_substituted: true,
    },
  });
  ok(attr.isMarker(substituted), 'proven substitution becomes a marker');
  eq(substituted.requested.model, 'gpt-6.1-sol', 'requested lane from the error payload');
  eq(substituted.requested.provider, 'anthropic', 'requested provider from the error payload');
  eq(substituted.effective.model, 'gpt-5', 'effective lane from the error payload');
  eq(substituted.effective.provider, 'openai', 'effective provider from the error payload');
  eq(substituted.substituted, true, 'substitution flagged');
}

// ── 9. messages.js: footer badge via the existing render path ───────────────
section('messages.js: footer badge rendering');

{
  const document = makeDocument();
  const inner = document._root;
  const turn = makeNode('div');
  turn.className = 'assistant-turn';
  const segment = makeNode('div');
  segment.className = 'assistant-segment';
  segment.setAttribute('data-msg-idx', '1');
  const foot = makeNode('div');
  foot.className = 'msg-foot';
  inner.appendChild(turn);
  turn.appendChild(segment);
  turn.appendChild(foot);

  const marker = attr.buildMarker({ warning: { type: 'fallback', message: NOTICE }, now: 1 });
  const S = {
    session: { session_id: 's1' },
    messages: [
      { role: 'user', content: 'q' },
      { role: 'assistant', content: 'a', _turnModelAttribution: marker },
    ],
  };
  const sandbox = buildMessagesSandbox({ document, S });

  sandbox._decorateTurnModelAttribution();
  eq(foot.children.length, 0, 'the badge never enters .msg-foot (ui.js skips its chips there)');
  const badges = turn.children.filter((c) => c.className === 'msg-model-attribution gateway-failover-inline');
  eq(badges.length, 1, 'exactly one badge is added to the turn');
  const badge = badges[0];
  eq(badge.className, 'msg-model-attribution gateway-failover-inline', 'badge reuses the existing note style hook');
  eq(badge.getAttribute('data-turn-attribution'), '1', 'badge is marked for tests and styling');
  eq(badge.textContent, 'Fallback: :kiro \u2192 deepseek-v4-flash-0731:free', 'badge is plain text');
  ok(typeof badge.title === 'string' && badge.title.indexOf('requested: :kiro (custom)') >= 0, 'hover text lists the requested lane');
  ok(badge.innerHTML === undefined, 'badge has no innerHTML payload');
  eq(turn.children.indexOf(badge), turn.children.indexOf(foot) - 1, 'badge sits directly above the footer');

  sandbox._decorateTurnModelAttribution();
  eq(turn.children.filter((c) => c.className.indexOf('msg-model-attribution') >= 0).length, 1,
    're-render does not duplicate the badge');

  // A message without a marker must not gain a badge.
  const document2 = makeDocument();
  const turn2 = makeNode('div');
  turn2.className = 'assistant-turn';
  const seg2 = makeNode('div');
  seg2.className = 'assistant-segment';
  seg2.setAttribute('data-msg-idx', '1');
  const foot2 = makeNode('div');
  foot2.className = 'msg-foot';
  document2._root.appendChild(turn2);
  turn2.appendChild(seg2);
  turn2.appendChild(foot2);
  const sandbox2 = buildMessagesSandbox({
    document: document2,
    S: { session: { session_id: 's2' }, messages: [{ role: 'assistant', content: 'a' }] },
  });
  sandbox2._decorateTurnModelAttribution();
  eq(turn2.children.length, 2, 'turn without a marker gets no badge');

  // Gateway-routed turns already get a durable "Model switched: …" chip from
  // ui.js; adding ours too would state the same fact twice.
  const document3 = makeDocument();
  const turn3 = makeNode('div');
  turn3.className = 'assistant-turn';
  const seg3 = makeNode('div');
  seg3.className = 'assistant-segment';
  seg3.setAttribute('data-msg-idx', '1');
  const foot3 = makeNode('div');
  foot3.className = 'msg-foot';
  const uiChip = makeNode('span');
  uiChip.className = 'msg-model-warning-inline';
  document3._root.appendChild(turn3);
  turn3.appendChild(seg3);
  turn3.appendChild(foot3);
  foot3.appendChild(uiChip);
  const sandbox3 = buildMessagesSandbox({
    document: document3,
    S: { session: { session_id: 's3' }, messages: [{ role: 'assistant', content: 'a', _turnModelAttribution: marker }] },
  });
  sandbox3._decorateTurnModelAttribution();
  eq(turn3.children.filter((c) => c.className.indexOf('msg-model-attribution') >= 0).length, 0,
    'ui.js own model-switch chip wins; no duplicate attribution');
}

// ── summary ─────────────────────────────────────────────────────────────────
process.stdout.write('\n' + (checks - failures.length) + '/' + checks + ' checks passed\n');
if (failures.length) {
  process.stdout.write('\nFAILURES:\n');
  failures.forEach((f) => process.stdout.write('  - ' + f + '\n'));
  process.exit(1);
}
