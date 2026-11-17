"""browser-guard systemd units must not hardcode a Python path that can dangle.

Context: `/opt/hermes-webui/.venv/bin/python3` is a symlink into a uv-managed
CPython. `uv` garbage-collected that generation on 2026-11-17, the link went
dangling, and both guard units died with `status=203/EXEC` without ever running
the health probe or the janitor. The units now start `run-guard.sh`, which picks
a working interpreter at run time.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "ops" / "browser-guard"
UNIT_DIR = GUARD / "systemd"
RESOLVER = GUARD / "run-guard.sh"
PROBE = GUARD / "browser_health.py"

SERVICES = sorted(UNIT_DIR.glob("*.service"))
ALL_UNITS = sorted(UNIT_DIR.iterdir())
DEPLOY_PREFIX = "/opt/hermes-webui/"
# Invocation of the dead venv interpreter as a *command* (start of line), which is
# what broke. Prose that explains the incident in backticks is fine.
STALE_COMMAND = r"^\s*/opt/hermes-webui/\.venv/bin/python"


def _exec_start(unit: Path) -> str:
    for line in unit.read_text(encoding="utf-8").splitlines():
        if line.startswith("ExecStart="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{unit.name} has no ExecStart")


def _posix_shell() -> str | None:
    if os.name == "nt":
        return None
    return shutil.which("sh") or shutil.which("bash")


def test_guard_units_present():
    assert SERVICES, "no browser-guard service units found"
    assert RESOLVER.is_file(), "run-guard.sh resolver is missing"


@pytest.mark.parametrize("unit", SERVICES, ids=lambda p: p.name)
def test_exec_start_goes_through_resolver(unit: Path):
    cmd = _exec_start(unit)
    assert ".venv/bin/python" not in cmd, f"{unit.name} hardcodes the uv venv interpreter"
    exe, *args = cmd.split()
    assert exe == DEPLOY_PREFIX + "ops/browser-guard/run-guard.sh", exe
    # Every deployed path in the command must exist in this checkout, otherwise
    # install.sh would install a unit that cannot start.
    for token in [exe, *args]:
        if token.startswith(DEPLOY_PREFIX):
            rel = token[len(DEPLOY_PREFIX):]
            assert (ROOT / rel).is_file(), f"{unit.name} references missing {rel}"


def test_no_stale_venv_interpreter_invocations():
    offenders = []
    for path in sorted(GUARD.rglob("*")):
        if not path.is_file() or path.suffix not in {".sh", ".service", ".md", ".py"}:
            continue
        if path == RESOLVER:
            continue  # the resolver lists the venv path as a candidate on purpose
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("ExecStart=") and ".venv/bin/python" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
            elif re.search(STALE_COMMAND, line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], f"stale venv interpreter invocations: {offenders}"


@pytest.mark.skipif(_posix_shell() is None, reason="POSIX shell helper")
def test_resolver_reports_a_working_interpreter():
    out = subprocess.run(
        [_posix_shell(), str(RESOLVER), "--print", str(PROBE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    interpreter = out.stdout.strip()
    assert interpreter, "resolver printed nothing"
    assert os.path.isfile(interpreter) and os.access(interpreter, os.X_OK)


@pytest.mark.skipif(_posix_shell() is None, reason="POSIX shell helper")
def test_resolver_skips_dangling_override(tmp_path):
    """The exact failure mode: a symlink to a deleted interpreter must be skipped."""
    dangling = tmp_path / "python3"
    dangling.symlink_to(tmp_path / "deleted-interpreter")
    assert not os.access(dangling, os.X_OK)

    env = dict(os.environ, MARS_GUARD_PYTHON=str(dangling))
    out = subprocess.run(
        [_posix_shell(), str(RESOLVER), "--print", str(PROBE)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() != str(dangling)


@pytest.mark.skipif(_posix_shell() is None, reason="POSIX shell helper")
def test_resolver_fails_loudly_for_missing_script():
    out = subprocess.run(
        [_posix_shell(), str(RESOLVER), str(GUARD / "does_not_exist.py")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 66
    assert "no such script" in out.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_systemd_analyze_accepts_units(tmp_path):
    """Unit syntax check on copies in a temp dir; ExecStart is rewritten to an
    existing interpreter so the check covers syntax, not this checkout's deploy state."""
    copies = []
    for unit in ALL_UNITS:
        text = unit.read_text(encoding="utf-8")
        if unit.suffix == ".service":
            lines = []
            for line in text.splitlines():
                if line.startswith("ExecStart="):
                    _, script = line.split("=", 1)[1].split(maxsplit=1)
                    line = f"ExecStart={sys.executable} {script}"
                lines.append(line)
            text = "\n".join(lines) + "\n"
        copy = tmp_path / unit.name
        copy.write_text(text, encoding="utf-8")
        copies.append(copy)

    proc = subprocess.run(
        [shutil.which("systemd-analyze"), "verify", "--man=no", *map(str, copies)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    for unit in ALL_UNITS:
        assert unit.name not in proc.stderr, f"{unit.name}: {proc.stderr}"
