#!/usr/bin/env python3
"""mars-test — изолированный запуск тестов проекта с жёсткими границами копии.

Тесты выполняются в копии рабочего дерева, а не в самом дереве, чтобы запись
артефактов не портила исходники. Копия ограничена по объёму, числу файлов и
времени, а тяжёлые кэши/сборки/артефакты в неё не попадают.

Границы (флаги, каждый дублируется переменной окружения):

  --max-bytes      объём копии, байт      (512 MiB, MARS_TEST_MAX_BYTES)
  --max-files      число файлов в копии   (20000,    MARS_TEST_MAX_FILES)
  --copy-timeout   секунды на копирование (20,       MARS_TEST_COPY_SECONDS)
  --tmpdir         каталог песочницы: /var/tmp на диске, НЕ /tmp на tmpfs
                   (MARS_TEST_TMPDIR)

Превышение бюджета обнаруживается на проходе-замере (только stat) до создания
копии: гигабайтный рабочий каталог отвергается за миллисекунды, ничего не
копируя и не занимая RAM.

Коды выхода: 0 — тесты прошли или их нет; 1 — тесты провалены; 2 — ошибка
использования; 3 — проект больше бюджета копии (копия не начата); 4 — каталог
не похож на проект либо запрещён; 124 — таймаут тестов; 130 — прерывание.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_TOO_BIG = 3
EXIT_REFUSED = 4
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130

# Маркер настоящего проекта. Каталог без единого маркера — рабочий/скрэтч-каталог
# (например /root/workspace), его тесты гонять нельзя: там нет одного проекта.
PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "setup.py",
    "setup.cfg",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
)

# Кэши, сборки и зависимости: не источник, копировать нечего.
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".pytype",
    ".tox",
    ".nox",
    ".cache",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".turbo",
    ".gradle",
    ".idea",
    ".terraform",
    "dist",
    "build",
    "target",
    "coverage",
    "htmlcov",
}
SKIP_DIR_SUFFIXES = (".egg-info", ".dist-info", ".egg")
SKIP_FILE_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".log",
    ".tmp",
    ".swp",
    ".apk",
    ".aab",
    ".zip",
    ".tar",
    ".gz",
    ".xz",
    ".7z",
    ".whl",
    ".mp4",
    ".webm",
    ".mkv",
    ".iso",
    ".img",
    ".sqlite",
    ".sqlite3",
    ".db",
)

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_FILES = 20000
DEFAULT_COPY_SECONDS = 20.0
DEFAULT_TMP_ROOT = "/var/tmp"
STALE_MARKER = ".mars-test-run"
STALE_MAX_AGE = 12 * 3600

_WEBUI_SMOKE_TESTS = (
    "tests/test_model_picker_fast_open.py",
    "tests/test_static_asset_resolver.py",
    "tests/test_service_worker_api_cache.py",
)
_DISCOVER_MAX_FILES = 200


class BudgetExceeded(Exception):
    """Копия не влезает в лимиты — поднимается до/во время копирования."""

    def __init__(self, reason: str, stats: "TreeStats") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stats = stats


@dataclass
class TreeStats:
    files: int = 0
    bytes: int = 0
    skipped_dirs: int = 0
    skipped_files: int = 0
    symlinks: int = 0
    unreadable: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "skipped_dirs": self.skipped_dirs,
            "skipped_files": self.skipped_files,
            "symlinks": self.symlinks,
            "unreadable": self.unreadable[:10],
        }


# --------------------------------------------------------------------------- #
# Обнаружение проекта
# --------------------------------------------------------------------------- #


def find_project_root(start_dir: Path) -> Path:
    curr = start_dir.resolve()
    for parent in [curr, *curr.parents]:
        if any((parent / marker).exists() for marker in PROJECT_MARKERS):
            return parent
    return curr


def _deny_roots() -> List[Path]:
    raw = os.environ.get("MARS_TEST_DENY_ROOTS")
    if raw is None:
        candidates = [Path.home() / "workspace", "/root/workspace"]
    else:
        candidates = [p for p in raw.split(os.pathsep) if p.strip()]
    out: List[Path] = []
    for item in candidates:
        try:
            out.append(Path(item).expanduser().resolve())
        except OSError:
            continue
    return out


def refusal_reason(root: Path, allow_markerless: bool = False) -> Optional[str]:
    """Почему этот каталог нельзя гонять как проект (None — можно)."""
    try:
        resolved = root.resolve()
    except OSError:
        return f"каталог недоступен: {root}"

    if resolved == Path(resolved.anchor) or resolved == Path.home():
        return f"корень файловой системы или домашний каталог: {resolved}"

    for denied in _deny_roots():
        if resolved == denied:
            return (
                f"каталог запрещён (MARS_TEST_DENY_ROOTS): {resolved} — "
                "это склад рабочих каталогов, а не проект"
            )

    if not allow_markerless and not any((resolved / m).exists() for m in PROJECT_MARKERS):
        return (
            f"в {resolved} нет маркера проекта ({' / '.join(PROJECT_MARKERS[:4])} …) — "
            "похоже на рабочий каталог, тесты не запускаются"
        )
    return None


# --------------------------------------------------------------------------- #
# Бюджет и копирование
# --------------------------------------------------------------------------- #


def _skip_dir_name(name: str) -> bool:
    return name in SKIP_DIRS or name.endswith(SKIP_DIR_SUFFIXES)


def _skip_file_name(name: str) -> bool:
    return Path(name).suffix.lower() in SKIP_FILE_SUFFIXES


def walk_bounded(
    src: Path,
    dst: Optional[Path],
    max_bytes: int,
    max_files: int,
    deadline: float,
    copy: bool,
) -> TreeStats:
    """Один проход по дереву: считает объём и (если copy) переносит файлы.

    dst=None означает режим замера. Лимиты проверяются на ходу и поднимают
    BudgetExceeded сразу — до того, как будет скопирован хоть один байт
    сверх бюджета (а для замера — до создания копии вообще).
    """
    stats = TreeStats()
    pending = [(src, dst)]
    seen = 0

    while pending:
        s_dir, d_dir = pending.pop()
        try:
            entries = sorted(os.scandir(s_dir), key=lambda e: e.name)
        except OSError as err:
            stats.unreadable.append(f"{s_dir}: {err.strerror or err}")
            continue

        if d_dir is not None:
            d_dir.mkdir(parents=True, exist_ok=True)

        for entry in entries:
            seen += 1
            if seen % 256 == 0 and time.monotonic() > deadline:
                raise BudgetExceeded(
                    f"копирование не уложилось в отведённое время "
                    f"({stats.files} файлов, {stats.bytes} байт)",
                    stats,
                )
            name = entry.name
            path = Path(entry.path)

            try:
                is_symlink = entry.is_symlink()
            except OSError:
                stats.unreadable.append(f"{path}: stat")
                continue

            if is_symlink:
                # Симлинк не разворачиваем: он не занимает места и не уводит
                # обход в чужие деревья (node_modules/.venv остаются рабочими).
                if _skip_dir_name(name) or _skip_file_name(name):
                    stats.skipped_files += 1
                    continue
                stats.symlinks += 1
                if copy and d_dir is not None:
                    try:
                        os.symlink(os.readlink(path), d_dir / name)
                    except OSError:
                        pass
                continue

            try:
                if entry.is_dir(follow_symlinks=False):
                    if _skip_dir_name(name):
                        stats.skipped_dirs += 1
                        continue
                    pending.append((path, d_dir / name if d_dir is not None else None))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue  # fifo/socket/device — не для копии
                if _skip_file_name(name):
                    stats.skipped_files += 1
                    continue
                size = entry.stat(follow_symlinks=False).st_size
            except OSError as err:
                stats.unreadable.append(f"{path}: {err.strerror or err}")
                continue

            stats.files += 1
            stats.bytes += size
            if stats.bytes > max_bytes:
                raise BudgetExceeded(
                    f"объём источника превысил лимит {max_bytes} байт", stats
                )
            if stats.files > max_files:
                raise BudgetExceeded(
                    f"число файлов превысило лимит {max_files}", stats
                )

            if copy and d_dir is not None:
                try:
                    shutil.copy2(path, d_dir / name, follow_symlinks=False)
                except OSError as err:
                    stats.unreadable.append(f"{path}: {err.strerror or err}")

    return stats


def tmp_root(explicit: Optional[str]) -> Path:
    candidate = explicit or os.environ.get("MARS_TEST_TMPDIR") or DEFAULT_TMP_ROOT
    path = Path(candidate).expanduser()
    if not path.is_dir():
        path = Path(DEFAULT_TMP_ROOT)
    if not path.is_dir():
        path = Path(tempfile.gettempdir())
    return path


def is_tmpfs(path: Path) -> bool:
    """tmpfs = RAM: копия в нём съедает память хоста (грабля /tmp)."""
    best_mount, best_fs = "", ""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount, fstype = parts[1], parts[2]
                if str(path).startswith(mount) and len(mount) >= len(best_mount):
                    best_mount, best_fs = mount, fstype
    except OSError:
        return False
    return best_fs in ("tmpfs", "ramfs")


def sweep_stale(root: Path) -> List[str]:
    """Убирает наши же песочницы, оставленные убитым процессом.

    Только каталоги с маркером STALE_MARKER, старше STALE_MAX_AGE и наши по uid:
    чужие каталоги в /var/tmp не трогаем.
    """
    removed: List[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    now = time.time()
    for entry in entries:
        if not entry.name.startswith("mars-test-") or not entry.is_dir(follow_symlinks=False):
            continue
        marker = Path(entry.path) / STALE_MARKER
        try:
            st = marker.stat()
        except OSError:
            continue
        if st.st_uid != os.getuid() or now - st.st_mtime < STALE_MAX_AGE:
            continue
        cleanup_tree(Path(entry.path))
        removed.append(entry.path)
    return removed


def cleanup_tree(path: Path) -> None:
    def _onerror(func, target, _exc):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onerror=_onerror)
    except TypeError:  # Python 3.13+: onerror переименован в onexc
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Обнаружение команды тестов
# --------------------------------------------------------------------------- #


def _usable_bin(path: Path) -> bool:
    """Executable whose shebang interpreter actually exists.

    A venv left behind by a `uv` cleanup keeps `.venv/bin/pytest` with
    ``#!/opt/.../.venv/bin/python`` pointing at a deleted interpreter: the file
    is still executable, so a naive check picks it and every run dies with
    "bad interpreter". One readline is cheap enough to avoid that.
    """
    try:
        if not (path.is_file() and os.access(path, os.X_OK)):
            return False
        with open(path, "rb") as fh:
            head = fh.readline(256)
    except OSError:
        return False
    if head.startswith(b"#!"):
        parts = head[2:].strip().split()
        if parts:
            interp = parts[0].decode("utf-8", "replace")
            if interp.startswith("/") and not os.path.exists(interp):
                return False
    return True


def _find_pytest(root: Path) -> Optional[str]:
    """Найти pytest: в проекте, в основном дереве (для worktree) или в PATH."""
    cands: List[Path] = [root / ".venv" / "bin" / "pytest", root / "venv" / "bin" / "pytest"]
    try:
        common = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if common.returncode == 0 and common.stdout.strip():
            gd = Path(common.stdout.strip())
            if not gd.is_absolute():
                gd = (root / gd).resolve()
            main_root = gd.parent
            cands += [
                main_root / ".venv" / "bin" / "pytest",
                main_root / "venv" / "bin" / "pytest",
            ]
    except (OSError, subprocess.SubprocessError):
        pass
    cands.append(Path("/usr/local/lib/hermes-agent/venv/bin/pytest"))
    for cand in cands:
        if _usable_bin(Path(cand)):
            return str(cand)
    found = shutil.which("pytest")
    return found if found and _usable_bin(Path(found)) else None


def detect_test_command(root: Path) -> Optional[List[str]]:
    pkg_json = root / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            test_script = str(scripts.get("test", "")).strip()
            if test_script and not test_script.startswith('echo "Error: no test specified"'):
                return ["npm", "test"]
        except (OSError, ValueError, AttributeError):
            pass

    if any(root.glob("test_*.py")) or any(root.glob("*_test.py")) or (root / "tests").is_dir():
        pytest_bin = _find_pytest(root)
        is_webui = (root / "api").is_dir() and (root / "static").is_dir()
        if pytest_bin:
            if is_webui:
                fast = [t for t in _WEBUI_SMOKE_TESTS if (root / t).is_file()]
                if fast:
                    return [pytest_bin, "-q", "-p", "no:cacheprovider", *fast]
                return None
            return [pytest_bin, "-q"]
        n = len(list((root / "tests").rglob("test_*.py"))) if (root / "tests").is_dir() else 0
        if n > _DISCOVER_MAX_FILES:
            print(
                f"⚠️ mars-test: pytest не найден, а в tests/ {n} файлов — полный "
                "'unittest discover' в песочнице упрётся в OOM. Прогон пропущен.",
                file=sys.stderr,
            )
            return None
        py_bin = sys.executable
        for venv_path in [root / ".venv" / "bin" / "python3", root / "venv" / "bin" / "python3"]:
            if venv_path.exists():
                py_bin = str(venv_path)
                break
        return [py_bin, "-m", "unittest", "discover", "-s", "tests"]

    return None


# --------------------------------------------------------------------------- #
# Запуск в процессе-группе
# --------------------------------------------------------------------------- #

_active: dict = {"proc": None}


def kill_process_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """SIGTERM всей группе процесса, затем SIGKILL по истечении grace.

    Убивается ровно своя группа (start_new_session=True), без pkill/killall по
    шаблону: чужие процессы на хосте не задеваются.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_bounded(cmd: List[str], cwd: Path, timeout: int) -> tuple:
    """(returncode, output, timed_out) — тесты живут в своей группе процессов."""
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    _active["proc"] = proc
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
            return proc.returncode, out or "", False
        except subprocess.TimeoutExpired:
            kill_process_group(proc)
            out, _ = proc.communicate()
            return proc.returncode, out or "", True
    finally:
        _active["proc"] = None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Изолированный раннер тестов Mars (с границами копии)")
    parser.add_argument("cmd", nargs="*", help="Команда для запуска (по умолчанию автоопределение)")
    parser.add_argument("--dir", "-d", default=".", help="Корневая директория проекта")
    parser.add_argument("--timeout", "-t", type=int, default=120, help="Таймаут тестов, с")
    parser.add_argument(
        "--copy-timeout",
        type=float,
        default=_env_float("MARS_TEST_COPY_SECONDS", DEFAULT_COPY_SECONDS),
        help="Бюджет времени на копирование, с",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=_env_int("MARS_TEST_MAX_BYTES", DEFAULT_MAX_BYTES),
        help="Максимальный объём копии, байт",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=_env_int("MARS_TEST_MAX_FILES", DEFAULT_MAX_FILES),
        help="Максимальное число файлов в копии",
    )
    parser.add_argument("--tmpdir", default=None, help="Каталог песочницы (по умолчанию /var/tmp)")
    parser.add_argument("--memory", "-m", default="1G", help="Лимит RAM (cgroup MemoryMax)")
    parser.add_argument("--cpu", default="100%", help="Лимит CPU (cgroup CPUQuota)")
    parser.add_argument("--no-sandbox", action="store_true", help="Без cgroup/systemd-run")
    parser.add_argument(
        "--allow-markerless",
        action="store_true",
        help="Разрешить каталог без маркера проекта (явное намерение)",
    )
    parser.add_argument(
        "--print-plan",
        action="store_true",
        help="Напечатать план (JSON) и выйти, ничего не копируя",
    )
    return parser


def _emit_plan(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args, remaining = parser.parse_known_args(list(argv) if argv is not None else None)

    project_dir = find_project_root(Path(args.dir))
    explicit_cmd = list(args.cmd or []) + list(remaining or [])
    if explicit_cmd and len(explicit_cmd) == 1 and Path(explicit_cmd[0]).is_dir():
        project_dir = find_project_root(Path(explicit_cmd[0]))
        explicit_cmd = []

    plan = {
        "root": str(project_dir),
        "max_bytes": args.max_bytes,
        "max_files": args.max_files,
        "copy_timeout": args.copy_timeout,
        "tmpdir": str(tmp_root(args.tmpdir)),
        "command": None,
        "stats": None,
        "ok": False,
        "code": EXIT_OK,
        "reason": None,
    }

    reason = refusal_reason(project_dir, allow_markerless=args.allow_markerless)
    if reason:
        plan.update(code=EXIT_REFUSED, reason=reason)
        if args.print_plan:
            _emit_plan(plan)
        print(f"⏭️ mars-test: пропуск — {reason}", file=sys.stderr)
        return EXIT_REFUSED

    test_cmd = explicit_cmd or detect_test_command(project_dir)
    plan["command"] = test_cmd
    if not test_cmd:
        plan.update(code=EXIT_OK, reason="тестов не найдено")
        if args.print_plan:
            _emit_plan(plan)
        print(
            f"ℹ️ mars-test: в {project_dir} нет стандартных тестов "
            "(npm test / pytest / unittest) — проверять нечего.",
            file=sys.stderr,
        )
        return EXIT_OK

    # Проход-замер: только stat, без копии. Гигабайтный каталог отвергается здесь.
    try:
        measured = walk_bounded(
            project_dir,
            None,
            args.max_bytes,
            args.max_files,
            time.monotonic() + args.copy_timeout,
            copy=False,
        )
    except BudgetExceeded as err:
        plan.update(code=EXIT_TOO_BIG, reason=err.reason, stats=err.stats.as_dict())
        if args.print_plan:
            _emit_plan(plan)
        print(
            f"⏭️ mars-test: пропуск — {err.reason}; скопировано 0 байт. "
            f"Лимиты: {args.max_bytes} байт / {args.max_files} файлов "
            "(--max-bytes / --max-files / MARS_TEST_MAX_*).",
            file=sys.stderr,
        )
        return EXIT_TOO_BIG

    plan["stats"] = measured.as_dict()
    if args.print_plan:
        plan.update(ok=True, code=EXIT_OK)
        _emit_plan(plan)
        return EXIT_OK

    root = tmp_root(args.tmpdir)
    if is_tmpfs(root):
        print(
            f"⚠️ mars-test: песочница {root} на tmpfs — копия займёт RAM. "
            "Укажите --tmpdir /var/tmp.",
            file=sys.stderr,
        )
    for stale in sweep_stale(root):
        print(f"🧹 mars-test: убрана брошенная песочница {stale}", file=sys.stderr)

    tmpdir = Path(tempfile.mkdtemp(prefix="mars-test-", dir=str(root)))
    os.chmod(tmpdir, 0o700)
    try:
        (tmpdir / STALE_MARKER).write_text(str(os.getpid()), encoding="utf-8")

        try:
            walk_bounded(
                project_dir,
                tmpdir,
                args.max_bytes,
                args.max_files,
                time.monotonic() + args.copy_timeout,
                copy=True,
            )
        except BudgetExceeded as err:
            print(f"⏭️ mars-test: пропуск — {err.reason}; копия удалена.", file=sys.stderr)
            return EXIT_TOO_BIG

        # node_modules/.venv пропущены при копировании: подключаем оригиналы,
        # чтобы зависимости были доступны и ничего не весило в песочнице.
        for shared in ("node_modules", ".venv", "venv"):
            src_s = project_dir / shared
            dst_s = tmpdir / shared
            if src_s.is_dir() and not dst_s.exists():
                try:
                    os.symlink(src_s, dst_s, target_is_directory=True)
                except OSError:
                    pass

        actual_cmd = list(test_cmd)
        sandbox_enabled = not args.no_sandbox and shutil.which("systemd-run") is not None
        if sandbox_enabled:
            actual_cmd = [
                "systemd-run",
                "--scope",
                "-q",
                f"-pMemoryMax={args.memory}",
                "-pMemorySwapMax=0",
                f"-pCPUQuota={args.cpu}",
                "--",
            ] + actual_cmd
            print(
                f"🧪 Запуск тестов в песочнице (cgroup MemoryMax={args.memory}, "
                f"CPUQuota={args.cpu}) {tmpdir}: {' '.join(test_cmd)}"
            )
        else:
            print(f"🧪 Запуск тестов в песочнице {tmpdir}: {' '.join(test_cmd)}")

        code, output, timed_out = run_bounded(actual_cmd, tmpdir, args.timeout)
        output = output.strip()

        if timed_out:
            print(
                f"❌ Таймаут тестов ({args.timeout}с) — группа процессов остановлена, "
                "песочница удалена.",
                file=sys.stderr,
            )
            return EXIT_TIMEOUT

        if code == 5 and "NO TESTS RAN" in output.upper():
            print("ℹ️ mars-test: тестов в каталоге нет (NO TESTS RAN) — проверять нечего.")
            return EXIT_OK
        if code == 0:
            print("✅ Все тесты успешно пройдены!")
            if output:
                lines = output.splitlines()
                print("\n".join(lines[-10:]))
            return EXIT_OK
        if code in (-9, 137) and sandbox_enabled:
            print(
                f"❌ Тесты аварийно завершены (OOM / лимит RAM {args.memory} "
                f"или квота CPU {args.cpu})!"
            )
            if output:
                print(output)
            return 137
        print(f"❌ Тесты завершились с ошибкой (код {code}):")
        print(output)
        return code if code > 0 else EXIT_FAIL
    finally:
        # Сюда попадаем и при SystemExit из обработчика сигналов: копия удаляется
        # всегда, брошенных 2.3-ГБ песочниц после SIGTERM не остаётся.
        cleanup_tree(tmpdir)


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        proc = _active.get("proc")
        if proc is not None and proc.poll() is None:
            kill_process_group(proc, grace=2.0)
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    _install_signal_handlers()
    sys.exit(main())
