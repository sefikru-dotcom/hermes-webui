#!/usr/bin/env node
/**
 * Поведенческие тесты резолва reasoning-пина (audit: frontend pin «Auto» bug).
 *
 * Гоняют РЕАЛЬНЫЙ static/model_identity.js и РЕАЛЬНЫЕ функции ui.js
 * (`_marsReasoningOverrideFor`, `_marsReasoningChipPayload`,
 * `_providerFromModelValue`, `_marsReasoningProvider`), извлечённые из файла по
 * имени, в изолированном vm-контексте: ни браузера, ни jsdom, ни живых настроек.
 *
 * Что проверяется:
 *   - пин, сохранённый бэкендом как `@provider:model`, виден браузеру для голой
 *     модели с той же полосой (это и был баг «Auto» при реальном пине);
 *   - пин той же модели на ДРУГОМ провайдере не протекает;
 *   - легаси-ключ без провайдера продолжает работать (совместимость);
 *   - payload.model не стрипается до голого id при scope=model.
 *
 * Запуск: node tests/js/reasoning_pin_behavior.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const UI_SRC = fs.readFileSync(path.join(ROOT, 'static', 'ui.js'), 'utf8');
const MODEL_SRC = fs.readFileSync(path.join(ROOT, 'static', 'model_identity.js'), 'utf8');

// ── извлечение функций из ui.js ─────────────────────────────────────────────

function skipString(src, i) {
  const q = src[i];
  for (let j = i + 1; j < src.length; j++) {
    if (src[j] === '\\') { j++; continue; }
    if (src[j] === q) return j;
  }
  return src.length;
}

function extractFn(name) {
  const marker = 'function ' + name + '(';
  const start = UI_SRC.indexOf(marker);
  if (start < 0) throw new Error('ui.js: функция ' + name + ' не найдена');
  let depth = 0;
  for (let i = UI_SRC.indexOf('{', start); i < UI_SRC.length; i++) {
    const c = UI_SRC[i];
    if (c === '"' || c === "'" || c === '`') { i = skipString(UI_SRC, i); continue; }
    if (c === '/' && UI_SRC[i + 1] === '/') { i = UI_SRC.indexOf('\n', i); continue; }
    if (c === '/' && UI_SRC[i + 1] === '*') { i = UI_SRC.indexOf('*/', i) + 1; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return UI_SRC.slice(start, i + 1); }
  }
  throw new Error('ui.js: несбалансированные скобки в ' + name);
}

const FN_NAMES = [
  '_providerFromModelValue',
  '_marsReasoningProvider',
  '_marsReasoningOverrideFor',
  '_marsReasoningChipPayload',
];
const FN_SRC = FN_NAMES.map(extractFn).join('\n');

// ── изолированные контексты ────────────────────────────────────────────────

/**
 * @param {boolean} withIdentity — подключать ли реальный model_identity.js
 *   (false = проверка фолбэка для старого кэша/харнесса без модуля).
 */
function makeCtx(withIdentity) {
  const ctx = { console, __effortCtx: {}, __pins: {} };
  vm.createContext(ctx);
  if (withIdentity) vm.runInContext(MODEL_SRC, ctx);
  vm.runInContext(
    'var _marsModelReasoningOverrides=__pins;\n' +
      'function _reasoningEffortContext(){ return __effortCtx; }\n' +
      FN_SRC,
    ctx
  );
  return ctx;
}

function pinOf(ctx, model, provider, pins, effortCtx) {
  ctx.__pins = pins || {};
  ctx._marsModelReasoningOverrides = ctx.__pins;
  if (effortCtx) ctx.__effortCtx = effortCtx;
  return ctx._marsReasoningOverrideFor(model, provider);
}

// ── мини-раннер ────────────────────────────────────────────────────────────

let failed = 0;
let passed = 0;

function check(name, actual, expected) {
  const ok = actual === expected;
  if (ok) {
    passed++;
    console.log('  ok   ' + name);
  } else {
    failed++;
    console.log('  FAIL ' + name + ': got ' + JSON.stringify(actual) + ', want ' + JSON.stringify(expected));
  }
}

function checkTrue(name, cond, detail) {
  check(name, !!cond, true);
  if (!cond && detail) console.log('       ' + detail);
}

// ── 1. реальный модуль: пин находится по полосе ────────────────────────────

console.log('pin lookup (model_identity.js подключён)');
{
  const ctx = makeCtx(true);

  // Баг аудита: пин лежит как `@provider:model`, чип искал по голому id → «Auto».
  check(
    'квалифицированный ключ виден для голой модели',
    pinOf(ctx, 'gpt-5.4-mini', 'openai', { '@openai:gpt-5.4-mini': 'high' }),
    'high'
  );
  check(
    'квалифицированный ключ виден без явного провайдера (из @provider:model)',
    pinOf(ctx, '@openai:gpt-5.4-mini', '', { '@openai:gpt-5.4-mini': 'high' }),
    'high'
  );
  check(
    'custom-провайдер: @custom:plusvibe-api:deepseek-v4.1-flash',
    pinOf(ctx, 'deepseek-v4.1-flash', 'custom:plusvibe-api', {
      '@custom:plusvibe-api:deepseek-v4.1-flash': 'medium',
    }),
    'medium'
  );
  check(
    'пин другой полосы не протекает',
    pinOf(ctx, 'gpt-5.4-mini', 'openai', { '@anthropic:gpt-5.4-mini': 'high' }),
    ''
  );
  check(
    'квалифицированный ключ приоритетнее легаси-ключа',
    pinOf(ctx, 'gpt-5.4-mini', 'openai', {
      'gpt-5.4-mini': 'low',
      '@openai:gpt-5.4-mini': 'high',
    }),
    'high'
  );
  check(
    'легаси-ключ без провайдера работает (совместимость)',
    pinOf(ctx, 'gpt-5.4-mini', 'openai', { 'gpt-5.4-mini': 'low' }),
    'low'
  );
  check(
    'легаси-ключ работает и без провайдера',
    pinOf(ctx, 'gpt-5.4-mini', '', { 'gpt-5.4-mini': 'low' }),
    'low'
  );
  check(
    'написание tolerated: gpt_5.4.mini == gpt-5.4-mini',
    pinOf(ctx, 'gpt-5.4-mini', '', { 'gpt_5.4.mini': 'minimal' }),
    'minimal'
  );
  check('пина нет → Auto (пустая строка)', pinOf(ctx, 'gpt-5.4-mini', 'openai', {}), '');
  check('пустая модель → пусто', pinOf(ctx, '', 'openai', { '@openai:': 'high' }), '');
}

// ── 2. фолбэк без model_identity.js (старый кэш / харнесс) ─────────────────

console.log('pin lookup (фолбэк без model_identity.js)');
{
  const ctx = makeCtx(false);
  checkTrue(
    'модуль действительно не загружен',
    typeof ctx.MarsModelIdentity === 'undefined'
  );
  check(
    'фолбэк: квалифицированный ключ виден для голой модели',
    pinOf(ctx, 'gpt-5.4-mini', 'openai', { '@openai:gpt-5.4-mini': 'high' }),
    'high'
  );
  check(
    'фолбэк: другая полоса не протекает',
    pinOf(ctx, 'gpt-5.4-mini', 'openai', { '@anthropic:gpt-5.4-mini': 'high' }),
    ''
  );
  check(
    'фолбэк: легаси-ключ работает',
    pinOf(ctx, 'gpt-5.4-mini', '', { 'gpt-5.4-mini': 'low' }),
    'low'
  );
  check(
    'фолбэк: custom-провайдер',
    pinOf(ctx, 'deepseek-v4.1-flash', 'custom:plusvibe-api', {
      '@custom:plusvibe-api:deepseek-v4.1-flash': 'medium',
    }),
    'medium'
  );
}

// ── 3. payload чипа: scope=model без стрипа квалификации ───────────────────

console.log('chip payload');
{
  const ctx = makeCtx(true);

  ctx.__pins = { '@openai:gpt-5.4-mini': 'high' };
  ctx._marsModelReasoningOverrides = ctx.__pins;
  ctx.__effortCtx = { model: '@openai:gpt-5.4-mini', provider: 'openai', session_id: 's1' };
  const withPin = ctx._marsReasoningChipPayload('low');
  check('scope=model при существующем пине', withPin.scope, 'model');
  check('квалифицированный model сохранён', withPin.model, '@openai:gpt-5.4-mini');
  check('provider сохранён', withPin.provider, 'openai');
  check('effort уходит как выбран', withPin.effort, 'low');
  check('session_id сохранён', withPin.session_id, 's1');

  ctx.__pins = {};
  ctx._marsModelReasoningOverrides = ctx.__pins;
  const noPin = ctx._marsReasoningChipPayload('low');
  check('без пина scope не выставляется (глобальный дефолт)', noPin.scope, undefined);
  check('без пина model не меняется', noPin.model, '@openai:gpt-5.4-mini');

  // Голая модель + провайдер: пин по полосе тоже включает scope=model.
  ctx.__pins = { '@anthropic:gpt-5.4-mini': 'high' };
  ctx._marsModelReasoningOverrides = ctx.__pins;
  ctx.__effortCtx = { model: 'gpt-5.4-mini', provider: 'anthropic' };
  check('голая модель на своей полосе → scope=model', ctx._marsReasoningChipPayload('low').scope, 'model');

  ctx.__effortCtx = { model: 'gpt-5.4-mini', provider: 'openai' };
  check('та же модель на чужой полосе → без scope', ctx._marsReasoningChipPayload('low').scope, undefined);
}

// ── итог ───────────────────────────────────────────────────────────────────

if (failed) {
  console.error('FAILED: ' + failed + ' из ' + (passed + failed) + ' проверок');
  process.exit(1);
}
console.log('OK: ' + passed + ' проверок');
