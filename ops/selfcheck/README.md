# ops/selfcheck — версионированные selfcheck-плагин и раннер тестов

`mars-selfcheck` — плагин Hermes, который после `write_file` / `patch` /
`skill_manage` перепроверяет записанный файл на диске (py compile, `node --check`,
`json.loads`, `yaml.safe_load`, `bash -n`) и, если у проекта есть тесты, гоняет их
через `mars-test` в изолированной копии дерева.

## Чем был плох прежний хост

`/root/.hermes/plugins/mars-selfcheck/__init__.py` (10349 Б, 27.09.2026) и
`/root/tools/mars-test.py` (10607 Б, 12.10.2026) лежали вне git, `root:root`,
через симлинк `/usr/local/bin/mars-test -> /root/tools/mars-test.py`. Любая правка
невидима в ревью, при переустановке теряется.

Дефект: `_find_project_root` в плагине искал корень по `.git` / `package.json` /
`pyproject.toml` / `setup.py`, а `_has_project_tests` отдельно считал проектом
каталог с `tests/`. В `/root/workspace` маркеров проекта нет, но есть `tests/`,
поэтому корнем становился **сам `/root/workspace` — 2.3 ГБ** разнородных
каталогов (`stv-awg-research` 1.1 ГБ, `md-base` 186 МБ, `kvartal` 99 МБ, …).
`mars-test` копировал его `shutil.copytree` в `tempfile.TemporaryDirectory()`,
то есть в `/tmp`, а `/tmp` на этом хосте — **tmpfs** (7.9 ГБ RAM). Плагин ждал
35 с, убивал процесс, `TemporaryDirectory.__exit__` не выполнялся — копия
оставалась в RAM и на диске мусором.

## Что изменено

### Раннер `mars-test.py`

1. **Отказ до копирования.** Корнем проекта считается только каталог с маркером
   (`.git`, `pyproject.toml`, `package.json`, `setup.py`, `setup.cfg`,
   `Cargo.toml`, `go.mod`, `pom.xml`). Каталог без маркера отвергается с кодом
   **4**, каталог из `MARS_TEST_DENY_ROOTS` (по умолчанию `~/workspace`,
   `/root/workspace`) — тоже. Копия не создаётся вообще.
2. **Бюджет копии.** Проход-замер (только `stat`) идёт **до** копирования и
   поднимает `BudgetExceeded` при превышении `--max-bytes` (512 MiB),
   `--max-files` (20000) или `--copy-timeout` (20 с) — код **3**, скопировано
   0 байт. Копирование идёт вторым проходом с теми же лимитами.
3. **Только исходники.** Пропускаются `.git`, `.venv`, `venv`, `node_modules`,
   `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.cache`,
   `dist`, `build`, `target`, `coverage`, `*.egg-info`, а также артефакты
   (`*.pyc`, `*.log`, `*.apk`, `*.zip`, `*.whl`, `*.mp4`, `*.sqlite`, …).
   `node_modules` / `.venv` / `venv` подключаются симлинками на оригинал, чтобы
   зависимости были доступны без копии.
4. **Песочница на диске.** `TMPDIR` по умолчанию `/var/tmp` (ext4), не `/tmp`
   (tmpfs). Каталог создаётся `mkdtemp` → режим `0700`. Если `--tmpdir` указывает
   на tmpfs, печатается предупреждение.
5. **Таймаут без утечек.** Тесты запускаются в своей группе процессов
   (`start_new_session=True`); по таймауту `SIGTERM` группе, через grace —
   `SIGKILL` группе. Никаких `pkill`/`killall` по шаблону: чужие процессы не
   задеваются.
6. **Cleanup всегда.** `finally` удаляет песочницу, включая путь через
   `SIGTERM`/`SIGINT`/`SIGHUP` (обработчик ставится в `__main__`). Брошенные
   песочницы от прежних убийств убираются на старте (`sweep_stale`), но только
   свои: каталог `mars-test-*` с маркером `.mars-test-run`, владелец = текущий
   uid, возраст > 12 ч.
7. **`--print-plan`** — печатает JSON (корень, команда, лимиты, `stats`,
   `code`, `reason`) и выходит, ничего не копируя. Используется установщиком и
   тестами.
8. **Битый pytest не выбирается.** Кандидат (`root/.venv/bin/pytest`,
   `.venv` основного дерева для worktree, bundled runtime агента, `PATH`)
   проверяется по shebang: если интерпретатор удалён (`uv` вычистил managed
   CPython 3.13 — `.venv/bin/pytest` в `/opt/hermes-webui` именно такой),
   файл пропускается, иначе каждый прогон падал бы с `bad interpreter`.

Коды выхода: `0` тесты прошли / их нет, `1` тесты провалены, `2` ошибка
использования, `3` проект больше бюджета (копия не начата), `4` не проект /
запрещённый каталог, `124` таймаут тестов, `130` прерывание.

### Плагин `plugin/`

- `_find_project_root` требует маркер проекта и возвращает `None`, если его нет
  (прежняя эвристика «есть `tests/` → это проект» удалена — она и приводила к
  `/root/workspace`).
- `_root_refusal` отсекает `/`, `$HOME` и `MARS_SELFCHECK_DENY_ROOTS`
  (по умолчанию `~/workspace` и `/root/workspace`).
- `_autotest_command` передаёт раннеру явные `--timeout`, `--copy-timeout`,
  `--max-bytes`, `--max-files`; значения настраиваются `MARS_SELFCHECK_*`.
- Запуск через `Popen(start_new_session=True)`; внешний таймаут =
  тесты + копия + 15 с; по таймауту — `SIGTERM`/`SIGKILL` своей группе.
- Коды `3`/`4`/`124` превращаются в информационный «⏭️ пропущено», а не в
  «❌ провал»: агент не получает ложную тревогу из-за бюджета.
- Из списка исключений убран `tmp` как часть пути (он глушил проверку для любого
  файла в `/tmp`); скрэтч-деревья и так отсекаются требованием маркера.
- Kill switch: `MARS_SELFCHECK_DISABLE=1` (вся проверка),
  `MARS_SELFTEST_DISABLE=1` (только авто-тесты).

### Переменные окружения

| Переменная | Значение по умолчанию | Смысл |
| --- | --- | --- |
| `MARS_TEST_TMPDIR` | `/var/tmp` | каталог песочницы раннера |
| `MARS_TEST_MAX_BYTES` | `536870912` | бюджет объёма копии |
| `MARS_TEST_MAX_FILES` | `20000` | бюджет числа файлов |
| `MARS_TEST_COPY_SECONDS` | `20` | бюджет времени копирования |
| `MARS_TEST_DENY_ROOTS` | `~/workspace:/root/workspace` | запрещённые корни (через `:`) |
| `MARS_SELFCHECK_MAX_BYTES` / `_MAX_FILES` / `_COPY_TIMEOUT` / `_AUTOTEST_TIMEOUT` | как у раннера / `30` | то же со стороны плагина |
| `MARS_SELFCHECK_DENY_ROOTS` | `~/workspace:/root/workspace` | запрещённые корни для плагина |
| `MARS_TEST_BIN` | `which mars-test` → `/root/tools/mars-test.py` | путь к раннеру |

## Установка (только по решению человека, не автоматически)

```bash
cd /opt/hermes-webui
ops/selfcheck/install.sh --dry-run   # план + те же проверки, ничего не меняет
ops/selfcheck/install.sh             # бэкап .bak-<stamp>, установка, smoke
```

`install.sh` до установки проверяет: `py_compile` раннера и плагина; на временном
фикстурном проекте — что валидный проект получает `code: 0`, каталог без маркера
— код `4`, превышение `--max-bytes` — код `3` **до** копии; импорт плагина и
игнорирование дерева без маркера. После установки — `ls` целей, smoke
`mars-test --print-plan` через симлинк и импорт установленного плагина.

Цели и откат:

- раннер: `MARS_SELFCHECK_TARGET` (`/root/tools/mars-test.py`), бэкап
  `mars-test.py.bak-<stamp>`;
- симлинк: `MARS_SELFCHECK_LINK` (`/usr/local/bin/mars-test`);
- плагин: `MARS_SELFCHECK_PLUGIN_DIR` (`/root/.hermes/plugins/mars-selfcheck`),
  бэкап каталога `mars-selfcheck.bak-<stamp>`, `__pycache__` удаляется.

Откат: `cp -a <target>.bak-<stamp> <target>` и
`rm -rf <plugin-dir> && cp -a <plugin-dir>.bak-<stamp> <plugin-dir>`.

Установщик не меняет владельцев (`install` под root даёт `root:root`, как и было)
и не трогает пользовательские данные.

## Тесты

```bash
/usr/local/lib/hermes-agent/venv/bin/python -m pytest ops/selfcheck/tests -q
```

Покрытие (все фикстуры — во временных каталогах, без прод-проектов, моделей и
тяжёлых прогонов):

- `test_mars_test_bounds.py` — отказ скрэтч-корню (`tests/` без маркера) и
  `MARS_TEST_DENY_ROOTS`; валидный маленький проект проходит и песочница
  удаляется; тесты пишут артефакт в копию, а не в исходник; превышение
  `--max-bytes`/`--max-files` отвергается до копии (0 байт, без каталога);
  кэши/сборки/артефакты не копируются; таймаут убивает группу процессов и не
  оставляет дочерних процессов; `SIGTERM` чистит песочницу и детей; брошенная
  песочница убирается, чужая — нет; `/var/tmp` не tmpfs; в исходниках нет
  `pkill`/`killall`.
- `test_selfcheck_plugin.py` — плагин не поднимает раннер для дерева без маркера
  и для запрещённого корня; в команде есть бюджеты; коды `3`/`124` дают
  «пропущено», а не «провал»; реальный провал тестов всё ещё «❌»; манифест
  `1.2.0` с хуком `transform_tool_result`; `install.sh --dry-run` ничего не
  создаёт и печатает `would:`; установщик делает бэкапы `.bak-$STAMP`.

## Известные ограничения

- Плагин по-прежнему запускает тесты на каждый `write_file`/`patch` в проекте с
  тестами — это дорого; бюджет копии ограничивает цену, но не отменяет её.
  Быстрый выключатель: `MARS_SELFTEST_DISABLE=1`.
- `systemd-run --scope` (cgroup-лимиты) включается по умолчанию, если бинарь
  есть; в средах без systemd-сессии используется `--no-sandbox`.
- Симлинки копируются как симлинки, каталоги-симлинки не разворачиваются: тест,
  который пишет через симлинк, попадёт в оригинал.
- Бюджет по умолчанию 512 MiB рассчитан на исходники. Каталоги с крупным
  нетрекнутым медиа-контентом (`/opt/hermes-webui/static/office` 479 МБ,
  `static/downloads` 100 МБ) в бюджет не влезают и отвергаются кодом `3` — это
  осознанно: такой прогон всё равно не уложится в 20 с копирования. Нужен
  больший лимит — `--max-bytes` / `MARS_TEST_MAX_BYTES`.
