#!/usr/bin/env python3
"""Доставка сообщения в видимый чат WebUI (зеркалирование в транскрипт сессии).

Зачем: cron-доставка Hermes не поддерживает платформу 'webui'
(при deliver=origin: "unknown platform 'webui'"), а служебный Bot Chat
в сайдбаре WebUI не отображается. Этот скрипт дописывает сообщение прямо
в транскрипт нужной WebUI-сессии (state.db) штатной функцией
gateway.mirror.mirror_to_session — так же, как зеркалятся cron-доставки в DM.

Использование:
  echo "текст" | mars-notify.py [session_id] [--role assistant|user] [--label cron]
  mars-notify.py <session_id> < file.txt

По умолчанию session_id — MARS_NOTIFY_SESSION из окружения либо 'e3de5a58acb5'.
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import datetime

sys.path.insert(0, "/usr/local/lib/hermes-agent")

# Системный /usr/bin/python3 несёт SQLite 3.46.1 с известным WAL-reset багом:
# запись в state.db таким интерпретатором может повредить базу истории чатов.
# Поэтому при запуске системным python3 скрипт пере-экзекьютится в venv агента.
_SAFE_PY = "/usr/local/lib/hermes-agent/venv/bin/python"

DEFAULT_SESSION = os.environ.get("MARS_NOTIFY_SESSION", "e3de5a58acb5")

# Каталоги состояния WebUI по профилям: у каждого изолированного пространства
# свой процесс и свой STATE_DIR, поэтому и свои VAPID-ключи с подписками.
PUSH_STATE_DIRS = {
    "default": "/root/.hermes/webui",
    "natali": "/root/.hermes/profiles/natali/webui",
    "sergey": "/root/.hermes/profiles/sergey/webui",
}
WEBUI_VENV_PY = "/opt/hermes-webui/.venv/bin/python3"
RUNTIME_PY_GLOB = (
    "/usr/local/lib/hermes-agent/.hermes-runtime/python/generation-*/"
    "cpython-3.12*-linux-x86_64-gnu/bin/python3.12"
)
# pywebpush вендорен в vendor/push. site-packages самого venv добавляем только
# если каталог существует: после `uv`-чистки он переезжает на другую версию
# Python (было lib/python3.12, стало lib/python3.13), и жёсткий путь врёт.
PUSH_PYTHONPATH = [
    "/opt/hermes-webui/vendor/push",
    *sorted(
        p
        for p in glob.glob("/opt/hermes-webui/.venv/lib/python3.*/site-packages")
        if os.path.isdir(p)
    ),
]


def _executable(path: str) -> bool:
    """Путь ведёт на реально запускаемый файл.

    `os.access` на битом симлинке возвращает False, поэтому мёртвый
    `.venv/bin/python3` здесь отсекается без отдельной проверки realpath.
    """
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _push_python() -> str:
    """Интерпретатор для Web Push.

    История поломок: venv WebUI (`/opt/hermes-webui/.venv/bin/python3`) сначала
    резолвился в системный Python 3.14 при пакетах в `lib/python3.12`
    (23.09.2026), а 17.11.2026 `uv` вычистил managed-CPython и симлинк вообще
    повис. Поэтому путь не хардкодится, а перебирается: сначала bundled runtime
    агента (cpython 3.12, на нём работает сам WebUI; проверено — `import
    pywebpush, py_vapid, cffi` OK), затем venv агента, затем venv WebUI, затем
    системный python3.
    """
    candidates = sorted(glob.glob(RUNTIME_PY_GLOB), reverse=True) + [
        "/usr/local/lib/hermes-agent/venv/bin/python",
        WEBUI_VENV_PY,
        "/usr/bin/python3",
    ]
    for cand in candidates:
        if _executable(cand):
            return cand
    raise RuntimeError(
        "не найден рабочий python3 для Web Push (проверены runtime агента, "
        "/usr/local/lib/hermes-agent/venv/bin/python, "
        f"{WEBUI_VENV_PY}, /usr/bin/python3)"
    )


def _reexec_safe_python() -> None:
    """Пере-экзек в venv агента, если запущены чужим интерпретатором.

    Держим это в функции и вызываем только из `__main__`: импорт модуля (тесты,
    установщик) не должен подменять процесс.
    """
    # os.access отсекает битый симлинк: если venv агента пересоздадут, путь повиснет.
    if not (os.path.isfile(_SAFE_PY) and os.access(_SAFE_PY, os.X_OK)):
        return
    real = os.path.realpath(sys.executable)
    if real == os.path.realpath(_SAFE_PY) or "hermes-agent/venv" in real:
        return
    try:
        os.execv(_SAFE_PY, [_SAFE_PY, os.path.abspath(__file__), *sys.argv[1:]])
    except OSError:
        pass  # пере-экзек не удался — работаем как есть, доставка важнее версии SQLite


def _send_push(title: str, body: str, profile: str = "default") -> str:
    """Отправить Web Push в указанный профиль. Возвращает краткий итог."""
    state_dir = PUSH_STATE_DIRS.get(profile or "default")
    if not state_dir:
        return f"push: неизвестный профиль {profile!r}"
    code = (
        "import sys; sys.path.insert(0, '/opt/hermes-webui');"
        "from api.push import send_push;"
        "print(send_push(sys.argv[1], sys.argv[2], tag='mars-cron'))"
    )
    try:
        import subprocess

        env = dict(os.environ, HERMES_WEBUI_STATE_DIR=state_dir)
        env["PYTHONPATH"] = os.pathsep.join(
            PUSH_PYTHONPATH + [env["PYTHONPATH"]] if env.get("PYTHONPATH") else PUSH_PYTHONPATH
        )
        # -S отключает site-шум плагинов, но не ломает import api.push.
        out = subprocess.run(
            [_push_python(), "-c", code, title[:120], body[:400]],
            env=env, capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return "push: ошибка отправки: " + (out.stderr.strip().splitlines() or [""])[-1]
        return "push: " + out.stdout.strip()
    except Exception as exc:  # push не должен ломать доставку в чат
        return f"push: исключение: {exc}"


def main() -> int:
    args = [a for a in sys.argv[1:]]
    role = "assistant"
    label = "cron"
    session_id = DEFAULT_SESSION
    push_profile = "default"
    push_enabled = True
    while args:
        a = args.pop(0)
        if a == "--role":
            role = args.pop(0)
        elif a == "--label":
            label = args.pop(0)
        elif a == "--profile":
            push_profile = args.pop(0)
        elif a == "--no-push":
            push_enabled = False
        else:
            session_id = a

    text = sys.stdin.read().strip()
    if not text:
        print("пусто: нечего доставлять", file=sys.stderr)
        return 2

    from gateway.mirror import mirror_to_session

    ok = mirror_to_session(
        "webui", session_id, text, source_label=label, role=role, session_id=session_id,
    )

    # Обновляем метку активности сессии, чтобы чат поднялся вверх списка WebUI.
    try:
        import sqlite3

        con = sqlite3.connect("/root/.hermes/state.db", timeout=10)
        try:
            con.execute(
                "UPDATE sessions SET last_activity_at = ?, "
                "message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?) "
                "WHERE id = ?",
                (datetime.now().timestamp(), session_id, session_id),
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print(f"предупреждение: не обновил активность сессии: {e}", file=sys.stderr)

    # Сбрасываем кэш списка чатов WebUI, иначе отчёт появится только после TTL.
    try:
        import json
        import time
        sys.path.insert(0, "/opt/hermes-webui")
        cache_file = "/root/.hermes/webui/.session_list_cache.json"
        if os.path.exists(cache_file):
            os.remove(cache_file)
    except Exception:
        pass

    print(f"mirrored={ok} session={session_id} role={role} len={len(text)}")

    if push_enabled:
        # Короткая шапка уведомления: первая непустая строка отчёта.
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        title = "Марс" if label == "cron" else f"Марс: {label}"
        print(_send_push(title, first_line or text[:200], push_profile))

    return 0 if ok else 1


if __name__ == "__main__":
    _reexec_safe_python()
    raise SystemExit(main())
