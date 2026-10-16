# Mars Desktop MCP (`mars_desktop`)

Локальные действия на ноутбуке **PCLite** (Windows 11) из чата Mars.

Модель в чате Mars Desktop выполняет команды на машине пользователя не через
самодельные SSH-туннели, а через штатный MCP-сервер `mars_desktop`. Инструменты
имеют имена вида `mcp__mars_desktop__*`.

## Что это

| | |
|---|---|
| Имя сервера | `mars_desktop` |
| Транспорт | stdio (JSON-RPC 2.0, newline-delimited) |
| Запуск | `/root/.hermes/mcp/mars_desktop_launch.sh` на сервере Mars |
| Исполнение | на ноутбуке PCLite, через reverse-SSH алиас `laptop` |
| Регистрация | `/root/.hermes/config.yaml` → `mcp_servers.mars_desktop` |

Штамп регистрации в логе (одна строка на старте discovery):

```
MCP server 'mars_desktop' (stdio): registered 3 tool(s):
  mcp__mars_desktop__get_active_window, mcp__mars_desktop__list_directory, mcp__mars_desktop__system_info
```

## Архитектура запуска

```
Mars WebUI (api/streaming.py, discovery каждый ход)
   └─ spawn: /root/.hermes/mcp/mars_desktop_launch.sh        [сервер, Linux]
         └─ exec ssh -T laptop "<remote command>"           [существующий туннель]
               └─ /root/.ssh/config: Host laptop
                     HostName 127.0.0.1 :2222  User SKY
                     IdentityFile /root/.ssh/laptop_ed25519
                        └─ tunnel ноутбук→сервер (обратный, ставит сама PCLite)
                              └─ Mars.Desktop.McpHost.exe    [ноутбук, Windows]
```

Туннель ноутбук→сервер уже существовал до этой работы и **не изменялся**.
`mars_desktop` — только потребитель готового транспорта.

## Где живут файлы

| Файл | Где | Назначение |
|---|---|---|
| `mars_desktop_launch.sh` | сервер: `/root/.hermes/mcp/` | обёртка stdio↔ssh, выбирает цель |
| `mars_desktop_mcp.py` | ноутбук: `C:\Users\SKY\.mars-desktop-mcp\` | сам MCP-сервер (текущая реализация) |
| `Mars.Desktop.McpHost.exe` | ноутбук: `...\mars-desktop\artifacts\publish\win-x64\` | действующий MCP-хост (C#, single-file). Лаунчер выбирает его, когда файл есть |
| `config.yaml` | сервер: `/root/.hermes/` | регистрация сервера + промпт-хинт |

## Как это менять

### Сменить цель запуска (stub ↔ реальный хост)

Лаунчер выбирает цель так (первое совпадение):

1. `$MARS_DESKTOP_MCP_REMOTE_CMD` — явное переопределение, дословная удалённая команда;
2. `Mars.Desktop.McpHost.exe` — если файл существует на ноутбуке;
3. `mars_desktop_mcp.py` — python-заглушка (всегда есть; используется, только если exe отсутствует или несовместим).

Чтобы принудительно зафиксировать цель, задайте переменную в конфиге сервера:

```yaml
mcp_servers:
  mars_desktop:
    command: /root/.hermes/mcp/mars_desktop_launch.sh
    env:
      MARS_DESKTOP_MCP_REMOTE_CMD: 'C:\path\to\Mars.Desktop.McpHost.exe'
```

Чтобы вернуться к авто-выбору — уберите `env`.

### Добавить инструмент

Инструменты описываются в `mars_desktop_mcp.py` в списке `TOOLS` и в словаре
`HANDLERS` (имя → вызываемая функция). `inputSchema` — обычная JSON Schema.
После правки файла перезапуск WebUI **не нужен**: MCP discovery идёт на каждом
ходу и перечитывает манифест. Перезапустите, только если меняли `config.yaml`.

### Правка config.yaml

Только через Python (`yaml.safe_load` → правка → `yaml.safe_dump`-совместимая
запись), **никогда** через `sed`/`awk`. Перед правкой — бэкап:

```bash
cp /root/.hermes/config.yaml /root/.hermes/config.yaml.bak-mcp-$(date +%Y%m%d-%H%M%S)
python3 -c "import yaml; yaml.safe_load(open('/root/.hermes/config.yaml'))"
```

Валидация обязательна: если YAML не парсится — откат из бэкапа немедленно.

## Промпт-хинт

Модель узнаёт, что она в десктопе, из system-prompt хинта, который живёт в том
же `/root/.hermes/config.yaml`:

```yaml
agent:
  platform_hints:
    webui:
      append: |
        ...
```

Ключ `webui` соответствует `platform='webui'`, который WebUI передаёт агенту
(`/opt/hermes-webui/api/streaming.py`, `_agent_kwargs`). Читается это в
`agent/system_prompt.py` → `platform_hint()` → `_resolve_platform_hint()`,
вставляется в системный промпт. `{append|replace}`: `append` дописывает,
`replace` подменяет дефолт (для `webui` дефолта нет).

Текст хинта: клиент — Mars Desktop на PCLite, локальные действия — через
`mcp__mars_desktop__*`, SSH-туннели/sshd/ключи/автозапуск для доступа к
ноутбуку — запрещены.

Проверить, что хинт реально рендерится:

```bash
cd /usr/local/lib/hermes-agent && /opt/hermes-webui/.venv/bin/python3 - <<'PY'
import sys, types, yaml
sys.path.insert(0, "/usr/local/lib/hermes-agent")
from agent.system_prompt import platform_hint
ov = (yaml.safe_load(open("/root/.hermes/config.yaml")).get("agent") or {}).get("platform_hints") or {}
print(platform_hint(types.SimpleNamespace(platform="webui", _platform_hint_overrides=ov)))
PY
```

## Диагностика

### Сервер виден и инструменты зарегистрированы

```bash
grep "mars_desktop" /root/.hermes/logs/agent.log | tail
grep "MCP: registered .* server(s)" /root/.hermes/logs/agent.log | tail -1
```

### Живое рукопожатие вручную (без WebUI)

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{}}}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"system_info","arguments":{}}}' \
 | /root/.hermes/mcp/mars_desktop_launch.sh
```

Ожидание: три JSON-строки, `serverInfo.name == "mars_desktop"`, в третьей —
живые данные ноутбука. Строка `mars_desktop MCP: target=...` идёт в **stderr**,
stdout остаётся чистым протоколом.

### Прямой доступ к транспорту

```bash
ssh -T -o BatchMode=yes -o LogLevel=ERROR laptop "hostname"
```

### Ноутбук не в сети

Инструменты автоматически исчезают у модели, ход не падает. В логе:

```
MCP server 'mars_desktop' failed initial connection after 3 attempts, parking ...
MCP: registered 0 tool(s) from 0 server(s) (1 failed)
```

Проверить изолированно, не трогая живую конфигурацию, можно так: скопировать
`~/.ssh/config` в `/tmp`, заменить `Port 2222` на несуществующий порт, затем
запустить discovery с `ssh -F /tmp/<копия>`. `mars_desktop` должен дать 0
инструментов и не бросить исключение.

## Известные ограничения

1. **`get_active_window` через SSH возвращает пусто** (`HWND 0`, `(no title)`):
   ssh-сессия на Windows не разделяет оконную станцию интерактивного сеанса.
   При локальном запуске на самой PCLite инструмент работает. Для окон нужен
   агент, живущий в пользовательском сеансе (тогда и появится `McpHost.exe`).
2. **`system_info` под ssh показывает `user: sky`** (sshd-сессия), а не `SKY` из
   интерактивного сеанса — косметика, на работу не влияет.
3. **Задержка запуска**: холодный старт MCP ≈ время ssh-хендшейка (~1–2 с).
   `connect_timeout: 20.0` покрывает с запасом.
4. Экземпляры заглушки живут до перезапуска WebUI; «повисшие» ssh-процессы
   чистятся вместе с сессией WebUI.

## Бэкапы

Перед этой работой создан: `/root/.hermes/config.yaml.bak-mcp-<YYYYMMDD-HHMMSS>`.
Восстановление:

```bash
cp /root/.hermes/config.yaml.bak-mcp-<штамп> /root/.hermes/config.yaml
systemctl restart hermes-webui.service
```

## История

- 2026-10-16 — сервер `mars_desktop` зарегистрирован, python-заглушка на
  ноутбуке, промпт-хинт `platform_hints.webui.append`. 6 MCP-серверов, 50
  инструментов.
