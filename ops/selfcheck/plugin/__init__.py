"""mars-selfcheck — self-healing syntax guard for file-writing tools.

``security-guidance`` scans what the model *intends* to write. This plugin checks
what actually *landed on disk*: after ``write_file`` / ``patch`` / ``skill_manage``
the target file is re-parsed and any syntax error is appended to the tool result.
The model sees the error in the same turn and fixes it, instead of discovering a
broken file at deploy time.

The auto-test step (``mars-test``) is bounded: the project root must carry a real
project marker, the copy has a byte/file/time budget, and the runner refuses
scratch trees such as ``/root/workspace`` (2.3 GB of unrelated directories with a
``tests/`` folder — it used to be copied wholesale into tmpfs on every write).

Non-blocking by design: the write already happened, we only report. Kill switch:
``MARS_SELFCHECK_DISABLE=1`` (auto-tests only: ``MARS_SELFTEST_DISABLE=1``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# tool name -> argument holding the written path
_PATH_ARGS: Dict[str, str] = {
    "write_file": "path",
    "patch": "path",
    # skill_manage writes relative to the skill directory; file_path may be absent
    # for SKILL.md edits, in which case there is nothing on-disk to re-check here.
    "skill_manage": "file_path",
}

# JSON/YAML feeds and generated bundles can be huge; parsing them is pure waste.
_MAX_CHECK_BYTES = 512 * 1024
_TIMEOUT_SECONDS = 15

# Auto-test budgets. The runner enforces them itself; they are also passed as
# flags so an older runner still gets sane numbers instead of its own defaults.
_AUTOTEST_TIMEOUT = 30
_AUTOTEST_COPY_TIMEOUT = 20
_AUTOTEST_MAX_BYTES = 512 * 1024 * 1024
_AUTOTEST_MAX_FILES = 20000
_AUTOTEST_OUTER_GRACE = 15

# A project root needs one of these. Without a marker the directory is a scratch
# area, not a project — climbing to it is what caused the 2.3 GB copy.
_PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "setup.py",
    "setup.cfg",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
)

_DENY_ROOT_DEFAULTS = ("/root/workspace",)

_TRUTHY = {"1", "true", "yes", "on"}


def _disabled() -> bool:
    return os.environ.get("MARS_SELFCHECK_DISABLE", "").lower() in _TRUTHY


def _run(cmd: List[str]) -> Tuple[bool, str]:
    """Run a validator. Returns (ok, message) with a short, model-readable message."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        return True, ""  # validator absent — silence beats a misleading warning
    except subprocess.TimeoutExpired:
        return False, f"проверка не завершилась за {_TIMEOUT_SECONDS}s"
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    # node prints the offending line source as the first line; the useful bit is the last.
    lines = [ln for ln in detail.splitlines() if ln.strip()]
    return False, "\n".join(lines[-6:]) if lines else f"exit {proc.returncode}"


def _check_python(path: Path, source: str) -> Tuple[bool, str]:
    try:
        compile(source, str(path), "exec")
    except SyntaxError as err:
        return False, f"SyntaxError: {err.msg} (строка {err.lineno}, позиция {err.offset})"
    except ValueError as err:  # e.g. NUL bytes
        return False, f"{type(err).__name__}: {err}"
    return True, ""


def _check_json(source: str) -> Tuple[bool, str]:
    try:
        json.loads(source)
    except json.JSONDecodeError as err:
        return False, f"JSONDecodeError: {err.msg} (строка {err.lineno}, колонка {err.colno})"
    return True, ""


def _check_yaml(source: str) -> Tuple[bool, str]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return True, ""
    try:
        yaml.safe_load(source)
    except Exception as err:  # yaml raises a zoo of exception classes
        mark = getattr(err, "problem_mark", None)
        where = f" (строка {mark.line + 1}, колонка {mark.column + 1})" if mark else ""
        return False, f"{type(err).__name__}{where}: {err}"
    return True, ""


def _validate(path: Path) -> Tuple[bool, str, str]:
    """(ok, validator_name, message) for one file."""
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return True, "", ""
    if len(source.encode("utf-8", errors="ignore")) > _MAX_CHECK_BYTES:
        return True, "", ""

    suffix = path.suffix.lower()

    if suffix in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        if suffix in (".ts", ".tsx") and not shutil.which("tsc"):
            # node --check cannot parse TypeScript; skip rather than emit noise.
            return True, "", ""
        node = shutil.which("node")
        if not node:
            return True, "", ""
        ok, msg = _run([node, "--check", str(path)])
        return ok, "node --check", msg

    if suffix == ".json":
        ok, msg = _check_json(source)
        return ok, "json", msg

    if suffix in (".yaml", ".yml"):
        ok, msg = _check_yaml(source)
        return ok, "yaml", msg

    if suffix in (".sh", ".bash"):
        bash = shutil.which("bash")
        if not bash:
            return True, "", ""
        ok, msg = _run([bash, "-n", str(path)])
        return ok, "bash -n", msg

    if suffix == ".py":
        ok, msg = _check_python(path, source)
        return ok, "py_compile", msg

    # Extensionless executable (mars-* tools, hooks): trust the shebang.
    if not suffix and source.startswith("#!"):
        first = source.splitlines()[0]
        if "python" in first:
            ok, msg = _check_python(path, source)
            return ok, "py_compile (shebang)", msg
        if "bash" in first or "/sh" in first:
            bash = shutil.which("bash")
            if bash:
                ok, msg = _run([bash, "-n", str(path)])
                return ok, "bash -n (shebang)", msg
    return True, "", ""


def _format_block(path: Path, validator: str, message: str) -> str:
    return (
        "\n\n---\n"
        f"⚠️ Синтаксис сломан — {path} ({validator})\n\n"
        f"```\n{message}\n```\n\n"
        "Файл записан в нерабочем виде. Исправь его сейчас же, до перехода к "
        "следующему шагу, и перепроверь (`mars-lint <файл>`)."
    )


def _selftest_disabled() -> bool:
    return os.environ.get("MARS_SELFTEST_DISABLE", "").lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _deny_roots() -> List[Path]:
    raw = os.environ.get("MARS_SELFCHECK_DENY_ROOTS")
    candidates = [p for p in raw.split(os.pathsep) if p.strip()] if raw is not None else []
    if not candidates:
        candidates = [str(Path.home() / "workspace"), *_DENY_ROOT_DEFAULTS]
    roots: List[Path] = []
    for item in candidates:
        try:
            roots.append(Path(item).expanduser().resolve())
        except OSError:
            continue
    return roots


def _find_project_root(start_dir: Path) -> Optional[Path]:
    """Nearest ancestor that actually looks like a project.

    Returns ``None`` when no marker is found: a directory tree without ``.git`` /
    ``pyproject.toml`` / ``package.json`` is a scratch area (``/root/workspace``
    has a stray ``tests/`` folder but is 2.3 GB of unrelated projects).
    """
    try:
        curr = start_dir.resolve()
    except OSError:
        return None
    for parent in [curr, *curr.parents]:
        if any((parent / marker).exists() for marker in _PROJECT_MARKERS):
            return parent
    return None


def _root_refusal(root: Path) -> Optional[str]:
    try:
        resolved = root.resolve()
    except OSError:
        return f"недоступен: {root}"
    if resolved == Path(resolved.anchor) or resolved == Path.home():
        return "корень файловой системы или домашний каталог"
    for denied in _deny_roots():
        if resolved == denied:
            return f"запрещённый каталог {resolved} (MARS_SELFCHECK_DENY_ROOTS)"
    return None


def _mars_test_bin() -> Optional[str]:
    explicit = os.environ.get("MARS_TEST_BIN", "")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("mars-test")
    if found:
        return found
    fallback = "/root/tools/mars-test.py"
    return fallback if Path(fallback).is_file() else None


def _autotest_command(root: Path) -> Optional[List[str]]:
    """Argv for the bounded runner, or None when there is nothing to run."""
    runner = _mars_test_bin()
    if not runner:
        return None
    return [
        runner,
        "--dir",
        str(root),
        "--timeout",
        str(_env_int("MARS_SELFCHECK_AUTOTEST_TIMEOUT", _AUTOTEST_TIMEOUT)),
        "--copy-timeout",
        str(_env_int("MARS_SELFCHECK_COPY_TIMEOUT", _AUTOTEST_COPY_TIMEOUT)),
        "--max-bytes",
        str(_env_int("MARS_SELFCHECK_MAX_BYTES", _AUTOTEST_MAX_BYTES)),
        "--max-files",
        str(_env_int("MARS_SELFCHECK_MAX_FILES", _AUTOTEST_MAX_FILES)),
    ]


def _kill_group(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """SIGTERM then SIGKILL for this process group only — never pkill/killall."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = grace
    while deadline > 0:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
        deadline -= 0.05
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _run_autotests(path: Path) -> Optional[str]:
    if _selftest_disabled():
        return None

    # Exclude internal / non-project paths. "tmp" is deliberately not in this
    # list: any path under /tmp would match and silence the guard; scratch trees
    # are already rejected by the project-marker requirement below.
    parts = set(path.parts)
    if any(
        p in parts
        for p in (".hermes", ".git", "backups", ".cache", "hermes-webui", "node_modules")
    ):
        return None

    suffix = path.suffix.lower()
    if suffix not in (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".sh"):
        return None

    root = _find_project_root(path.parent)
    if root is None:
        return None
    refusal = _root_refusal(root)
    if refusal:
        logger.info("mars-selfcheck: авто-тесты пропущены (%s)", refusal)
        return None

    cmd = _autotest_command(root)
    if not cmd:
        return None

    timeout = _env_int("MARS_SELFCHECK_AUTOTEST_TIMEOUT", _AUTOTEST_TIMEOUT)
    copy_timeout = _env_int("MARS_SELFCHECK_COPY_TIMEOUT", _AUTOTEST_COPY_TIMEOUT)
    outer = timeout + copy_timeout + _AUTOTEST_OUTER_GRACE

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own group: a timeout kills only our tests
        )
    except (OSError, ValueError) as err:
        logger.warning("mars-selfcheck: не удалось запустить mars-test: %s", err)
        return None

    try:
        stdout, stderr = proc.communicate(timeout=outer)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        timed_out = True

    output = ((stdout or "") + "\n" + (stderr or "")).strip()
    lines = [ln for ln in output.splitlines() if ln.strip()]
    code = proc.returncode

    if timed_out:
        # Not a test failure: report the skip, do not accuse the file.
        return (
            "\n\n---\n"
            f"⏭️ mars-test: авто-тесты пропущены — раннер не уложился в {outer}s "
            f"(тесты {timeout}s + копия {copy_timeout}s). Песочница удалена, "
            f"процессы остановлены. Файл {path.name} записан."
        )

    if code == 0:
        summary = lines[-1] if lines else "тесты пройдены"
        return f"\n\n---\n✅ mars-test: авто-тесты успешно пройдены в песочнице ({summary})."

    if code in (3, 4):
        # Budget refusal / not a project: informational, never a red flag.
        reason = lines[-1] if lines else f"код {code}"
        return (
            "\n\n---\n"
            f"⏭️ mars-test: авто-тесты пропущены ({reason}). "
            "Копирование не выполнялось; границы задаются "
            "MARS_SELFCHECK_MAX_BYTES / MARS_SELFCHECK_MAX_FILES."
        )

    if code == 124:
        return (
            "\n\n---\n"
            f"⏭️ mars-test: авто-тесты прерваны по таймауту {timeout}s "
            "(группа процессов остановлена, песочница удалена)."
        )

    tail = "\n".join(lines[-15:]) if len(lines) > 15 else "\n".join(lines)
    return (
        "\n\n---\n"
        f"❌ Ошибка авто-тестов mars-test (в песочнице) после изменения {path.name}:\n\n"
        f"```\n{tail}\n```\n\n"
        "Файл записан, но авто-тесты проекта провалены. Исправь ошибку перед переходом к следующему шагу!"
    )


def _on_transform_tool_result(
    tool_name: str = "", args: Any = None, result: Any = None, **_: Any
) -> Optional[str]:
    if _disabled() or not isinstance(args, dict) or not isinstance(result, str):
        return None

    path_key = _PATH_ARGS.get(tool_name)
    if path_key is None:
        return None

    raw_path = args.get(path_key)
    if not isinstance(raw_path, str) or not raw_path:
        return None

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if not path.is_file():
        return None  # the write failed or the file was removed — nothing to check

    # A failed tool call is already noisy; don't stack a second complaint on it.
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("error"):
            return None
    except (ValueError, TypeError):
        pass

    try:
        ok, validator, message = _validate(path)
    except Exception as err:  # never break the agent loop over a checker bug
        logger.warning("mars-selfcheck: %s could not be validated: %s", path, err)
        return None
    if not ok:
        return result + _format_block(path, validator, message)

    # Syntax is valid. Next check if automated tests should run (for write_file and patch only).
    if tool_name in ("write_file", "patch"):
        test_block = _run_autotests(path)
        if test_block:
            return result + test_block

    return None


def register(ctx) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
