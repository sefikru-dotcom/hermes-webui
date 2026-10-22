#!/usr/bin/env node
/**
 * Поведенческие тесты appearance-состояния и автосейва (Issue: modes audit P1).
 *
 * Гоняют РЕАЛЬНЫЙ static/appearance_store.js и РЕАЛЬНЫЕ функции panels.js
 * (извлекаются из файла по имени), в изолированном vm-контексте: ни браузера,
 * ни jsdom, ни живых настроек. Сеть заменена фейковым `_enqueueSettingsPost`,
 * который складывает тела POST в массив — тесты проверяют ровно то, что уйдёт
 * на сервер.
 *
 * Запуск: node tests/js/appearance_store_behavior.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const STORE_SRC = fs.readFileSync(path.join(ROOT, 'static', 'appearance_store.js'), 'utf8');
const PANELS_SRC = fs.readFileSync(path.join(ROOT, 'static', 'panels.js'), 'utf8');

// ── извлечение функций из panels.js ─────────────────────────────────────────

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
  const start = PANELS_SRC.indexOf(marker);
  if (start < 0) throw new Error('panels.js: функция ' + name + ' не найдена');
  let depth = 0;
  for (let i = PANELS_SRC.indexOf('{', start); i < PANELS_SRC.length; i++) {
    const c = PANELS_SRC[i];
    if (c === '"' || c === "'" || c === '`') { i = skipString(PANELS_SRC, i); continue; }
    if (c === '/' && PANELS_SRC[i + 1] === '/') { i = PANELS_SRC.indexOf('\n', i); continue; }
    if (c === '/' && PANELS_SRC[i + 1] === '*') { i = PANELS_SRC.indexOf('*/', i) + 1; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return PANELS_SRC.slice(start, i + 1); }
  }
  throw new Error('panels.js: несбалансированные скобки в ' + name);
}

// Только те функции, которые участвуют в пути «изменение → diff → POST → UI».
const EXTRACTED = [
  '_canonicalActivityMode',
  '_currentActivityMode',
  '_structuredCodeViewFromUi',
  '_composerControlVisibilityPayload',
  '_appearancePayloadFromUi',
  '_appearancePayloadFromUiLegacy',
  '_scheduleAppearanceAutosave',
  '_pickChatActivityDisplayMode',
  '_ensureAppearanceStore',
  '_syncChatActivityDisplayModeControl',
  '_syncTransparentEventTimestampsControl',
  '_applyActivityModeFast',
  '_registerActivityModeRenderer',
  '_activityModeRendererFn',
  '_onAppearanceStoreStatus',
  '_onAppearanceStoreSaved',
  '_onAppearanceStoreError',
];

// Настоящий _applyAppearanceKeyValue переименовывается, чтобы тест мог и
// записать факт применения ключа, и выполнить реальную DOM-синхронизацию.
function extractApplyKey() {
  return extractFn('_applyAppearanceKeyValue').replace(
    'function _applyAppearanceKeyValue(',
    'function _applyAppearanceKeyValueReal('
  );
}

// ── фейковый DOM / сеть / рантайм ───────────────────────────────────────────

const PRELUDE = `
var posts=[];              // тела POST /api/settings по порядку
var pending=[];            // незавершённые запросы (ручной режим)
var autoRespond=true;      // true — запрос завершается сам в микротаске
var statusLog=[];          // состояния индикатора автосейва
var applied=[];            // ключи, применённые к UI
var titlebar=[];           // синхронизации кнопки режима в шапке
var runtimeSync=[];        // _applyAppearanceSavedToRuntime
var legacySync=0;          // сколько раз планировался старый rebuild
var thinkingClasses=0;     // _applyThinkingModeClasses
var serverState={};        // «сервер»: то, что реально сохранено

var _settingsAppearanceControlsBound=false;
var _appearanceStoreRef=null;
var _appearanceStoreConfigured=false;
var _appliedAppearance={};
var _activityModeRenderer=null;

var dom=new Map();

function __el(id,init){
  var e=Object.assign({
    id:id, value:'', checked:false,
    style:{},
    classList:{toggle:function(){},add:function(){},remove:function(){},contains:function(){return false;}},
    setAttribute:function(){}, getAttribute:function(){return null;}, addEventListener:function(){}
  },init||{});
  dom.set(id,e);
  return e;
}
function __clearDom(){ dom.clear(); }
function $(id){ return dom.get(id)||null; }

var localStorage={
  _v:{},
  getItem:function(k){ return Object.prototype.hasOwnProperty.call(this._v,k)?this._v[k]:null; },
  setItem:function(k,v){ this._v[k]=String(v); },
  removeItem:function(k){ delete this._v[k]; }
};

var document={
  readyState:'complete',
  body:{appendChild:function(){}},
  documentElement:{classList:{toggle:function(){},add:function(){},remove:function(){},contains:function(){return false;}},dataset:{}},
  querySelector:function(){ return null; },
  querySelectorAll:function(){ return []; },
  getElementById:function(id){ return $(id); },
  createElement:function(){ return {style:{},classList:{add:function(){},remove:function(){}},setAttribute:function(){},addEventListener:function(){},appendChild:function(){}}; },
  addEventListener:function(){}
};
var window=this;
window.addEventListener=function(){};
window.localStorage=localStorage;
window.document=document;
window.innerWidth=1200;

function t(k){ return k; }
function esc(s){ return String(s); }

function _applyAppearanceKeyValue(key,value,meta){
  applied.push({key:key,value:value,reason:(meta&&meta.reason)||''});
  return _applyAppearanceKeyValueReal(key,value,meta);
}
function _setAppearanceAutosaveStatus(state){ statusLog.push(state); }
function _rememberAppearanceSaved(payload){ runtimeSync.push({memo:payload}); }
function _applyAppearanceSavedToRuntime(saved,payload){ runtimeSync.push({saved:saved,payload:payload}); }
function _applyThinkingModeClasses(){ thinkingClasses++; }
function _syncTitlebarActivityModeButton(mode){ titlebar.push(mode); }
function _scheduleLegacyActivityRenderSync(){ legacySync++; }
function _composerControlDefsForSettings(){ return [{key:'hide_composer_attach'},{key:'hide_composer_mic'}]; }
function _getComposerControlOrder(){ return ['btnAttach']; }
function _getHiddenTabs(){ return []; }
function _getTabOrder(){ return []; }
function _setComposerControlOrder(order){ return Array.isArray(order)?order.slice():[]; }
function _setHiddenTabs(panels){ localStorage.setItem('hermes-webui-hidden-tabs',JSON.stringify(panels||[])); }
function _setTabOrder(panels){ localStorage.setItem('hermes-webui-tab-order',JSON.stringify(panels||[])); }
function _renderComposerControlChips(){}
function _renderComposerSituationalControlChips(){}
function _renderTabVisibilityChips(){}
function _applyTabVisibility(){}
function _applyTabOrder(){}

// Фейковый POST /api/settings. Порядок вызовов и тела — то, что проверяют тесты.
function _enqueueSettingsPost(options){
  var body=JSON.parse(options.body);
  posts.push(body);
  return new Promise(function(resolve,reject){
    var req={
      body:body,
      done:false,
      ok:function(){
        if(req.done) return;
        req.done=true;
        Object.assign(serverState,body);
        resolve(Object.assign({},serverState));
      },
      fail:function(err){
        if(req.done) return;
        req.done=true;
        reject(err||new Error('HTTP 500'));
      }
    };
    pending.push(req);
    if(autoRespond) queueMicrotask(function(){ req.ok(); });
  });
}

function __state(){
  return {
    posts:posts, pending:pending, statusLog:statusLog, applied:applied,
    titlebar:titlebar, runtimeSync:runtimeSync, legacySync:legacySync,
    thinkingClasses:thinkingClasses, serverState:serverState, dom:dom,
    bound:_settingsAppearanceControlsBound
  };
}
function __setBound(v){ _settingsAppearanceControlsBound=!!v; }
function __setAutoRespond(v){ autoRespond=!!v; }
function __resetLogs(){
  posts.length=0; pending.length=0; statusLog.length=0; applied.length=0;
  titlebar.length=0; runtimeSync.length=0; legacySync=0; thinkingClasses=0;
}
function __pendingRequests(){ return pending.filter(function(r){ return !r.done; }); }
`;

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  queueMicrotask,
  setInterval,
  clearInterval,
  Promise,
  Map,
  Set,
  Date,
  JSON,
  Math,
  Object,
  Array,
  String,
  Number,
  Boolean,
  Error,
  parseInt,
  parseFloat,
  isFinite,
  isNaN,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  PRELUDE + '\n' + STORE_SRC + '\n' + extractApplyKey() + '\n' + EXTRACTED.map(extractFn).join('\n\n'),
  sandbox,
  { filename: 'appearance_behavior_bundle.js' }
);

const store = sandbox.AppearanceStore;
if (!store) throw new Error('AppearanceStore не зарегистрирован в window');

// debounce 350ms из панели в тестах не нужен: ждём POST явно через flush().
sandbox._ensureAppearanceStore();
store.configure({ debounceMs: 0 });

// ── мини-фреймворк ──────────────────────────────────────────────────────────

let failures = 0;
let checks = 0;

function ok(cond, msg) {
  checks++;
  if (cond) return;
  failures++;
  console.error('  ✗ ' + msg);
}

function eq(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  ok(a === e, msg + ' — ожидалось ' + e + ', получено ' + a);
}

function section(name) {
  console.log('• ' + name);
}

const SERVER = {
  theme: 'dark',
  skin: 'default',
  font_size: 'default',
  chat_activity_display_mode: 'stream_window',
  transparent_stream_event_timestamps: true,
  thinking_mode: 'auto',
  show_tool_calls: true,
  auto_collapse_previous_turns: true,
  worklog_details_expanded_default: false,
  auto_scroll_follow: true,
  session_jump_buttons: false,
  session_endless_scroll: false,
  render_user_markdown: false,
  large_text_paste_as_attachment: true,
  project_quick_create_buttons: false,
  show_titlebar_profile: false,
  structured_code_default_view: 'auto',
  structured_code_auto_tree_lines: 10,
  hide_composer_attach: false,
  hide_composer_mic: false,
  composer_control_order: ['btnAttach'],
  hidden_tabs: [],
  tab_order: [],
  default_model: 'hermes-4', // не appearance-ключ: обязан игнорироваться
};

function fresh() {
  store.reset();
  sandbox.__resetLogs();
  sandbox.__clearDom();
  sandbox.__setBound(false);
  sandbox.__setAutoRespond(true);
  Object.keys(SERVER).forEach((k) => { sandbox.serverState[k] = SERVER[k]; });
  store.hydrate(SERVER);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// store вызывает opts.save в микротаске — даём ей выполниться.
const tick = () => sleep(0);

// ── тесты ───────────────────────────────────────────────────────────────────

async function main() {
  // 1. Регресс P1: кнопка режима в шапке ДО открытия Settings.
  section('header mode до Settings: в сеть уходит только свой ключ');
  {
    store.reset();
    sandbox.__resetLogs();
    sandbox.__clearDom();
    sandbox.__setBound(false);
    const modeSel = sandbox.__el('settingsChatActivityDisplayMode', { value: 'stream_window' });
    ok(store.isHydrated() === false, 'store не гидратирован (Settings ни разу не открывался)');

    sandbox._pickChatActivityDisplayMode('stream_quiet');

    // Быстрое применение — синхронно, до сети.
    eq(modeSel.value, 'stream_quiet', 'селект режима обновлён сразу');
    eq(sandbox.window._chatActivityDisplayMode, 'stream_quiet', 'window._chatActivityDisplayMode обновлён');
    ok(sandbox.__state().thinkingClasses === 1, 'html-классы режима пересчитаны');
    eq(sandbox.__state().posts.length, 0, 'POST отложен дебаунсом');

    await store.flush();

    eq(sandbox.__state().posts.length, 1, 'ровно один POST');
    eq(sandbox.__state().posts[0], { chat_activity_display_mode: 'stream_quiet' },
      'тело POST содержит только chat_activity_display_mode');
    ok(sandbox.__state().posts[0].theme === undefined, 'theme не перезаписан');
    ok(sandbox.__state().posts[0].skin === undefined, 'skin не перезаписан');
    ok(sandbox.__state().posts[0].font_size === undefined, 'font_size не перезаписан');
    ok(sandbox.__state().posts[0].thinking_mode === undefined, 'thinking_mode не перезаписан');
    ok(sandbox.__state().posts[0].show_tool_calls === undefined, 'show_tool_calls не перезаписан');
    ok(sandbox.__state().legacySync <= 1, 'транскрипт не перестраивается на каждое переключение');
    eq(store.diff(), {}, 'после подтверждения diff пуст');
  }

  // 2. До Settings: DOM-контролы не читаются как значения по умолчанию.
  section('payload до гидратации контролов = только явные правки');
  {
    fresh();
    sandbox.__el('settingsTheme', { value: 'light' });      // «разметка»: не дефолт пользователя
    sandbox.__el('settingsSkin', { value: 'neon' });
    sandbox.__el('settingsShowToolCalls', { checked: false });
    sandbox.__setBound(false);
    store.set('chat_activity_display_mode', 'stream_full', { noSave: true });
    const payload = sandbox._appearancePayloadFromUi();
    eq(payload, { chat_activity_display_mode: 'stream_full' },
      'неподключённые контролы не попадают в payload');
  }

  // 3. После Settings: гидратированный DOM не создаёт ложных изменений.
  section('после Settings: совпадающий DOM не даёт POST');
  {
    fresh();
    const modeSel = sandbox.__el('settingsChatActivityDisplayMode', { value: 'stream_window' });
    sandbox.__el('settingsTheme', { value: 'dark' });
    sandbox.__el('settingsSkin', { value: 'default' });
    sandbox.__el('settingsFontSize', { value: 'default' });
    sandbox.__el('settingsTransparentEventTimestamps', { checked: true });
    sandbox.__el('settingsShowToolCalls', { checked: true });
    sandbox.__el('settingsAutoCollapsePreviousTurns', { checked: true });
    sandbox.__el('settingsAutoScrollFollow', { checked: true });
    sandbox.__el('settingsWorklogDetailsExpandedDefault', { checked: false });
    sandbox.__setBound(true);

    sandbox._scheduleAppearanceAutosave();
    await store.flush();
    eq(sandbox.__state().posts.length, 0, 'пустой diff не превращается в POST');

    modeSel.value = 'stream_full';
    sandbox._scheduleAppearanceAutosave();
    await store.flush();
    eq(sandbox.__state().posts.length, 1, 'одно изменение — один POST');
    eq(sandbox.__state().posts[0], { chat_activity_display_mode: 'stream_full' },
      'в POST только изменённый ключ');
  }

  // 4. Последовательные сохранения двигают baseline.
  section('последовательные сохранения');
  {
    fresh();
    store.set('theme', 'light');
    await store.flush();
    eq(sandbox.__state().posts[0], { theme: 'light' }, 'первый POST: theme');
    eq(store.savedValue('theme'), 'light', 'baseline theme сдвинут');
    eq(store.diff(), {}, 'diff пуст после первого сохранения');

    store.set('skin', 'neon');
    await store.flush();
    eq(sandbox.__state().posts.length, 2, 'второй POST');
    eq(sandbox.__state().posts[1], { skin: 'neon' }, 'второй POST не повторяет theme');
    eq(store.savedValue('skin'), 'neon', 'baseline skin сдвинут');
    eq(sandbox.__state().statusLog[sandbox.__state().statusLog.length - 1], 'saved',
      'индикатор в состоянии saved');
  }

  // 5. Concurrent: правка во время in-flight POST не теряется и не дублируется.
  section('concurrent saves: склейка без потерь и дублей');
  {
    fresh();
    sandbox.__setAutoRespond(false);

    store.set('chat_activity_display_mode', 'stream_full');
    const first = store.saveNow();
    await tick();
    eq(sandbox.__state().posts.length, 1, 'первый POST стартовал');
    eq(sandbox.__state().posts[0], { chat_activity_display_mode: 'stream_full' }, 'тело первого POST');

    store.set('theme', 'light');
    store.saveNow(); // второй вызов при in-flight только ставит очередь
    await tick();
    eq(sandbox.__state().posts.length, 1, 'второй POST не стартует параллельно');

    const reqs = sandbox.__pendingRequests();
    eq(reqs.length, 1, 'в полёте ровно один запрос');
    reqs[0].ok();
    await first;
    await tick();

    eq(sandbox.__state().posts.length, 2, 'после подтверждения ушёл второй POST');
    eq(sandbox.__state().posts[1], { theme: 'light' },
      'второй POST содержит только новую правку (без повтора режима)');
    sandbox.__pendingRequests().forEach((r) => r.ok());
    await store.flush();
    eq(store.diff(), {}, 'оба изменения сохранены');
    eq(store.savedValue('chat_activity_display_mode'), 'stream_full', 'baseline режима');
    eq(store.savedValue('theme'), 'light', 'baseline темы');
  }

  // 6. Ошибка сохранения: baseline не двигается, retry отправляет то же.
  section('error rollback + retry');
  {
    fresh();
    sandbox.__setAutoRespond(false);

    store.set('theme', 'light');
    const p = store.saveNow();
    await tick();
    sandbox.__pendingRequests()[0].fail(new Error('boom'));
    await p;

    eq(store.status(), 'error', 'статус error');
    eq(store.diff(), { theme: 'light' }, 'изменение осталось в diff (не потеряно)');
    eq(store.savedValue('theme'), 'dark', 'baseline не сдвинут при ошибке');
    eq(sandbox.__state().statusLog[sandbox.__state().statusLog.length - 1], 'failed',
      'индикатор показал failed');

    sandbox.__setAutoRespond(true);
    await store.retry();
    eq(sandbox.__state().posts.length, 2, 'retry отправил повторный POST');
    eq(sandbox.__state().posts[1], { theme: 'light' }, 'retry отправляет тот же diff');
    eq(store.diff(), {}, 'после успешного retry diff пуст');
    eq(store.savedValue('theme'), 'light', 'baseline сдвинут только после успеха');
  }

  // 6b. Явный rollback после неудачного сохранения: state + UI возвращаются
  //     к подтверждённому сервером значению, индикатор ошибки снят, POST не ушёл.
  section('явный rollback после неудачного save');
  {
    fresh();
    sandbox.__setAutoRespond(false);
    sandbox.__resetLogs();

    store.set('theme', 'light');
    const p = store.saveNow();
    await tick();
    sandbox.__pendingRequests()[0].fail(new Error('boom'));
    await p;
    eq(store.get('theme'), 'light', 'оптимистичное значение осталось в UI до отката');

    const postsBefore = sandbox.__state().posts.length;
    const changed = store.rollback();

    eq(changed, ['theme'], 'rollback вернул список откатанных ключей');
    eq(store.get('theme'), 'dark', 'значение вернулось к серверному');
    eq(store.diff(), {}, 'diff очищен — retry больше нечего отправлять');
    eq(store.status(), 'idle', 'индикатор ошибки снят');
    eq(store.lastError(), null, 'lastError очищен');
    eq(sandbox.__state().posts.length, postsBefore, 'rollback не делает POST');
    // UI получил откат через apply(reason:'revert').
    const revertApply = sandbox.__state().applied.filter((a) => a.key === 'theme' && a.reason === 'revert');
    eq(revertApply.length, 1, 'apply с reason=revert вызван один раз');
    eq(revertApply[0].value, 'dark', 'в UI применено серверное значение');
  }

  // 6c. Concurrent patch: две правки одного ключа во время полёта дают один
  //     следующий POST с последним значением (не два, не потеря).
  section('concurrent patch одного ключа: последнее значение побеждает');
  {
    fresh();
    sandbox.__setAutoRespond(false);

    store.set('theme', 'light');
    const first = store.saveNow();
    await tick();
    eq(sandbox.__state().posts.length, 1, 'первый POST в полёте');

    store.set('theme', 'neon');   // правка того же ключа во время POST
    store.saveNow();
    store.set('theme', 'ocean');  // и ещё одна — должна склеиться
    store.saveNow();
    await tick();
    eq(sandbox.__state().posts.length, 1, 'параллельного POST нет');

    sandbox.__pendingRequests()[0].ok();
    await first;
    await tick();

    eq(sandbox.__state().posts.length, 2, 'ровно один догоняющий POST');
    eq(sandbox.__state().posts[1], { theme: 'ocean' },
      'догоняющий POST содержит последнее значение, без промежуточного');
    sandbox.__pendingRequests().forEach((r) => r.ok());
    await store.flush();
    eq(store.diff(), {}, 'все правки сохранены');
    eq(store.savedValue('theme'), 'ocean', 'baseline = последнее значение');
  }

  // 6d. Concurrent patch разных ключей во время полёта: обе правки в одном
  //     догоняющем POST, каждая ровно один раз.
  section('concurrent patch разных ключей: один догоняющий POST');
  {
    fresh();
    sandbox.__setAutoRespond(false);

    store.set('theme', 'light');
    const first = store.saveNow();
    await tick();
    store.set('skin', 'neon');
    store.set('font_size', 'large');
    store.saveNow();
    await tick();
    eq(sandbox.__state().posts.length, 1, 'параллельного POST нет');

    sandbox.__pendingRequests()[0].ok();
    await first;
    await tick();
    eq(sandbox.__state().posts[1], { skin: 'neon', font_size: 'large' },
      'догоняющий POST содержит ровно новые ключи');
    sandbox.__pendingRequests().forEach((r) => r.ok());
    await store.flush();
    eq(store.diff(), {}, 'diff пуст');
    eq(sandbox.__state().posts.length, 2, 'итого два POST, без дублей');
  }

  // 7. Ошибка + правка во время полёта: обе правки уходят одним POST.
  section('ошибка + правка во время полёта');
  {
    fresh();
    sandbox.__setAutoRespond(false);

    store.set('theme', 'light');
    const p = store.saveNow();
    await tick();
    store.set('skin', 'neon');   // во время in-flight
    store.saveNow();             // ставит очередь, второй POST не стартует
    await tick();
    eq(sandbox.__state().posts.length, 1, 'параллельного POST нет');
    sandbox.__pendingRequests()[0].fail(new Error('boom'));
    await p;
    await tick();

    eq(sandbox.__state().posts.length, 2, 'после ошибки ушёл объединённый POST');
    eq(sandbox.__state().posts[1], { theme: 'light', skin: 'neon' },
      'объединённый POST содержит обе правки');
    sandbox.__pendingRequests().forEach((r) => r.ok());
    await store.flush();
    eq(store.diff(), {}, 'после подтверждения diff пуст');
  }

  // 8. Гидратация не перетирает локальную несохранённую правку.
  section('hydrate не перетирает локальную правку');
  {
    fresh();
    store.set('theme', 'light', { noSave: true });
    store.hydrate(Object.assign({}, SERVER, { theme: 'dark', skin: 'default' }));
    eq(store.get('theme'), 'light', 'локальное значение сохранено');
    eq(store.diff(), { theme: 'light' }, 'diff остался');
    eq(store.savedValue('theme'), 'dark', 'baseline обновлён с сервера');
  }

  // 9. Посторонние ключи не попадают в состояние и в сеть.
  section('посторонние ключи отбрасываются');
  {
    fresh();
    eq(store.set('default_model', 'x'), false, 'set постороннего ключа отклонён');
    eq(store.diff(), {}, 'diff пуст');
    ok(store.get('default_model') === undefined, 'значение не сохранено в store');
    eq(store.set('activity_feed_expanded_default', true) !== false, true, 'легаси-ключ принимается');
    eq(store.diff(), { worklog_details_expanded_default: true }, 'легаси-ключ канонизирован');
  }

  // 10. Полное сохранение (Save all) двигает baseline: автосейв не дублирует.
  section('после полного сохранения автосейв молчит');
  {
    fresh();
    store.set('theme', 'light', { noSave: true });
    store.set('skin', 'neon', { noSave: true });
    store.adoptSaved({ theme: 'light', skin: 'neon' }, ['theme', 'skin']);
    eq(store.diff(), {}, 'diff пуст после подтверждения полного сохранения');

    sandbox.__setBound(true);
    sandbox.__el('settingsTheme', { value: 'light' });
    sandbox.__el('settingsSkin', { value: 'neon' });
    sandbox._scheduleAppearanceAutosave();
    await store.flush();
    eq(sandbox.__state().posts.length, 0, 'автосейв не отправляет уже сохранённое');
  }

  // 11. Применение режима — через существующие ui API, без rebuild.
  section('быстрое применение режима');
  {
    fresh();
    sandbox.__resetLogs();
    const modeSel = sandbox.__el('settingsChatActivityDisplayMode', { value: 'stream_window' });
    store.set('chat_activity_display_mode', 'stream_full');
    eq(modeSel.value, 'stream_full', 'селект синхронизирован');
    eq(sandbox.window._transparentStream, true, 'window._transparentStream обновлён');
    eq(sandbox.__state().titlebar, ['stream_full'], 'кнопка в шапке синхронизирована');
    ok(sandbox.__state().legacySync <= 1, 'rebuild запланирован не более одного раза');
    await store.flush();
  }

  // 12. Callback point для автора рендер-слоя: зарегистрированный рендерер
  //     получает канонический режим + reason, legacy-rebuild не планируется.
  section('рендерер режима: регистрация и вызов');
  {
    fresh();
    sandbox.__resetLogs();
    const calls = [];
    eq(sandbox._registerActivityModeRenderer((mode, meta) => calls.push({ mode, meta })), true,
      'рендерер зарегистрирован');
    eq(sandbox._registerActivityModeRenderer(null), false, 'не-функция отклонена');

    store.set('chat_activity_display_mode', 'stream_quiet');
    eq(calls.length, 1, 'рендерер вызван синхронно на set');
    eq(calls[0].mode, 'stream_quiet', 'режим в каноне');
    eq(calls[0].meta.reason, 'local', 'reason=local для действия пользователя');
    eq(calls[0].meta.live, true, 'live=true');
    eq(sandbox.__state().legacySync, 0, 'legacy-rebuild не планируется при живом рендерере');

    store.set('chat_activity_display_mode', 'transparent_stream'); // легаси-имя
    eq(calls[1].mode, 'stream_full', 'легаси-имя канонизировано до вызова рендерера');

    // Гидратация НЕ перетирает локальную несохранённую правку (инвариант store),
    // поэтому рендерер повторно не вызывается.
    const before = calls.length;
    store.hydrate(Object.assign({}, SERVER, { chat_activity_display_mode: 'stream_window' }));
    eq(calls.length, before, 'hydrate не тронул pending-правку режима');
    eq(store.get('chat_activity_display_mode'), 'stream_full', 'локальный режим сохранён');
    eq(sandbox.__state().posts.length, 0, 'гидратация не делает POST');

    // Гидратация применяет ключ, по которому нет локальных правок, — и тоже
    // через рендерер (reason=hydrate), без сохранения.
    store.hydrate(Object.assign({}, SERVER, { theme: 'light' }));
    eq(sandbox.__state().applied.filter((a) => a.key === 'theme' && a.reason === 'hydrate').length, 1,
      'hydrate применил theme через apply(reason=hydrate)');
    eq(sandbox.__state().posts.length, 0, 'гидратация не делает POST');

    // Ошибка рендерера не ломает применение состояния.
    sandbox._registerActivityModeRenderer(() => { throw new Error('render boom'); });
    store.set('chat_activity_display_mode', 'stream_full');
    eq(store.get('chat_activity_display_mode'), 'stream_full', 'состояние применилось несмотря на ошибку рендерера');
    await store.flush();
  }

  console.log('');
  if (failures) {
    console.error('FAILED: ' + failures + ' из ' + checks + ' проверок');
    process.exit(1);
  }
  console.log('OK: ' + checks + ' проверок пройдено');
}

main().catch((err) => {
  console.error('HARNESS ERROR: ' + (err && err.stack ? err.stack : err));
  process.exit(1);
});
