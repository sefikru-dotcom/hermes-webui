/*!
 * appearance_store.js — единый источник истины для appearance-настроек WebUI.
 *
 * Зачем: до этого модуля appearance-настройки собирались из живого DOM
 * (`_appearancePayloadFromUi()` в panels.js). Пока Settings ни разу не
 * открывался, контролы не гидратированы и отдают дефолты разметки — кнопка
 * режима в шапке отправляла весь снапшот и молча перезаписывала
 * theme/skin/font_size/thinking_mode/show_tool_calls значениями по умолчанию.
 * См. /root/workspace/webui-audit-modes.md (P1).
 *
 * Модель:
 *   values : Map<key, value>  — текущее значение (то, что видит пользователь)
 *   saved  : Map<key, value>  — последнее подтверждённое сервером значение
 *   diff() = ключи, где values != saved
 *
 * Инварианты:
 *   1. В сеть уходят ТОЛЬКО ключи, которые пользователь реально изменил.
 *      Значение по умолчанию из разметки не может стать «изменением».
 *   2. Пустой diff никогда не превращается в POST.
 *   3. Один POST в полёте; изменения во время POST склеиваются в один
 *      следующий POST (ни один ключ не теряется и не отправляется дважды).
 *   4. Ошибка сохранения не двигает baseline: diff сохраняется, retry
 *      отправляет те же ключи. Явный откат — revert().
 *   5. Применение значения к UI — синхронное (apply), запись — отложенная.
 *
 * Модуль не знает про DOM: сохранение и применение внедряются через
 * configure({save, apply, ...}). Поэтому он же тестируется в node без браузера.
 *
 * Classic script: window.AppearanceStore. Для тестов — module.exports.
 */
(function(root){
'use strict';

var VERSION=1;

// Единственный словарь режимов активности. Легаси-имена живут здесь и только
// здесь: наружу (в store/UI/сеть) всегда уходит канон.
var ACTIVITY_MODES=['stream_full','stream_window','stream_quiet'];
var LEGACY_ACTIVITY_MODES={
  transparent_stream:'stream_full',
  compact_worklog:'stream_window',
  hide_all_activity:'stream_quiet'
};
var THINKING_MODES=['auto','expanded','collapsed','hidden'];

// key -> нормализация. kind=raw — значение проходит как есть (композерные
// тумблеры с динамическими именами и т.п.).
var SPECS={
  theme:{kind:'string',def:'dark'},
  skin:{kind:'string',def:'default',lower:true},
  font_size:{kind:'string',def:'default'},
  chat_activity_display_mode:{kind:'activity_mode'},
  thinking_mode:{kind:'thinking_mode'},
  structured_code_default_view:{kind:'string',def:'auto'},
  structured_code_auto_tree_lines:{kind:'int',def:10,min:1,max:1000},

  transparent_stream_event_timestamps:{kind:'bool',def:true},
  session_jump_buttons:{kind:'bool',def:false},
  session_endless_scroll:{kind:'bool',def:false},
  auto_scroll_follow:{kind:'bool',def:true},
  render_user_markdown:{kind:'bool',def:false},
  large_text_paste_as_attachment:{kind:'bool',def:true},
  project_quick_create_buttons:{kind:'bool',def:false},
  show_titlebar_profile:{kind:'bool',def:false},
  worklog_details_expanded_default:{kind:'bool',def:false},
  show_tool_calls:{kind:'bool',def:true},
  auto_collapse_previous_turns:{kind:'bool',def:true},

  composer_control_order:{kind:'array'},
  hidden_tabs:{kind:'array'},
  tab_order:{kind:'array'}
};

// Сервер принимает легаси-имя и сам перекладывает его в канон
// (api/config.py:10585-10591), поэтому в diff его быть не должно.
var LEGACY_ALIASES={
  activity_feed_expanded_default:'worklog_details_expanded_default'
};

var KEYS=Object.keys(SPECS);

// Композерные тумблеры имеют динамические имена (boot.js: _COMPOSER_*_DEFS),
// поэтому принимаются по префиксу. Всё остальное store не принимает: посторонний
// ключ не может попасть в diff и, значит, в POST.
var DYNAMIC_KEY_PREFIX='hide_composer_';

function isAppearanceKey(key){
  if(!key||typeof key!=='string') return false;
  return !!SPECS[key]||key.indexOf(DYNAMIC_KEY_PREFIX)===0;
}

function canonicalActivityMode(mode){
  var raw=(mode===undefined||mode===null)?'':String(mode).trim().toLowerCase();
  if(LEGACY_ACTIVITY_MODES[raw]) return LEGACY_ACTIVITY_MODES[raw];
  if(ACTIVITY_MODES.indexOf(raw)>=0) return raw;
  // Дефолт сервера (api/config.py: chat_activity_display_mode) и прежний
  // дефолт _canonicalActivityMode() в panels.js.
  return 'stream_window';
}

function _isPlainObject(v){
  return !!v&&typeof v==='object'&&!Array.isArray(v);
}

function _deepEqual(a,b){
  if(a===b) return true;
  if(Array.isArray(a)||Array.isArray(b)){
    if(!Array.isArray(a)||!Array.isArray(b)||a.length!==b.length) return false;
    for(var i=0;i<a.length;i++){ if(!_deepEqual(a[i],b[i])) return false; }
    return true;
  }
  if(_isPlainObject(a)&&_isPlainObject(b)){
    var ka=Object.keys(a),kb=Object.keys(b);
    if(ka.length!==kb.length) return false;
    for(var j=0;j<ka.length;j++){
      if(!Object.prototype.hasOwnProperty.call(b,ka[j])) return false;
      if(!_deepEqual(a[ka[j]],b[ka[j]])) return false;
    }
    return true;
  }
  return false;
}

function _clone(v){
  if(Array.isArray(v)) return v.slice();
  if(_isPlainObject(v)) return Object.assign({},v);
  return v;
}

function norm(key,value){
  var spec=SPECS[key];
  if(!spec) return value===undefined?undefined:_clone(value);
  switch(spec.kind){
    case 'bool':
      return value===undefined||value===null?spec.def:!!value;
    case 'int':{
      var n=parseInt(value,10);
      if(!isFinite(n)) return spec.def;
      if(typeof spec.min==='number'&&n<spec.min) n=spec.min;
      if(typeof spec.max==='number'&&n>spec.max) n=spec.max;
      return n;
    }
    case 'string':{
      if(value===undefined||value===null) return spec.def;
      var s=String(value).trim();
      if(!s) return spec.def;
      return spec.lower?s.toLowerCase():s;
    }
    case 'array':{
      if(!Array.isArray(value)) return [];
      return value.filter(function(item){ return typeof item==='string'&&item.trim(); });
    }
    case 'activity_mode':
      return canonicalActivityMode(value);
    case 'thinking_mode':{
      var m=String(value||'').trim().toLowerCase();
      return THINKING_MODES.indexOf(m)>=0?m:'auto';
    }
  }
  return value;
}

// ── состояние ────────────────────────────────────────────────────────────────

var state={
  hydrated:false,
  values:new Map(),
  saved:new Map(),
  // Ревизия значения на ключ. Нужна, чтобы ответ сервера на POST не затирал
  // правку, сделанную ПОСЛЕ отправки этого POST (иначе последнее слово
  // пользователя терялось бы при медленной сети).
  rev:new Map(),
  status:'idle',
  lastError:null,
  lastSaved:null,
  lastPayload:null
};

var opts={
  save:null,
  apply:null,
  onStatus:null,
  onSaved:null,
  onError:null,
  debounceMs:350
};

var timer=null;
var inFlight=null;
var queued=false;

function configure(options){
  if(!options) return store;
  Object.keys(options).forEach(function(k){
    if(options[k]!==undefined) opts[k]=options[k];
  });
  return store;
}

function isHydrated(){ return !!state.hydrated; }
function get(key){
  var k=LEGACY_ALIASES[key]||key;
  return state.values.has(k)?_clone(state.values.get(k)):undefined;
}
function savedValue(key){
  var k=LEGACY_ALIASES[key]||key;
  return state.saved.has(k)?_clone(state.saved.get(k)):undefined;
}
function has(key){ return state.values.has(LEGACY_ALIASES[key]||key); }
function snapshot(){
  var out={};
  state.values.forEach(function(v,k){ out[k]=_clone(v); });
  return out;
}
function status(){ return state.status; }
function lastError(){ return state.lastError; }
function lastPayload(){ return state.lastPayload?_clone(state.lastPayload):null; }
function pendingKeys(){
  var out=[];
  state.values.forEach(function(v,k){
    if(!state.saved.has(k)||!_deepEqual(v,state.saved.get(k))) out.push(k);
  });
  return out;
}
function diff(){
  var out={};
  state.values.forEach(function(v,k){
    if(!state.saved.has(k)||!_deepEqual(v,state.saved.get(k))) out[k]=_clone(v);
  });
  return out;
}

function applyKey(key,value,meta){
  if(typeof opts.apply!=='function') return;
  try{ opts.apply(key,value,meta||{}); }
  catch(err){
    if(root&&root.console&&root.console.warn) root.console.warn('[appearance] apply failed',key,err);
  }
}

function setStatus(next){
  if(state.status===next) return;
  state.status=next;
  if(typeof opts.onStatus==='function'){
    try{ opts.onStatus(next); }catch(err){}
  }
}

/**
 * Единственный вход изменения значения. Применяет к UI сразу (если не silent)
 * и планирует сохранение (если не noSave).
 */
function set(key,value,options){
  if(!key) return false;
  var k=LEGACY_ALIASES[key]||key;
  if(!isAppearanceKey(k)) return false;
  var next=norm(k,value);
  if(next===undefined) return false;
  var had=state.values.has(k);
  var prev=had?state.values.get(k):undefined;
  var changed=!had||!_deepEqual(prev,next);
  if(changed){
    state.values.set(k,next);
    state.rev.set(k,(state.rev.get(k)||0)+1);
  }
  if(changed&&!(options&&options.silent)) applyKey(k,next,{reason:'local'});
  if(changed&&!(options&&options.noSave)) queueSave();
  return changed;
}

function setMany(payload,options){
  if(!payload||typeof payload!=='object') return false;
  var any=false;
  Object.keys(payload).forEach(function(k){
    if(set(k,payload[k],options)) any=true;
  });
  return any;
}

/** Значения из UI-контролов: меняют store, но не применяют и не сохраняют. */
function stage(payload){
  setMany(payload,{silent:true,noSave:true});
  return diff();
}

/**
 * Гидратация из GET /api/settings (boot и каждое открытие Settings).
 * Локальные несохранённые значения не перетираются: baseline обновляется,
 * diff остаётся.
 */
function hydrate(settings,options){
  if(!_isPlainObject(settings)) return false;
  var rebase=!(options&&options.rebase===false);
  var force=!!(options&&options.force);
  var keys=Object.keys(settings);
  keys.forEach(function(rawKey){
    var key=LEGACY_ALIASES[rawKey]||rawKey;
    // Только appearance-ключи: остальные настройки живут в своих путях.
    if(!isAppearanceKey(key)) return;
    if(rawKey!==key&&Object.prototype.hasOwnProperty.call(settings,key)) return;
    var next=norm(key,settings[rawKey]);
    if(next===undefined) return;
    var hasValue=state.values.has(key);
    var pending=hasValue&&state.saved.has(key)&&!_deepEqual(state.values.get(key),state.saved.get(key));
    if(!hasValue||force||(rebase&&!pending)){
      var prev=state.values.get(key);
      if(!_deepEqual(prev,next)){
        state.values.set(key,next);
        // Серверное значение авторитетно: инвалидируем ответы старых POST.
        state.rev.set(key,(state.rev.get(key)||0)+1);
        // Гидратация — это не действие пользователя: UI подтягиваем, но
        // сохранение не планируем (иначе получим эхо-запись).
        applyKey(key,next,{reason:'hydrate'});
      }
    }
    state.saved.set(key,next);
  });
  state.hydrated=true;
  return true;
}

/** Значения, реально присутствующие в payload (для полного saveSettings). */
function keysPresent(payload){
  if(!_isPlainObject(payload)) return [];
  var out=[];
  Object.keys(payload).forEach(function(rawKey){
    var key=LEGACY_ALIASES[rawKey]||rawKey;
    if(isAppearanceKey(key)&&out.indexOf(key)===-1) out.push(key);
  });
  return out;
}

/**
 * Подтверждение записи сервером. `keys` — что именно отправляли; baseline
 * двигается только по ним, поэтому поздние правки не считаются сохранёнными.
 */
function adoptSaved(saved,keys,revs){
  var list=(Array.isArray(keys)&&keys.length)?keys:keysPresent(saved||{});
  if(!list.length) return false;
  var any=false;
  list.forEach(function(rawKey){
    var key=LEGACY_ALIASES[rawKey]||rawKey;
    var value;
    if(_isPlainObject(saved)&&Object.prototype.hasOwnProperty.call(saved,rawKey)){
      value=norm(key,saved[rawKey]);
    }else if(_isPlainObject(saved)&&Object.prototype.hasOwnProperty.call(saved,key)){
      value=norm(key,saved[key]);
    }else{
      value=state.values.has(key)?state.values.get(key):undefined;
    }
    if(value===undefined) return;
    state.saved.set(key,value);
    // Правка, сделанная после отправки этого POST, новее ответа: baseline
    // двигаем (сервер это сохранил), а живое значение оставляем локальным,
    // чтобы оно ушло следующим POST, а не было затёрто.
    if(revs&&typeof revs[key]==='number'&&(state.rev.get(key)||0)!==revs[key]){
      any=true;
      return;
    }
    if(!state.values.has(key)||!_deepEqual(state.values.get(key),value)){
      state.values.set(key,value);
      state.rev.set(key,(state.rev.get(key)||0)+1);
      applyKey(key,value,{reason:'server'});
    }
    any=true;
  });
  return any;
}

/** Явный откат к последнему подтверждённому сервером состоянию. */
function revert(){
  var changed=[];
  // ВАЖНО: итератор Map.keys() не массив — slice.call() по нему даёт [].
  var keys=[];
  state.values.forEach(function(_v,key){ keys.push(key); });
  keys.forEach(function(key){
    if(state.saved.has(key)){
      if(!_deepEqual(state.values.get(key),state.saved.get(key))){
        state.values.set(key,state.saved.get(key));
        state.rev.set(key,(state.rev.get(key)||0)+1);
        changed.push(key);
      }
    }else{
      state.values.delete(key);
      state.rev.set(key,(state.rev.get(key)||0)+1);
      changed.push(key);
    }
  });
  changed.forEach(function(key){
    if(state.values.has(key)) applyKey(key,state.values.get(key),{reason:'revert'});
  });
  return changed;
}

/**
 * Явный откат после неудачного сохранения: возвращает состояние и UI к
 * последнему подтверждённому сервером значению и снимает индикатор ошибки.
 * Сама по себе ошибка baseline не двигает (оптимистичное значение остаётся,
 * diff жив для retry) — откат выполняется только по явному решению.
 */
function rollback(){
  var changed=revert();
  state.lastError=null;
  setStatus('idle');
  return changed;
}

// ── сохранение ───────────────────────────────────────────────────────────────

function _clearTimer(){
  if(timer){ clearTimeout(timer); timer=null; }
}

function queueSave(){
  if(typeof opts.save!=='function') return store;
  _clearTimer();
  var wait=opts.debounceMs>0?opts.debounceMs:0;
  timer=setTimeout(function(){
    timer=null;
    runSave();
  },wait);
  return store;
}

function _finish(result){
  inFlight=null;
  if(queued){
    queued=false;
    if(pendingKeys().length) runSave();
    else setStatus(state.status==='error'?'error':'saved');
  }
  return result;
}

function runSave(){
  if(typeof opts.save!=='function') return Promise.resolve(null);
  _clearTimer();
  if(inFlight){
    // Уже пишем: склеиваем всё, что накопится, в один следующий POST.
    queued=true;
    return inFlight;
  }
  var payload=diff();
  if(!Object.keys(payload).length){
    setStatus('idle');
    return Promise.resolve(null);
  }
  var sentKeys=Object.keys(payload);
  // Снимок ревизий: по нему adoptSaved отличит «ответ на этот POST» от
  // «пользователь успел изменить ключ ещё раз».
  var sentRevs={};
  sentKeys.forEach(function(key){ sentRevs[key]=state.rev.get(key)||0; });
  state.lastPayload=payload;
  setStatus('saving');
  inFlight=Promise.resolve()
    .then(function(){ return opts.save(payload); })
    .then(function(saved){
      if(!_isPlainObject(saved)) throw new Error('Invalid settings response');
      adoptSaved(saved,sentKeys,sentRevs);
      state.lastSaved=saved;
      state.lastError=null;
      setStatus('saved');
      if(typeof opts.onSaved==='function'){
        try{ opts.onSaved(saved,payload,sentKeys); }catch(err){}
      }
      return saved;
    },function(err){
      // Baseline не двигаем: diff остаётся, retry отправит те же ключи.
      state.lastError=err||new Error('save failed');
      setStatus('error');
      if(typeof opts.onError==='function'){
        try{ opts.onError(state.lastError,payload,sentKeys); }catch(_){}
      }
      return null;
    })
    .then(_finish);
  return inFlight;
}

function retry(){
  _clearTimer();
  return runSave();
}

/** Ждём, пока очередь (debounce + in-flight + накопленное) опустеет. */
function flush(){
  var deadline=Date.now()+8000;
  return new Promise(function(resolve){
    (function tick(){
      if(!timer&&!inFlight&&!queued) return resolve(store);
      if(Date.now()>deadline) return resolve(store);
      setTimeout(tick,1);
    })();
  });
}

function reset(){
  _clearTimer();
  inFlight=null;
  queued=false;
  state.hydrated=false;
  state.values=new Map();
  state.saved=new Map();
  state.rev=new Map();
  state.status='idle';
  state.lastError=null;
  state.lastSaved=null;
  state.lastPayload=null;
  return store;
}

var store={
  VERSION:VERSION,
  KEYS:KEYS.slice(),
  ACTIVITY_MODES:ACTIVITY_MODES.slice(),
  THINKING_MODES:THINKING_MODES.slice(),
  LEGACY_ACTIVITY_MODES:Object.assign({},LEGACY_ACTIVITY_MODES),
  LEGACY_ALIASES:Object.assign({},LEGACY_ALIASES),
  canonicalActivityMode:canonicalActivityMode,
  normalize:norm,
  isAppearanceKey:isAppearanceKey,

  configure:configure,
  isHydrated:isHydrated,
  hydrate:hydrate,
  get:get,
  has:has,
  savedValue:savedValue,
  snapshot:snapshot,
  keysPresent:keysPresent,

  set:set,
  setMany:setMany,
  stage:stage,
  diff:diff,
  pendingKeys:pendingKeys,
  adoptSaved:adoptSaved,
  revert:revert,
  rollback:rollback,

  queueSave:queueSave,
  saveNow:runSave,
  retry:retry,
  flush:flush,
  reset:reset,

  status:status,
  lastError:lastError,
  lastPayload:lastPayload,
  isSaving:function(){ return !!inFlight; }
};

root.AppearanceStore=store;
if(typeof module!=='undefined'&&module.exports) module.exports=store;
})(typeof window!=='undefined'?window:globalThis);
