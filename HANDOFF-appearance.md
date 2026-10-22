# HANDOFF — appearance store & autosave (ветка `appearance`)

Ветка: `appearance` (worktree `/root/workspace/webui-upgrade-20261022/appearance`).
Область: единое состояние appearance-настроек + автосейв. Контракт:
`CONTRACT.md`, разбор: `/root/workspace/webui-audit-modes.md` (P1 — header mode).

## Что сделано

1. **`static/appearance_store.js` (новый)** — единственный источник истины:
   `values` (что видит пользователь) + `saved` (что подтвердил сервер),
   `diff()` = ключи, где они расходятся.
   - В сеть уходят **только** реально изменённые ключи; пустой diff → нет POST.
   - Один POST в полёте; правки во время POST склеиваются в следующий POST
     (ни потери, ни дубля).
   - Ответ на POST не затирает правку, сделанную **после** отправки: у каждого
     ключа есть ревизия, `adoptSaved()` двигает baseline, но оставляет более
     новое локальное значение (оно уйдёт следующим POST). Иначе при медленной
     сети последний выбор пользователя терялся.
   - Ошибка не двигает baseline: diff сохраняется, `retry()` отправляет то же.
     Явный откат — `rollback()`: возвращает состояние и UI к серверному
     значению (`apply(reason:'revert')`), чистит diff и снимает индикатор
     ошибки; POST не делает.
   - `hydrate()` не перетирает локальную несохранённую правку.
   - Посторонние ключи (`default_model` и т.п.) в состояние и в POST не попадают.
   - Модуль не знает про DOM: `save`/`apply` внедряются через `configure()` —
     поэтому он тестируется в node без браузера.
   - Единственный словарь режимов (`stream_full|stream_window|stream_quiet` +
     легаси-имена) теперь здесь; `_canonicalActivityMode()` в panels.js его
     переиспользует.

2. **`static/panels.js`** — store подключён как основа UI и сохранения:
   - `_ensureAppearanceStore()` конфигурирует store: `save` → существующий
     `_enqueueSettingsPost` (та же очередь POST, что у Settings), `apply` →
     `_applyAppearanceKeyValue` (существующие `_applyTheme/_applySkin/
     _applyFontSize/_sync*Picker/...`; **ui.js не менялся**).
   - `_pickChatActivityDisplayMode()` (кнопка режима в шапке) → `store.set(...)`:
     мгновенное применение + POST ровно одного ключа. Раньше отправлялся полный
     DOM-снапшот и затирал `theme/skin/font_size/thinking_mode/show_tool_calls`
     дефолтами разметки, если Settings ни разу не открывался (P1).
   - `_registerActivityModeRenderer(fn)` — callback point для автора
     live-rerender (см. ниже), плюс совместимость с `_marsActivityModeChanged`.
   - `_appearancePayloadFromUi()` → `store.stage(DOM) + store.diff()`; до
     гидратации контролов возвращает только явные правки (`store.diff()`),
     а не DOM-снапшот. Старый снапшот сохранён как
     `_appearancePayloadFromUiLegacy()` — фолбэк, если store не подключён.
   - `loadSettingsPanel()` гидратирует store из `GET /api/settings` **до**
     заполнения контролов; контролы затем читают значения через
     `_appearanceStoreValue(key, serverValue)`.
   - Полное `saveSettings()` двигает baseline (`store.hydrate(saved)`), поэтому
     автосейв не переотправляет уже сохранённое.
   - `thinking_mode` больше не читается из DOM при автосейве: в селекте нет
     `auto`, и любой автосейв молча превращал `auto` → `expanded`.

3. **`tests/js/appearance_store_behavior.js` + `tests/test_appearance_store_behavior.py`**
   — поведенческие тесты (node + vm, фейковый POST, без браузера и без живых
   настроек): 60 проверок, 11 сценариев:
   - header mode **до** открытия Settings → POST `{chat_activity_display_mode}`
     и ничего кроме;
   - payload до гидратации контролов = только явные правки;
   - **после** Settings совпадающий DOM не даёт POST, изменение — один ключ;
   - последовательные сохранения двигают baseline;
   - concurrent: правка во время in-flight не теряется и не дублируется;
   - concurrent patch одного ключа: последнее значение побеждает, промежуточное
     не уходит в сеть;
   - concurrent patch разных ключей: один догоняющий POST, без дублей;
   - error rollback: baseline не двигается, `retry()` шлёт тот же diff;
   - явный `rollback()` после ошибки: state + UI к серверному значению, diff
     очищен, индикатор снят, POST не сделан;
   - ошибка + правка во время полёта → один объединённый POST;
   - `hydrate()` не перетирает локальную правку;
   - посторонние ключи отбрасываются, легаси-ключ канонизируется;
   - после полного сохранения автосейв молчит;
   - быстрое применение режима через существующие ui API (без rebuild);
   - регистрация рендерера: канон режима, `reason`, отсутствие legacy-rebuild,
     устойчивость к исключению в рендерере.

## Что нужно от интегратора (wiring)

Пока `appearance_store.js` **не подключён** в `static/index.html`, panels.js
работает по старому DOM-пути (фолбэк), т.е. P1 остаётся. Два шага:

1. `static/index.html` — подключить **до** `panels.js` (классический скрипт,
   без `type="module"`, порядок важен):

   ```html
   <script src="/static/appearance_store.js"></script>
   ```

   Если в проекте есть SRI/манифест ассетов — добавить файл туда же, где
   перечислены остальные `static/*.js`.

2. `static/boot.js` — в стартовом IIFE сразу после загрузки настроек
   (`const s=await api('/api/settings');`) добавить гидратацию, чтобы baseline
   был известен до первого клика:

   ```js
   // boot.js — ровно одна строка, appearance_store.js может отсутствовать
   // (старый клиент/кеш), поэтому проверка обязательна.
   if(window.AppearanceStore&&typeof AppearanceStore.hydrate==='function'){
     AppearanceStore.hydrate(s);
   }
   ```

   Это единственная правка boot.js; без неё store гидратируется при открытии
   Settings и при первом автосейве (поведение корректное, просто baseline
   появляется позже).

Ничего больше подключать не нужно: `panels.js` сам находит
`window.AppearanceStore` и без него деградирует в прежний путь.

### Точный hydration API (для boot/layout)

```js
window.AppearanceStore.hydrate(settings, options?) -> boolean
```

- `settings` — объект из `GET /api/settings` (или его подмножество). Передавать
  как есть: store сам отбирает appearance-ключи, посторонние игнорирует.
- Принимаются и легаси-имена (`activity_feed_expanded_default`,
  `transparent_stream|compact_worklog|hide_all_activity`) — канонизируются.
- `options.rebase` (по умолчанию `true`) — двигать baseline. `options.force`
  (`false`) — перезаписать даже локальную несохранённую правку. Для boot оба
  флага не нужны: вызов `hydrate(s)` — дефолт.
- Возвращает `false`, если аргумент не объект; исключений не бросает.
- Идемпотентно: повторный вызов с тем же снимком ничего не применяет и **не**
  делает POST (гидратация никогда не сохраняет).
- Вызывать **до** первого взаимодействия с UI; порядок относительно `panels.js`
  не важен — `panels.js` при первом обращении сам конфигурирует store.

Проверка «модуль есть» обязательна: `appearance_store.js` — необязательный
ассет, и при старом кеше/частичной выкладке его может не быть.

## Контракт с автором ui.js (режимы активности)

`ui.js` **не менялся** и меняться не должен. Быстрое применение режима идёт
через уже существующие функции: `_syncChatActivityDisplayModeControl()`,
`_syncTitlebarActivityModeButton()`, `_applyThinkingModeClasses()`,
`window._chatActivityDisplayMode` / `window._transparentStream`,
html-классы `mars-*`.

Точка расширения — **регистрация рендерера** (её и должен использовать автор
live-rerender):

```js
// ui.js, при инициализации — один раз:
window._registerActivityModeRenderer(function(mode, meta){
  // mode: 'stream_full' | 'stream_window' | 'stream_quiet'  (всегда канон)
  // meta: { reason: 'local'|'hydrate'|'server'|'revert', live: true }
});
```

- `_registerActivityModeRenderer(fn)` → `true`; не-функция → `false` (без throw).
  Возвращает `false`/`true`, ничего не сохраняет.
- Рендерер вызывается **синхронно** из `_applyActivityModeFast()` на каждое
  применение режима: клик в шапке (`reason:'local'`), гидратация
  (`'hydrate'`), подтверждение сервером (`'server'`), откат (`'revert'`).
- Если рендерер зарегистрирован — panels.js его вызывает и **не** трогает
  транскрипт (никакого `renderMessages`). Если он бросит исключение, оно
  логируется, состояние и остальной UI применяются дальше.
- Если рендерера нет — прежний фолбэк: один склеенный
  `clearMessageRenderCache()+renderMessages({preserveScroll:true})` на серию
  переключений, и только если в ленте есть activity-разметка.
- Совместимость: если рендер-слой уже объявил глобальный
  `window._marsActivityModeChanged(mode)`, он считается зарегистрированным
  рендерером (старый контракт работает без правок).
- `mode` всегда канон: `stream_full | stream_window | stream_quiet`.
- Рендерер не должен сам сохранять настройки и не должен вызывать
  `_pickChatActivityDisplayMode`: запись — забота store.

## Проверки

```bash
node --check static/appearance_store.js
node --check static/panels.js
node tests/js/appearance_store_behavior.js      # OK: 93 проверки, 15 сценариев
python -m pytest tests/test_appearance_store_behavior.py \
                 tests/test_1003_appearance_autosave.py \
                 tests/test_1003_preferences_autosave.py -q
```

## Границы

- production/push/restart не выполнялись;
- `ui.js`, `boot.js`, `index.html`, стили не менялись (wiring — выше);
- живые настройки и `/api/settings` в тестах не используются.
