#!/usr/bin/env node
/**
 * Consumer-contract тесты: как ui.js РАБОТАЕТ с модулями рендер-ядра.
 *
 * Это не тест модулей «самих по себе» (их гоняет test_upgrade_render_core.mjs), а
 * тест ГРАНИЦЫ между ui.js и ими — той границы, где интеграция ломалась:
 *
 *   - `chat_render_activity_block.js` экспортирует ФАБРИКУ. ui.js звал
 *     `ChatActivityBlock.setMode(...)` на самом экспорте → TypeError, исключение
 *     вылетало из live-рендера и уносило весь живой ход (desktop: turns=0).
 *     Здесь ui.js-функции извлекаются из файла и вызываются против РЕАЛЬНОГО
 *     модуля, так что любая такая ошибка — падение теста, а не «GREEN без
 *     интеграции».
 *   - keyed patch: ход, которым владеет стрим, не должен удаляться рендером;
 *     карточка, вставленная напрямую (compression/handoff), должна двигать
 *     курсор патча, иначе следующий ряд встаёт перед ней.
 *   - scroll-контроллер: `force` обязан переживать frame-coalescing.
 *
 * Запуск: node tests/js/render_consumer_contract.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { createDocument, installGlobals, parseHTML } from '../../scripts/lib/mini_dom.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const UI = fs.readFileSync(path.join(ROOT, 'static', 'ui.js'), 'utf8');
const require = createRequire(import.meta.url);

let passed = 0;
const failures = [];
function ok(name, cond, extra) {
  if (cond) { passed++; return; }
  failures.push(name + (extra ? ' — ' + extra : ''));
}
function eq(name, actual, expected) {
  ok(name, JSON.stringify(actual) === JSON.stringify(expected), `got ${JSON.stringify(actual)} want ${JSON.stringify(expected)}`);
}

/* ── извлечение функций из ui.js (по имени, без исполнения всего файла) ─────── */
function skipString(src, i) {
  const q = src[i];
  for (let j = i + 1; j < src.length; j++) {
    if (src[j] === '\\') { j++; continue; }
    if (src[j] === q) return j;
  }
  return src.length;
}
function extractFunction(src, name) {
  const marker = 'function ' + name + '(';
  const start = src.indexOf(marker);
  if (start < 0) throw new Error('ui.js: функция ' + name + ' не найдена');
  let depth = 0;
  for (let i = src.indexOf('{', start); i < src.length; i++) {
    const c = src[i];
    if (c === '"' || c === "'" || c === '`') { i = skipString(src, i); continue; }
    if (c === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); continue; }
    if (c === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 1; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('ui.js: несбалансированные скобки в ' + name);
}
function extractLetDeclaration(src, name) {
  const re = new RegExp('(?:^|\\n)(let|var|const) ' + name + '\\s*=[^;]*;');
  const m = re.exec(src);
  if (!m) throw new Error('ui.js: объявление ' + name + ' не найдено');
  return m[0].trim();
}

const doc = createDocument();
installGlobals(doc);

/* Свежесобранный ряд с тем же ключом.
 *
 * ui.js строит ряд ЗАНОВО и отдаёт его в `place()` — mounted-узел никогда не
 * клонируется. mini_dom не реализует `cloneNode`, поэтому «пересборка» здесь —
 * это re-parse разметки: тот же ключ/id, но ДРУГОЙ объект. Именно это и должен
 * проверять контракт (переиспользование по ключу, а не по идентичности ссылки). */
function rebuilt(node) {
  return parseHTML(node.outerHTML, doc)[0];
}

/* ui.js-код исполняется в общем глобале (document/S/t из mini_dom), но каждая
 * загрузка — в собственной области видимости: `let _activityModeFacade` объявлен
 * в файле один раз и при повторной загрузке в тот же лексический скоуп упал бы. */
function loadUi(names, exported) {
  const decls = names.filter((n) => DECLS.has(n)).map((n) => extractLetDeclaration(UI, n)).join('\n');
  const body = names.filter((n) => !DECLS.has(n)).map((n) => extractFunction(UI, n)).join('\n');
  const code = `(function(){\n${decls}\n${body}\nreturn ${exported};\n})()`;
  return vm.runInThisContext(code, { filename: 'ui.js#' + exported });
}
// `let`-объявления, которые живут рядом с функциями и должны попасть в скоуп.
const DECLS = new Set(['_activityModeFacade']);
const loadActivityModule = () => loadUi(['_activityModeFacade', '_activityModeModule'], '_activityModeModule');

/* ── 1. activity block: ui.js обязан получить INSTANCE, а не factory ──────── */
{
  const AB = require(path.join(ROOT, 'static', 'chat_render_activity_block.js'));
  ok('модуль экспортирует фабрику', typeof AB.create === 'function');
  ok('на самом экспорте НЕТ setMode (это и было причиной падения)',
    typeof AB.setMode !== 'function');

  globalThis.ChatActivityBlock = AB;
  const accessor = loadActivityModule();
  const facade = accessor();

  ok('_activityModeModule() вернул объект', !!facade);
  for (const m of ['ensureChrome', 'setMode', 'setState', 'disclosureFor', 'syncVisibility']) {
    ok(`facade.${m} — функция (это вызывают из _applyActivityModeToHost)`,
      !!facade && typeof facade[m] === 'function', typeof (facade && facade[m]));
  }
  ok('facade.Disclosure.set — функция (вызывает _toggleActivityWindow)',
    !!facade && !!facade.Disclosure && typeof facade.Disclosure.set === 'function');
  ok('pure-хелперы фабрики доступны через facade',
    typeof facade.normalizeMode === 'function' && typeof facade.turnKey === 'function');
  ok('фасад мемоизирован (один инстанс на страницу)', facade === accessor());

  // Легаси-сборка, где методы лежат прямо на глобале, должна приниматься как есть.
  const legacy = { setMode() {}, ensureChrome() {}, setState() {}, disclosureFor() {}, syncVisibility() {}, Disclosure: { set() {} } };
  globalThis.ChatActivityBlock = legacy;
  ok('легаси-форма (методы на глобале) принимается без обёртки', loadActivityModule()() === legacy);
  globalThis.ChatActivityBlock = AB;
}

/* ── 2. _applyActivityModeToHost: реальный вызов на реальном блоке ─────────── */
{
  const AB = require(path.join(ROOT, 'static', 'chat_render_activity_block.js'));
  globalThis.ChatActivityBlock = AB;
  globalThis.S = { session: { session_id: 'sess' }, messages: [] };
  globalThis.chatActivityMode = () => 'stream_window';
  globalThis.t = (k) => k;

  const apply = loadUi(
    ['_activityModeFacade', '_activityModeModule', '_activityShellLabel', '_activityContainersIn', '_applyActivityModeToHost'],
    '_applyActivityModeToHost',
  );

  const host = doc.createElement('div');
  doc.body.appendChild(host);
  const turnKey = 'sess:t1';
  const chrome = AB.create({ document: doc }).ensureChrome(host, { turnKey, mode: 'stream_window', state: 'live', label: 'Working' });
  for (let i = 0; i < 2; i++) {
    const r = doc.createElement('div');
    r.className = 'tool-card-row';
    r.setAttribute('data-activity-row', '1');
    r.textContent = 'tool ' + i;
    chrome.list.appendChild(r);
  }
  const rowNodes = Array.from(chrome.list.children);

  let threw = null;
  try { apply(host, 'stream_quiet', 'live', turnKey, ['live:abc']); }
  catch (e) { threw = e; }
  ok('quiet применяется без исключения (был TypeError: AB.setMode is not a function)', !threw, threw && threw.message);
  ok('quiet: контейнер помечен режимом', chrome.root.getAttribute('data-activity-mode') === 'stream_quiet');
  ok('quiet: shell скрыт, ряды в DOM', chrome.root.style.display === 'none' && chrome.list.children.length === 2);

  threw = null;
  try { apply(host, 'stream_full', 'live', turnKey, ['live:abc']); }
  catch (e) { threw = e; }
  ok('full применяется без исключения', !threw, threw && threw.message);
  ok('full: shell снова виден', chrome.root.style.display === '');
  ok('full: те же узлы рядов (ничего не пересобрано)',
    Array.from(chrome.list.children).every((n, i) => n === rowNodes[i]));
  ok('full: data-activity-mode обновлён', chrome.root.getAttribute('data-activity-mode') === 'stream_full');
}

/* ── 3. keyed patch: живой ход принадлежит стриму ──────────────────────────── */
{
  const Patch = require(path.join(ROOT, 'static', 'chat_render_patch.js'));
  const inner = doc.createElement('div');
  inner.id = 'msgInner';
  doc.body.appendChild(inner);

  const userRow = doc.createElement('div');
  userRow.className = 'msg-row';
  userRow.id = 'msg-user-0';
  inner.appendChild(userRow);
  const live = doc.createElement('div');
  live.className = 'msg-row assistant-turn';
  live.id = 'liveAssistantTurn';
  inner.appendChild(live);

  // Мид-стрим снапшот: серверная копия хода без `_live`, рендер не строит для неё ряд.
  const streaming = true;
  const ctx = Patch.begin(inner, { protect: (n) => streaming && n.id === 'liveAssistantTurn' });
  Patch.place(ctx, rebuilt(userRow));
  const stats = Patch.finish(ctx);
  ok('ряд живого хода НЕ удалён посреди стрима', inner.querySelector('#liveAssistantTurn') === live,
    `kept=${stats.kept} removed=${stats.removed}`);
  eq('patch отметил удержанный ряд', stats.kept, 1);
  eq('patch ничего не удалил', stats.removed, 0);

  // После `done` защита снимается — обычный settled-рендер ряд заменяет.
  const ctx2 = Patch.begin(inner, { protect: (n) => false && n.id === 'liveAssistantTurn' });
  Patch.place(ctx2, rebuilt(userRow));
  const settledRow = doc.createElement('div');
  settledRow.className = 'msg-row assistant-turn';
  settledRow.setAttribute('data-msg-idx', '1');
  Patch.place(ctx2, settledRow);
  const stats2 = Patch.finish(ctx2);
  ok('после стрима старый ряд заменён settled-рендером', inner.querySelector('#liveAssistantTurn') === null,
    `removed=${stats2.removed}`);
  ok('settled-ряд на месте', inner.querySelector('[data-msg-idx="1"]') === settledRow);
}

/* ── 4. курсор патча: карточка, вставленная напрямую, не переворачивает порядок ── */
{
  const Patch = require(path.join(ROOT, 'static', 'chat_render_patch.js'));
  const inner = doc.createElement('div');
  doc.body.appendChild(inner);
  const first = doc.createElement('div');
  first.setAttribute('data-render-key', 'k1');
  inner.appendChild(first);

  const ctx = Patch.begin(inner);
  Patch.place(ctx, rebuilt(first));             // переиспользует first, курсор = first
  // compression/handoff карточка вставляется напрямую после курсора
  const card = doc.createElement('div');
  card.className = 'compression-turn';
  card.setAttribute('data-render-key', 'compression:1');
  inner.insertBefore(card, first.nextSibling);
  Patch.adopt(ctx, card);
  // следующий ряд обязан встать ПОСЛЕ карточки
  const next = doc.createElement('div');
  next.setAttribute('data-render-key', 'k2');
  Patch.place(ctx, next);
  Patch.finish(ctx);
  const order = Array.from(inner.children).map((n) => n.getAttribute('data-render-key')).join(',');
  eq('порядок после прямой вставки карточки сохранён', order, 'k1,compression:1,k2');

  // adopt не двигает курсор на узел, который встал НЕ сразу за ним.
  const inner2 = doc.createElement('div');
  doc.body.appendChild(inner2);
  const a = doc.createElement('div'); a.setAttribute('data-render-key', 'a'); inner2.appendChild(a);
  const c = doc.createElement('div'); c.setAttribute('data-render-key', 'c'); inner2.appendChild(c);
  const ctx2 = Patch.begin(inner2);
  Patch.place(ctx2, rebuilt(a));
  const mid = doc.createElement('div'); mid.setAttribute('data-render-key', 'mid');
  inner2.insertBefore(mid, a);                    // вставлен ПЕРЕД курсором
  Patch.adopt(ctx2, mid);
  const tail = doc.createElement('div'); tail.setAttribute('data-render-key', 'tail');
  Patch.place(ctx2, tail);
  Patch.finish(ctx2);
  eq('adopt игнорирует узел не на курсоре',
    Array.from(inner2.children).map((n) => n.getAttribute('data-render-key')).join(','), 'mid,a,tail');
}

/* ── 5. scroll: force переживает frame-coalescing ─────────────────────────── */
{
  const Scroll = require(path.join(ROOT, 'static', 'chat_scroll_controller.js'));
  const scroller = { scrollTop: 0, scrollHeight: 2000, clientHeight: 600,
    get max() { return this.scrollHeight - this.clientHeight; } };
  let frames = [];
  let follow = false;                       // читатель откреплён
  const env = {
    getScroller: () => scroller,
    canFollow: ({ explicit, force }) => (explicit || force ? true : follow),
    raf: (cb) => { frames.push(cb); return frames.length; },
    caf: () => { frames = []; },
    setTimeout: () => 0,
    clearTimeout: () => {},
    makeObserver: () => ({ observe() {}, disconnect() {} }),
    now: () => 0,
  };
  const ctl = Scroll.create(env);
  const flush = () => { let guard = 0; while (frames.length && guard++ < 50) { const f = frames; frames = []; f.forEach((cb) => cb()); } };

  // Первый (не forced) write занимает кадр, второй приходит в тот же кадр —
  // коалесится, но он forced и не должен потерять force.
  ctl.write('stream');                      // откреплён → отказ
  flush();
  scroller.scrollTop = 0;
  const refusedBefore = ctl.stats().refused;
  ctl.write('stream');                      // займёт следующий кадр, откажется
  flush();
  ok('откреплённый читатель: обычный write отказан', ctl.stats().refused > refusedBefore);

  scroller.scrollTop = 0;
  ctl.write('stream');                      // кадр занят → коалесится
  ctl.write('explicit-bottom', { force: true });  // forced, тоже коалесится
  const writesBefore = ctl.stats().writes;
  flush();
  ok('coalesced forced write всё-таки прокручивает', ctl.stats().writes === writesBefore + 1,
    `writes ${writesBefore} -> ${ctl.stats().writes}`);
  eq('прокрутка встала на дно', scroller.scrollTop, scroller.max);

  // forced-запрос не «протекает» в следующий кадр.
  scroller.scrollTop = 0;
  follow = false;
  const refused2 = ctl.stats().refused;
  ctl.write('stream');
  flush();
  ok('force не залипает на последующие кадры', ctl.stats().refused > refused2 && scroller.scrollTop === 0);
}

/* ── отчёт ────────────────────────────────────────────────────────────────── */
if (failures.length) {
  console.error('FAIL ' + failures.length + ' / ' + (passed + failures.length));
  for (const f of failures) console.error('  ✗ ' + f);
  process.exit(1);
}
console.log('OK: ' + passed + ' проверок');
