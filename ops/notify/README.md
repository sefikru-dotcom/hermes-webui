# ops/notify — версионированный путь запуска уведомлений

`mars-notify.py` доставляет сообщение в видимый чат WebUI (зеркалирование в
транскрипт сессии через `gateway.mirror.mirror_to_session`) и опционально шлёт
Web Push на телефон. Его вызывают cron-задачи Hermes строкой вида:

```bash
python3 /root/tools/mars-notify.py "$SID" --label mail-cleanup < /tmp/report.md
```

## Чем был плох прежний хост

`/root/tools/mars-notify.py` — **неверсионированный файл**, `root:root`, вне
любого git-репозитория (`/root/tools` — обычный каталог, `git rev-parse` там
падает). Симлинк `/usr/local/bin/mars-notify -> /root/tools/mars-notify.py`
уже пропадал (23.09.2026), а интерпретаторы внутри файла правились руками по
месту. Поэтому любая правка пути к Python не видна в ревью и теряется при
следующей ручной правке.

## Что сломано в живом файле

`/root/tools/mars-notify.py:45`

```python
WEBUI_VENV_PY = "/opt/hermes-webui/.venv/bin/python3"
```

Этот путь — битый симлинк с 17.11.2026 (`uv` вычистил managed-CPython
3.13, в который он указывал). Он используется как **fallback** в
`_push_python()`: если glob по bundled runtime агента ничего не нашёл, скрипт
возвращает мёртвый путь, `subprocess.run` падает, и Web Push деградирует в
строку `push: ошибка отправки: ...` — то есть уведомление в чат уходит, а на
телефон нет.

Рядом та же болезнь: `PUSH_PYTHONPATH` жёстко содержит
`/opt/hermes-webui/.venv/lib/python3.12/site-packages`, а каталог после чистки
называется `lib/python3.13` (сейчас его нет вовсе; `pywebpush` берётся из
`vendor/push`, поэтому push пока работает).

## Версия в репозитории

`ops/notify/mars-notify.py` — копия живого файла на 17.11.2026 с тремя
изменениями, всё остальное байт-в-байт:

1. `_push_python()` перебирает кандидатов и берёт первый **реально запускаемый**
   (`os.path.isfile` + `os.access(X_OK)` отсекает битые симлинки): bundled
   runtime агента → `venv` агента → venv WebUI → `/usr/bin/python3`. Если не
   подошёл никто — `RuntimeError`, который `_send_push` ловит и печатает как
   `push: исключение: ...`, доставка в чат не страдает.
2. `PUSH_PYTHONPATH` собирается из `vendor/push` плюс существующих
   `.venv/lib/python3.*/site-packages`, без хардкода версии.
3. Пере-экзек в venv агента вынесен в `_reexec_safe_python()` и вызывается
   только из `__main__` — импорт модуля (тесты, установщик) больше не подменяет
   процесс.

## План исправления через GitHub (проверяемый, без правки боевого файла руками)

1. Ревью и мерж ветки `fix/ops-python-path-20261117` в `main`.
2. На сервере из `/opt/hermes-webui` после `git pull`:
   ```bash
   ops/notify/install.sh --dry-run     # печатает план и прогоняет проверки
   ops/notify/install.sh               # бэкап .bak-<stamp>, install, relink, smoke
   ```
3. `install.sh` проверяет до установки: `py_compile`, импорт модуля без
   пере-экзека, `_push_python()` возвращает исполняемый путь, все элементы
   `PUSH_PYTHONPATH` существуют. После установки — smoke с пустым stdin
   (`пусто: нечего доставлять`, exit 2), который **не пишет** в `state.db`.
4. Откат: `cp -a /root/tools/mars-notify.py.bak-<stamp> /root/tools/mars-notify.py`.
5. Проверка живого пути — одна cron-задача с реальным отчётом днём (не ночью:
   push = баннер на телефон владельца). Ночные прогоны идут с `--no-push`.

Пока пункты 1–2 не выполнены, боевой файл остаётся как есть: его текущее
поведение рабочее (glob находит runtime агента), ломается только fallback.
