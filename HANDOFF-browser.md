# HANDOFF — browser regression harness (WebUI upgrade wave)

Зона: `browser` (CONTRACT.md, строка 9). Только новые файлы, production JS/backend не тронуты.
Ветка worktree: `upgrade-20261022-browser`. База волны: `ebb0f0da6`.

## Что это

Настоящее `static/` приложение (index.html + все `static/*.js` из указанного app root) грузится
в реальном Chromium через Playwright. Сеть подменена локальным mock-сервером: HTML/JS отдаются
как есть с диска, `/api/*` и SSE — синтетические. Логина нет, живых сессий/БД нет, платных
LLM-вызовов нет (все нелокальные запросы **abort'ятся** на уровне route, факт попытки — падающий чек).

## Файлы (все новые)

| файл | роль |
|---|---|
| `scripts/upgrade_browser_harness.py` | драйвер: поднимает mock-сервер, гоняет runner, гасит **свой** PID |
| `scripts/upgrade_browser_server.py` | mock backend: static + `/api/sessions`, `/api/settings`, `/api/chat/stream` (SSE) |
| `scripts/upgrade_browser_run.mjs` | сценарии, Playwright, отчёт JSON |
| `tests/test_upgrade_browser_harness.py` | pytest-обёртка (flock, изолированный HOME, skip без browser) |
| `tests/test_upgrade_browser_baseline.json` | known failures текущего дерева (8 записей) |
| `tests/fixtures/upgrade_chat/*.json` | только данные: тред, длинный stream, settings |

## Как запускать

```bash
cd /root/workspace/webui-upgrade-20261022/browser
python3 scripts/upgrade_browser_harness.py \
  --app-root . \
  --viewports desktop,mobile \
  --baseline tests/test_upgrade_browser_baseline.json \
  --out /tmp/upgrade-browser.json
```

Через pytest (как принято в волне):

```bash
HOME=$(mktemp -d) HERMES_HOME=$(mktemp -d) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /opt/hermes-webui/.venv/bin/python3 -m pytest tests/test_upgrade_browser_harness.py -q
```

Env-переменные: `UPGRADE_VIEWPORTS`, `UPGRADE_APP_ROOT`, `UPGRADE_ONLY`, `UPGRADE_CHROMIUM`,
`UPGRADE_FIXTURE_DIR`, `PLAYWRIGHT_BROWSERS_PATH`.

### Повторный прогон на интеграционном дереве (после merge)

```bash
python3 scripts/upgrade_browser_harness.py --app-root ../integration \
  --baseline tests/test_upgrade_browser_baseline.json
```

Проверено фактически: `--app-root ../integration` → `2/2 scenario runs passed` (desktop,
`modes_settled_cycle` + `long_code_and_table`). Fixtures и baseline берутся из worktree browser,
поэтому один и тот же набор работает и на смерженном коде.

## Runtime

- Playwright: временный `playwright-core@1.64.1` в `/tmp/upgrade-browser-node` (npm `--prefix`, не в проекте).
- Браузер: `/root/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome`.
  Разрешение: `UPGRADE_CHROMIUM` → `PLAYWRIGHT_BROWSERS_PATH` → `~/.cache/ms-playwright` → `/root/.cache/ms-playwright` → системный chromium. Скачивания на лету нет.
- Live `.venv` не используется для установки; из него только `python3 -m pytest`.
- `.venv` в проекте — symlink на PROD, `pip install` туда не делался.

## Изоляция процессов и портов

- Порт: `bind(127.0.0.1, 0)` — номер выбирает ядро, из state-файла читается фактический.
- Остановка: только `SIGTERM` по PID/process group, созданному harness'ом (`start_new_session=True`).
- **Нет** `pkill -f`, **нет** `fuser -k <port>`, **нет** kill по номеру порта. Чужой сервер на 8795
  (pid 3371164 / позже 3454682) этим стендом не затрагивался: kill'ы в ранних итерапах шли только
  по портам, которые стенд сам забиндил (44311/36899/39725/43035/37021/40513).
- Браузер закрывается в `finally` (`browser.close()`), контекст — на каждый сценарий.

## Сценарии (6 × 2 viewport = 12 прогонов, 146 чеков)

| сценарий | что проверяет |
|---|---|
| `modes_live_quiet_to_full` | live-стрим → переключение режима quiet→full: текст и turn сохраняются, поток продолжается без нового SSE, финал на месте |
| `modes_settled_cycle` | цикл full→window→quiet→full на settled треде: ни один ход/текст не теряется, disclosure стабилен |
| `settings_theme_persist` | Settings → Appearance: тема применяется, POST частичный, reload сохраняет, кнопка режима в заголовке шлёт только свой ключ |
| `stream_reading_scroll` | жест вверх во время стрима важнее follow/RO/timer, DOM не пересобирается, возврат вниз возобновляет follow |
| `completion_resize_composer_geometry` | завершение стрима, resize 1440→1024, mobile 393×851, клавиатура (viewport 393×500) — композер и ввод остаются в кадре, без h-скролла |
| `long_code_and_table` | длинный code block и широкая таблица: рендер, внутренний скролл, транскрипт не расширяется |

Viewports: desktop `1440×900`; mobile `393×851` (`isMobile`, `hasTouch`, DPR 3). Жест на mobile —
CDP `Input.dispatchTouchEvent`, на desktop — `mouse.wheel`; отправка на touch — кнопка, на desktop — Enter
(с fallback на кнопку).

## Фактический результат (полный прогон, baseline применён)

```
12/12 scenario runs passed; report: /tmp/upgrade-browser.json
known baseline failures: 14 (documented, not counted as regressions)
[baseline] no new failures
```

Без `--baseline`: `6/12` прогонов, 14 чеков красные — это и есть baseline текущего дерева.

Фактические прогоны, на которых основаны цифры:

```
python3 scripts/upgrade_browser_harness.py --app-root . --viewports desktop,mobile \
  --baseline tests/test_upgrade_browser_baseline.json --out /tmp/rep8.json
  → 12/12 scenario runs passed; known baseline failures: 14; [baseline] no new failures

HOME=$(mktemp -d) HERMES_HOME=$(mktemp -d) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /opt/hermes-webui/.venv/bin/python3 -m pytest tests/test_upgrade_browser_harness.py -q
  → 1 passed in 121.38s

python3 scripts/upgrade_browser_harness.py --app-root ../integration --viewports desktop \
  --only modes_settled_cycle,long_code_and_table
  → 2/2 scenario runs passed (0 of 21 checks failed)
```

## Baseline / known failures (8 записей в `tests/test_upgrade_browser_baseline.json`)

Воспроизводятся на базе `ebb0f0da6` в обоих viewport'ах, если не указано иное.

1. **`modes_live_quiet_to_full` — 4 чека (desktop + mobile).** Переключение режима во время live-стрима
   перерисовывает транскрипт из `S.messages`, где live-ход ещё пуст: текст схлопывается 69–77 → 19 символов
   (остаётся только user-сообщение), `turns=0`, поток визуально обрывается, финальный ответ не появляется.
   Нарушает «Данные не уничтожать при hide» и «стабильный disclosure live→settled». Зона: render.
2. **`settings_theme_persist` — `theme survives a reload` (desktop + mobile).** После reload `body.className`
   пуст, `data-theme` отсутствует даже через 6 с (проверено отдельным пробником, не таймаут). В рамках
   сессии переключение темы работает. Зона: appearance.
3. **`settings_theme_persist` — `header mode button saves only its own key` (desktop + mobile).**
   Кнопка режима в заголовке отправляет весь appearance-блок (37 ключей), а не один
   `chat_activity_display_mode`. Нарушает «header mode сохраняет только свой ключ». Зона: appearance.
4. **`completion_resize_composer_geometry` — overflow при 1024 px.** `aside.rightpanel` и
   `#rightpanelResize` выходят за вьюпорт (`docScrollW=1038` при `vw=1024`; offenders
   `@1038…@1214`). Следствия: desktop — горизонтальный скролл страницы; mobile-прогон того же
   сценария — композер заканчивается на 720 при `vh=730` (10 px зазор от низа). Зона: layout.

Каждая запись содержит `reason` и `viewports`. Когда баг починят, runner напечатает
`FIXED …` и упадёт `assert totals["baseline_fixed"] == 0` в pytest-обёртке — запись надо удалить.

## Ограничения / что не покрыто

- Mock backend не реализует полный `api/routes.py`: только sessions/settings/chat-stream. Новые
  эндпоинты волны (если появятся) нужно добавить в `scripts/upgrade_browser_server.py`.
- Реальный SSE-тайминг сервера не воспроизводится: поток эмитится с фиксированным шагом, поэтому
  гонки «медленный сервер vs быстрый рендер» ловятся только искусственными паузами сценария.
- Mobile-клавиатура моделируется уменьшением viewport до `393×500` (нет `visualViewport` resize
  событий реальной ОС); этого достаточно для проверки геометрии композера, но не для iOS Safari.
- `app_version_token` в отчёте всегда `null` — версия фронта не пробрасывается в static.
- Проверки a11y/контраста и i18n вне этой зоны.

## Для интегратора

- Script order: ничего не добавляется в `index.html` — стенд грузит существующий набор скриптов.
- После merge достаточно `--app-root ../integration` (или путь смерженного дерева).
- Если меняются селекторы (`#composerWrap`, `#messages`, `#btnTitlebarActivityMode`,
  `[data-theme-val]`, `[data-settings-section]`), сценарии нужно править вместе с ними —
  дублирования production-логики в fixtures нет, поэтому расхождения ловятся как падения.
