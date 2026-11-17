# browser-guard — защита WebUI от зависших тестовых браузеров

Инцидент 2026-11-15: headless Chromium, запущенный из чат-сессии агента, остался
жить в cgroup `hermes-webui.service` (`PPID 1`, `--user-data-dir=/tmp/kv-pop-chrome-*`,
`--remote-debugging-port=19225`). Он съел swap (4/4 ГБ), memory PSI full дошёл до
95%, load average до 35 на 4 ядрах, сайт перестал отвечать. Служба была
перезапущена, браузеры убиты.

Причина не в Chromium, а в способе запуска: агент поднимал браузер как фоновую
задачу терминала. Такая задача — потомок `hermes-webui.service`, без таймаута, без
уборки, и её память списывается на бюджет WebUI (`MemoryMax=5500M`). Умерев,
родитель оставляет браузер сиротой, но cgroup остаётся прежней.

## Что здесь есть

| Файл | Назначение |
|---|---|
| `browser-guard.sh` | запуск любого тестового браузера: своя slice, жёсткий дедлайн, гарантированная уборка |
| `browser_janitor.py` | сметает зависшие браузеры и старые временные профили (таймер каждые 5 мин) |
| `browser_health.py` | read-only проба: HTTP, PSI, swap, память службы, число тестовых браузеров |
| `guard_lib.py` | общее определение «что считается тестовым браузером» и чтение /proc |
| `systemd/mars-testbrowsers.slice` | лимиты изолированной слайсы (1800 МБ, без swap) |
| `systemd/mars-browser-{janitor,health}.*` | таймеры и oneshot-юниты |
| `run-guard.sh` | выбирает рабочий python3 и запускает им пробу/janitor (юниты не хардкодят интерпретатор) |
| `install.sh` | установка юнитов с бэкапом существующих и preflight-проверкой интерпретатора |

## Интерпретатор

Юниты **не** содержат путь к Python. Они запускают `run-guard.sh`, а он
перебирает кандидатов и берёт первый, который существует, исполняем и реально
стартует (`-x` + `python -c 'import sys'`):

1. `$MARS_GUARD_PYTHON` — ручной override;
2. `/usr/local/lib/hermes-agent/venv/bin/python` — runtime агента (3.12), на нём
   же работает сам WebUI;
3. `/opt/hermes-webui/.venv/bin/python3` — только если ссылка ещё жива;
4. `/usr/bin/python3`, затем `python3` из `PATH`.

Почему так: `.venv/bin/python3` — симлинк в CPython, которым управляет `uv`
(`~/.local/share/uv/python/cpython-3.13-*`). 17.11.2026 `uv` вычистил эту
генерацию, симлинк повис, и юниты с `ExecStart=/opt/hermes-webui/.venv/bin/python3`
падали с `status=203/EXEC`, не запуская ни пробу, ни janitor. Живые юниты на
сервере были вручную переведены на `/usr/bin/python3` — это работало, но не было
версионировано и расходилось с репозиторием.

Скрипты guard'а используют только стандартную библиотеку, поэтому подходит любой
рабочий `python3`; отдельный venv для них не нужен. Проверить выбор:

```bash
/opt/hermes-webui/ops/browser-guard/run-guard.sh --print \
  /opt/hermes-webui/ops/browser-guard/browser_health.py
```

## Три уровня защиты

1. **Изоляция.** `browser-guard.sh` запускает браузер через
   `systemd-run --scope --slice=mars-testbrowsers.slice`. Процесс физически
   выходит из cgroup WebUI. У слайсы свои `MemoryMax=1800M`, `MemorySwapMax=0`:
   утечка упирается в лимит и получает OOM внутри слайсы, а не в службе.
2. **Таймаут.** Перед стартом armится транзиентный таймер systemd
   (`--on-active=<timeout>`), который делает `systemctl kill -s SIGKILL` по
   всей scope. Это работает, даже если сам `browser-guard.sh` убит SIGKILL или
   умер вместе с родителем — таймер живёт в PID 1.
3. **Уборка.** `trap EXIT/INT/TERM` гасит scope, снимает таймер и удаляет
   временный профиль. `browser_janitor.py` каждые 5 минут добивает всё, что
   проскочило: браузеры старше 45 минут в зоне утечки и каталоги `/tmp/kv-*`,
   `/tmp/guard-*`, `*-probe-profile` старше 6 часов.

## Как запускать браузер

```bash
/opt/hermes-webui/ops/browser-guard/browser-guard.sh \
  --name kv-pop --timeout 600 --bin /snap/bin/chromium -- \
  --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --remote-debugging-port=0 about:blank
```

`--remote-debugging-port=0` (не фиксированный порт) — Chrome сам выберет
свободный и запишет его в `<profile>/DevToolsActivePort`. Профиль создаётся в
`/tmp/guard-<name>-XXXXXX` и удаляется на выходе.

Проверить, что запуск изолирован:

```bash
systemctl status 'mars-tb-*' --no-pager          # scope живёт в mars.slice
cat /proc/<pid>/cgroup                            # .../mars-testbrowsers.slice/...
systemctl show mars-testbrowsers.slice -p MemoryMax
```

## Мониторинг

```bash
systemctl list-timers 'mars-browser-*' --no-pager
journalctl -u mars-browser-health -n 20 --no-pager
/opt/hermes-webui/ops/browser-guard/browser_health.py            # человеку
/opt/hermes-webui/ops/browser-guard/browser_health.py --json     # машинно
tail -5 /var/log/mars-guard/health.jsonl
```

Запуск руками — через тот же резолвер, что и у юнитов (иначе легко снова
наткнуться на мёртвый `.venv/bin/python3`):

```bash
/opt/hermes-webui/ops/browser-guard/run-guard.sh \
  /opt/hermes-webui/ops/browser-guard/browser_health.py --json
```

Порог критики: HTTP не отвечает или > 6 c, memory PSI full avg10 ≥ 40%,
swap ≥ 90%, память службы ≥ 5300 МБ, ≥ 4 тестовых браузера в cgroup службы.

Janitor безопасен по построению:

- смотрит только в `hermes-webui.service` и `mars-testbrowsers.slice` — `mars-browser.service` (постоянный CDP-браузер `:9222`) вне зоны;
- **обязательное условие — временный профиль** (`--user-data-dir=/tmp/...` или
  `/var/tmp/...`). Собственный браузер Марса живёт в
  `$HERMES_HOME/browser-profile/<browser>` (`/root/.hermes/browser-profile/...`) и
  под это условие не попадает, поэтому легитимная сессия `browser_exec` не будет
  убита, даже если она headless и находится в той же cgroup. Именно так выглядел
  инцидент: `--user-data-dir=/tmp/kv-pop-chrome-*`;
- уважает `/run/mars-guard/protected.jsonl` (запуски guard'а, которые ещё в своём дедлайне);
- удаляет только каталоги `/tmp` с известными префиксами, старше 6 часов и не занятые живым процессом.

Проверить классификацию, ничего не убивая:

```bash
/opt/hermes-webui/ops/browser-guard/run-guard.sh \
  /opt/hermes-webui/ops/browser-guard/browser_janitor.py --dry-run
```

## Что НЕ покрыто

- **Встроенный браузер Hermes** (`browser_exec`, `browser_use_cli`,
  `browser_supervisor.py`) — у него свой lifecycle и своя политика таймаутов;
  он не переписывался. Он же работает через `mars-browser.service` (`:9222`),
  который janitor не трогает.
- **Ручной запуск в обход guard.** Если агент или человек снова напишет
  `chrome --headless ... &` в фоне, изоляции не будет. Такой процесс поймает
  только janitor — с задержкой до 45 минут (плюс до 5 минут на тик таймера).
  Инструкция для агентов обновлена в скилле `devops/mars-integrations`.
- **`scripts/*.mjs`** — dev-пробы запускают Chrome сами; они переведены на
  `scripts/lib/browser_proc.mjs` (групповой kill + watchdog + уборка профиля),
  но не изолированы в слайсу, потому что это foreground-скрипты, которые
  завершаются вместе с прогоном.
- **Глобальный Hermes runtime** (`/usr/local/lib/hermes-agent`) не изменялся.

## Установка

```bash
sudo /opt/hermes-webui/ops/browser-guard/install.sh --dry-run
sudo /opt/hermes-webui/ops/browser-guard/install.sh
```

`install.sh` сначала резолвит интерпретатор (`run-guard.sh --print`) и падает с
понятным сообщением, если рабочего python3 нет, — чтобы не поставить юниты,
которые не стартуют. Он не трогает постоянный браузер `mars-browser.service` и не
включает ничего, кроме двух таймеров guard'а.
