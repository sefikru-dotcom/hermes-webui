/**
 * approval_i18n.js — русские описания подтверждений (approval) для «Марса».
 *
 * Ядро Hermes формирует текст «description» опасной команды на английском
 * (tools/approval_detection.py: таблицы HARDLINE/DANGEROUS — 116 формулировок)
 * и отдаёт его в WebUI без перевода. Этот модуль подменяет описание при рендере
 * карточки подтверждения, когда активная локаль интерфейса — русская.
 * Ядро и его файлы не патчатся: перевод живёт только в UI-слое.
 */
const MARS_APPROVAL_DESC_RU = {
  "PowerShell destructive delete (Remove-Item)": "деструктивное удаление в PowerShell (Remove-Item)",
  "PowerShell encoded command execution": "выполнение закодированной команды PowerShell",
  "SQL DELETE without WHERE": "SQL DELETE без условия WHERE",
  "SQL DROP": "SQL DROP (удаление объектов БД)",
  "SQL TRUNCATE": "SQL TRUNCATE (очистка таблиц)",
  "Windows PowerShell destructive delete": "деструктивное удаление в Windows PowerShell",
  "Windows cmd destructive delete": "деструктивное удаление в Windows cmd",
  "Windows destructive delete (recursive/quiet switch)": "деструктивное удаление в Windows (рекурсивный/тихий ключ)",
  "access to Hermes secrets (Windows path)": "доступ к секретам Hermes (путь Windows)",
  "access to SSH keys (Windows path)": "доступ к SSH-ключам (путь Windows)",
  "chmod +x followed by immediate execution": "chmod +x с немедленным запуском файла",
  "cloud metadata endpoint access (instance credentials)": "обращение к метаданным облака (учётные данные инстанса)",
  "command parser limit exceeded": "превышен лимит разбора команды",
  "command parser limit or malformed executable payload": "превышен лимит разбора команды или некорректная полезная нагрузка",
  "copy/move file into sensitive credential/SSH/shell-rc path": "копирование/перемещение файла в чувствительный путь (учётные данные, SSH, rc-файлы оболочки)",
  "copy/move file into system config path": "копирование/перемещение файла в системный каталог конфигурации",
  "dd to raw block device": "запись dd на блочное устройство",
  "delete backups (wbadmin)": "удаление резервных копий (wbadmin)",
  "delete in root path": "удаление в корневом пути",
  "delete volume shadow copies (vssadmin)": "удаление теневых копий томов (vssadmin)",
  "disk copy": "копирование диска",
  "disk partitioning (diskpart)": "разметка диска (diskpart)",
  "docker compose restart/stop/kill/down (container lifecycle)": "docker compose restart/stop/kill/down (жизненный цикл контейнеров)",
  "docker context use (switches default daemon for future commands)": "docker context use (переключение демона по умолчанию для будущих команд)",
  "docker restart/stop/kill (container lifecycle)": "docker restart/stop/kill (жизненный цикл контейнеров)",
  "docker with daemon redirect (--context: alternate daemon)": "docker с перенаправлением на другой демон (--context)",
  "docker with remote daemon redirect (-H/--host)": "docker с перенаправлением на удалённый демон (-H/--host)",
  "docker/podman daemon redirect via environment (DOCKER_HOST/CONTAINER_HOST)": "перенаправление демона docker/podman через окружение (DOCKER_HOST/CONTAINER_HOST)",
  "dynamic shell word may expand to arbitrary program execution flag": "динамическое слово shell может развернуться в флаг запуска произвольной программы",
  "execute remote content via Invoke-Expression": "выполнение удалённого кода через Invoke-Expression",
  "execute remote content via command substitution": "выполнение удалённого кода через подстановку команды",
  "execute remote script via process substitution": "выполнение удалённого скрипта через подстановку процесса",
  "find -delete": "find -delete (удаление найденных файлов)",
  "find -exec/-execdir rm": "find -exec/-execdir rm (удаление через find)",
  "find dynamic shell word may expand to destructive flag": "динамическое слово в find может развернуться в деструктивный флаг",
  "force kill processes": "принудительное завершение процессов",
  "force kill processes (Stop-Process -Force)": "принудительное завершение процессов (Stop-Process -Force)",
  "force kill processes (killall -KILL)": "принудительное завершение процессов (killall -KILL)",
  "force kill processes (killall -s KILL)": "принудительное завершение процессов (killall -s KILL)",
  "force kill processes (taskkill /F)": "принудительное завершение процессов (taskkill /F)",
  "force stop service (Stop-Service -Force)": "принудительная остановка службы (Stop-Service -Force)",
  "fork bomb": "fork-бомба",
  "format drive (format.com)": "форматирование диска (format.com)",
  "format filesystem": "форматирование файловой системы",
  "format filesystem (Format-Volume)": "форматирование файловой системы (Format-Volume)",
  "format filesystem (mkfs)": "форматирование файловой системы (mkfs)",
  "git branch force delete": "принудительное удаление ветки git",
  "git branch force delete (long flags)": "принудительное удаление ветки git (полные ключи)",
  "git branch force delete (long flags, force-first)": "принудительное удаление ветки git (полные ключи, force впереди)",
  "git clean with force (deletes untracked files)": "git clean с force (удаляет неотслеживаемые файлы)",
  "git force push (rewrites remote history)": "git force push (перезапись истории удалённого репозитория)",
  "git force push short flag (rewrites remote history)": "git force push коротким ключом (перезапись истории удалённого репозитория)",
  "git reset --hard (destroys uncommitted changes)": "git reset --hard (уничтожает незакоммиченные изменения)",
  "grant Everyone access (icacls)": "выдача доступа Everyone (icacls)",
  "hermes update (restarts gateway, kills running agents)": "hermes update (перезапуск шлюза, убивает работающих агентов)",
  "in-place edit of Hermes config/env": "правка конфигурации/окружения Hermes на месте",
  "in-place edit of Hermes config/env (long flag)": "правка конфигурации/окружения Hermes на месте (полный ключ)",
  "in-place edit of Hermes config/env (perl/ruby)": "правка конфигурации/окружения Hermes на месте (perl/ruby)",
  "in-place edit of sensitive credential/SSH/shell-rc path": "правка чувствительного пути на месте (учётные данные, SSH, rc-файлы оболочки)",
  "in-place edit of sensitive credential/SSH/shell-rc path (long flag)": "правка чувствительного пути на месте (полный ключ)",
  "in-place edit of sensitive credential/SSH/shell-rc path (perl/ruby)": "правка чувствительного пути на месте (perl/ruby)",
  "in-place edit of system config": "правка системной конфигурации на месте",
  "in-place edit of system config (long flag)": "правка системной конфигурации на месте (полный ключ)",
  "init 0/6 (shutdown/reboot)": "init 0/6 (выключение/перезагрузка)",
  "kill all processes": "завершение всех процессов",
  "kill hermes/gateway process (self-termination)": "завершение процесса hermes/шлюза (самоубийство агента)",
  "kill process via backtick pgrep/pidof expansion (self-termination)": "завершение процесса через подстановку pgrep/pidof (самоубийство агента)",
  "kill process via pgrep/pidof expansion (self-termination)": "завершение процесса через подстановку pgrep/pidof (самоубийство агента)",
  "kill processes by regex (killall -r)": "завершение процессов по регулярному выражению (killall -r)",
  "modify boot configuration (bcdedit /set)": "изменение конфигурации загрузки (bcdedit /set)",
  "overwrite project env/config file": "перезапись файла .env/config проекта",
  "overwrite project env/config via redirection": "перезапись .env/config проекта через перенаправление",
  "overwrite project env/config via tee": "перезапись .env/config проекта через tee",
  "overwrite system config": "перезапись системной конфигурации",
  "overwrite system file via redirection": "перезапись системного файла через перенаправление",
  "overwrite system file via tee": "перезапись системного файла через tee",
  "package manager uninstall": "удаление пакета через менеджер пакетов",
  "pipe decoded content to shell (possible command obfuscation)": "передача декодированного содержимого в shell (возможная обфускация)",
  "pipe openssl-decoded content to shell (possible command obfuscation)": "передача содержимого, декодированного openssl, в shell (возможная обфускация)",
  "pipe remote content to PowerShell (iwr | iex)": "передача удалённого содержимого в PowerShell (iwr | iex)",
  "pipe remote content to shell": "передача удалённого содержимого в shell",
  "pipe tr-transformed output to shell (possible command obfuscation)": "передача вывода tr в shell (возможная обфускация)",
  "pipe xxd-decoded content to shell (possible command obfuscation)": "передача содержимого, декодированного xxd, в shell (возможная обфускация)",
  "podman remote mode (-r/--remote: remote daemon)": "podman в удалённом режиме (-r/--remote)",
  "podman with remote daemon redirect (--url/--connection/--identity)": "podman с перенаправлением на удалённый демон (--url/--connection/--identity)",
  "recursive chown to root": "рекурсивный chown на root",
  "recursive chown to root (long flag)": "рекурсивный chown на root (полный ключ)",
  "recursive delete": "рекурсивное удаление",
  "recursive delete (flags after operands)": "рекурсивное удаление (ключи после операндов)",
  "recursive delete (long flag)": "рекурсивное удаление (полный ключ)",
  "recursive delete of home directory": "рекурсивное удаление домашнего каталога",
  "recursive delete of root filesystem": "рекурсивное удаление корня файловой системы",
  "recursive delete of system directory": "рекурсивное удаление системного каталога",
  "recursive world/other-writable (long flag)": "рекурсивная выдача прав на запись всем (полный ключ)",
  "redirect to raw block device": "перенаправление вывода на блочное устройство",
  "registry delete (reg delete)": "удаление раздела реестра (reg delete)",
  "registry value delete (Remove-ItemProperty -Force)": "удаление параметра реестра (Remove-ItemProperty -Force)",
  "reset ACLs recursively (icacls /reset)": "рекурсивный сброс ACL (icacls /reset)",
  "shell execution via heredoc": "выполнение shell-кода через heredoc",
  "script execution via heredoc": "выполнение скрипта через heredoc",
  "script execution via -e/-c flag": "выполнение скрипта ключом -e/-c",
  "start gateway outside systemd (use 'systemctl --user restart hermes-gateway')": "запуск шлюза вне systemd (используй 'systemctl --user restart hermes-gateway')",
  "stop/delete service (sc)": "остановка/удаление службы (sc)",
  "stop/restart hermes gateway (kills running agents)": "остановка/перезапуск шлюза hermes (убивает работающих агентов)",
  "stop/restart hermes gateway via shell-spliced verb (kills running agents)": "остановка/перезапуск шлюза hermes склеенной командой (убивает работающих агентов)",
  "stop/restart hermes launchd service (kills running agents)": "остановка/перезапуск службы launchd hermes (убивает работающих агентов)",
  "stop/restart system service": "остановка/перезапуск системной службы",
  "sudo password guessing via stdin (sudo -S)": "подбор пароля sudo через stdin (sudo -S)",
  "sudo with combined-flag privilege escalation": "sudo с повышением привилегий составным ключом",
  "sudo with privilege flag (stdin/askpass/shell/list)": "sudo с привилегированным ключом (stdin/askpass/shell/list)",
  "system shutdown/reboot": "выключение или перезагрузка системы",
  "systemctl poweroff/reboot": "systemctl poweroff/reboot (выключение/перезагрузка)",
  "telinit 0/6 (shutdown/reboot)": "telinit 0/6 (выключение/перезагрузка)",
  "wipe disk (Clear-Disk)": "стирание диска (Clear-Disk)",
  "wipe free space (cipher /w)": "затирание свободного места (cipher /w)",
  "world/other-writable permissions": "права на запись для всех (world/other-writable)",
  "write to block device": "запись на блочное устройство",
  "xargs with rm": "xargs с rm (массовое удаление)"
};

/** Активная локаль WebUI: localStorage('hermes-lang') → <html lang>. */
function marsActiveLocale() {
  try {
    const stored = localStorage.getItem('hermes-lang');
    if (stored) return String(stored).toLowerCase();
  } catch (_) {}
  const html = (typeof document !== 'undefined' && document.documentElement && document.documentElement.lang) || '';
  return String(html).toLowerCase();
}

/**
 * Переводит описание опасной команды на русский, если интерфейс русский.
 * Неизвестные формулировки возвращаются как есть — английский, но без потери смысла.
 * @param {string} text
 * @returns {string}
 */
function marsLocalizeApprovalDesc(text) {
  const raw = (text === null || text === undefined) ? '' : String(text);
  if (!raw) return raw;
  if (!marsActiveLocale().startsWith('ru')) return raw;
  const key = raw.trim();
  if (MARS_APPROVAL_DESC_RU[key]) return MARS_APPROVAL_DESC_RU[key];
  // Уточняющий суффикс в скобках: "recursive delete (long flag)".
  const m = key.match(/^(.+?)\s*\(([^()]+)\)$/);
  if (m) {
    const base = MARS_APPROVAL_DESC_RU[m[1].trim()];
    if (base) return base + ' (' + m[2] + ')';
  }
  return raw;
}

if (typeof window !== 'undefined') {
  window.marsLocalizeApprovalDesc = marsLocalizeApprovalDesc;
  window.MARS_APPROVAL_DESC_RU = MARS_APPROVAL_DESC_RU;
}
