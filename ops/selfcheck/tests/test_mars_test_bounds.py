"""Regression tests for the bounded mars-test runner (ops/selfcheck/mars-test.py).

The bug being pinned: writing any file under a scratch tree such as
/root/workspace made the runner copy 2.3 GB into tmpfs, get killed at 35 s and
leave the half-copy behind. These tests use tiny fixtures only — no real project
test suites, no model calls, no heavy IO.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

OPS_DIR = Path(__file__).resolve().parent.parent
RUNNER = OPS_DIR / "mars-test.py"
PLUGIN_DIR = OPS_DIR / "plugin"


def _plan(result: subprocess.CompletedProcess) -> dict:
    payload = result.stdout.strip().splitlines()
    assert payload, f"no plan on stdout; stderr={result.stderr!r}"
    return json.loads(payload[-1])


def _sandbox_entries(sandbox: Path) -> list:
    return sorted(p.name for p in sandbox.iterdir())


def test_runner_exists_and_is_versioned() -> None:
    assert RUNNER.is_file(), f"missing versioned runner at {RUNNER}"
    text = RUNNER.read_text(encoding="utf-8")
    assert '"pkill"' not in text and '"killall"' not in text, "no broad kills allowed"
    assert "killpg" in text, "timeouts must kill the child's process group"


def test_workspace_style_scratch_root_is_refused(run_runner, sandbox_root, tmp_path) -> None:
    """A tree with tests/ but no project marker is a scratch area, not a project."""
    scratch = tmp_path / "workspace"
    (scratch / "tests").mkdir(parents=True)
    (scratch / "tests" / "test_any.py").write_text("def test_x():\n    assert True\n")
    (scratch / "sub-project-a").mkdir()
    (scratch / "sub-project-b").mkdir()

    result = run_runner("--print-plan", "--dir", scratch)
    assert result.returncode == 4, result.stderr
    plan = _plan(result)
    assert plan["code"] == 4
    assert "маркер" in plan["reason"]
    assert _sandbox_entries(sandbox_root) == [], "nothing may be copied for a refused root"


def test_denied_root_is_refused(run_runner, make_project, sandbox_root) -> None:
    project = make_project("denied")
    result = run_runner(
        "--print-plan",
        "--dir",
        project,
        env_extra={"MARS_TEST_DENY_ROOTS": str(project)},
    )
    assert result.returncode == 4, result.stderr
    assert "MARS_TEST_DENY_ROOTS" in result.stderr
    assert _sandbox_entries(sandbox_root) == []


def test_valid_small_project_runs_in_isolated_copy(run_runner, make_project, sandbox_root) -> None:
    """The happy path: tests execute in the copy and the copy is cleaned up."""
    body = (
        "from pathlib import Path\n"
        "def test_writes_artifact():\n"
        "    Path(__file__).with_name('artifact.txt').write_text('x')\n"
        "    assert True\n"
    )
    project = make_project("small", test_body=body)

    result = run_runner("--dir", project, "--no-sandbox", "--timeout", "60")
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "✅" in result.stdout
    assert _sandbox_entries(sandbox_root) == [], "sandbox must be removed after the run"
    assert not (project / "tests" / "artifact.txt").exists(), "tests must not write into the source tree"


def test_oversized_project_is_rejected_before_any_copy(run_runner, make_project, sandbox_root) -> None:
    """Budget is enforced on a stat-only pass: zero bytes copied, no sandbox dir."""
    project = make_project("huge", extra_files={"blob.bin": b"x" * 200_000})

    started = time.monotonic()
    plan_result = run_runner("--print-plan", "--dir", project, "--max-bytes", "50000")
    elapsed = time.monotonic() - started
    assert plan_result.returncode == 3, plan_result.stderr
    assert elapsed < 30, "the rejection must not depend on copying"
    plan = _plan(plan_result)
    assert plan["code"] == 3
    assert plan["stats"]["bytes"] > 50_000

    real_result = run_runner("--dir", project, "--no-sandbox", "--max-bytes", "50000")
    assert real_result.returncode == 3
    assert "скопировано 0 байт" in real_result.stderr
    assert _sandbox_entries(sandbox_root) == [], "oversized projects must not create a sandbox"


def test_file_count_budget_is_enforced(run_runner, make_project, sandbox_root) -> None:
    extra = {f"src/f{i}.txt": b"a" for i in range(40)}
    project = make_project("many", extra_files=extra)
    result = run_runner("--print-plan", "--dir", project, "--max-files", "10")
    assert result.returncode == 3, result.stderr
    assert "файлов" in _plan(result)["reason"]


def test_caches_builds_and_artifacts_are_skipped(run_runner, make_project, sandbox_root) -> None:
    extra = {
        "node_modules/pkg/index.js": b"x" * 100_000,
        "build/out.o": b"x" * 100_000,
        "dist/app.apk": b"x" * 100_000,
        "__pycache__/app.cpython-313.pyc": b"x" * 50_000,
        ".pytest_cache/v/cache": b"x" * 10_000,
        "logs/run.log": b"x" * 10_000,
        "src/real.py": b"print('hi')\n",
    }
    project = make_project("messy", extra_files=extra)

    result = run_runner("--print-plan", "--dir", project, "--max-bytes", "60000")
    assert result.returncode == 0, result.stderr
    stats = _plan(result)["stats"]
    assert stats["bytes"] < 60_000, f"artifacts leaked into the copy: {stats}"
    assert stats["skipped_dirs"] >= 4, stats
    assert stats["skipped_files"] >= 1, stats


def test_timeout_kills_group_and_cleans_up(run_runner, make_project, sandbox_root, tmp_path) -> None:
    pidfile = tmp_path / "child.pid"
    body = (
        "import os, time\n"
        "from pathlib import Path\n"
        "def test_slow():\n"
        "    Path(os.environ['MARS_TEST_LEAK_PIDFILE']).write_text(str(os.getpid()))\n"
        "    time.sleep(120)\n"
    )
    project = make_project("slow", test_body=body)

    started = time.monotonic()
    result = run_runner(
        "--dir",
        project,
        "--no-sandbox",
        "--timeout",
        "3",
        env_extra={"MARS_TEST_LEAK_PIDFILE": str(pidfile)},
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 124, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert elapsed < 60, "timeout must stop the run, not wait for the test"
    assert _sandbox_entries(sandbox_root) == [], "sandbox left behind after timeout"

    assert pidfile.is_file(), "the child never started — timeout path not exercised"
    child_pid = int(pidfile.read_text().strip())
    assert not _pid_alive(child_pid), f"child process {child_pid} leaked after timeout"


def test_sigterm_cleans_up_sandbox_and_children(runner_env, make_project, sandbox_root, tmp_path) -> None:
    pidfile = tmp_path / "child-sigterm.pid"
    body = (
        "import os, time\n"
        "from pathlib import Path\n"
        "def test_slow():\n"
        "    Path(os.environ['MARS_TEST_LEAK_PIDFILE']).write_text(str(os.getpid()))\n"
        "    time.sleep(120)\n"
    )
    project = make_project("sigterm", test_body=body)
    env = dict(runner_env)
    env["MARS_TEST_LEAK_PIDFILE"] = str(pidfile)

    proc = subprocess.Popen(
        [
            sys.executable,
            str(RUNNER),
            "--dir",
            str(project),
            "--no-sandbox",
            "--timeout",
            "60",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not pidfile.is_file():
            time.sleep(0.1)
        assert pidfile.is_file(), "child never started"
        assert _sandbox_entries(sandbox_root), "sandbox should exist while the tests run"

        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)

    assert proc.returncode != 0, f"SIGTERM must not look like success: {out}\n{err}"
    assert _sandbox_entries(sandbox_root) == [], "SIGTERM left the sandbox behind"
    child_pid = int(pidfile.read_text().strip())
    assert not _pid_alive(child_pid), f"child process {child_pid} survived SIGTERM"


def test_stale_sandbox_is_swept(run_runner, make_project, sandbox_root) -> None:
    stale = sandbox_root / "mars-test-abandoned"
    stale.mkdir()
    (stale / "leftover.bin").write_bytes(b"x" * 1024)
    marker = stale / ".mars-test-run"
    marker.write_text("1")
    old = time.time() - 3 * 24 * 3600
    os.utime(marker, (old, old))

    project = make_project("after-sweep")
    result = run_runner("--dir", project, "--no-sandbox", "--timeout", "60")
    assert result.returncode == 0, result.stderr
    assert not stale.exists(), "abandoned sandbox was not swept"
    assert _sandbox_entries(sandbox_root) == []


def test_foreign_dirs_are_not_swept(run_runner, make_project, sandbox_root) -> None:
    foreign = sandbox_root / "mars-test-not-ours"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep")  # no ownership marker
    old = time.time() - 30 * 24 * 3600
    os.utime(foreign, (old, old))

    project = make_project("keep-foreign")
    result = run_runner("--dir", project, "--no-sandbox", "--timeout", "60")
    assert result.returncode == 0, result.stderr
    assert (foreign / "keep.txt").is_file(), "directories without our marker must be left alone"


def test_broken_venv_pytest_is_skipped(tmp_path) -> None:
    """A .venv/bin/pytest whose interpreter was deleted must not be chosen."""
    module = _load_runner()
    project = tmp_path / "broken-venv"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    venv_bin = project / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    broken = venv_bin / "pytest"
    broken.write_text("#!/nonexistent/python3\n")
    broken.chmod(0o755)

    assert module._usable_bin(broken) is False
    cmd = module.detect_test_command(project)
    assert cmd is None or str(broken) not in cmd, f"broken pytest chosen: {cmd}"


def test_usable_bin_accepts_real_interpreter() -> None:
    module = _load_runner()
    assert module._usable_bin(Path(sys.executable)) is True
    assert module._usable_bin(Path("/nonexistent/definitely-missing")) is False


def _load_runner():
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location("mars_test_runner_under_test", RUNNER)
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_sandbox_is_disk_not_tmpfs() -> None:
    module = _load_runner()

    assert module.DEFAULT_TMP_ROOT == "/var/tmp"
    if not Path("/proc/mounts").is_file():
        pytest.skip("no /proc/mounts")
    assert module.is_tmpfs(Path("/var/tmp")) is False, "/var/tmp must not be RAM"


def _pid_alive(pid: int, timeout: float = 5.0) -> bool:
    """True if the pid is still running (waits briefly for the kill to land)."""
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.is_dir():
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            stat = (proc_dir / "stat").read_text()
        except OSError:
            return False
        state = stat.rsplit(")", 1)[-1].split()[0] if ")" in stat else ""
        if state == "Z":  # zombie: already dead, just not reaped
            return False
        time.sleep(0.1)
    return proc_dir.is_dir()


def test_ops_selfcheck_layout_is_complete() -> None:
    for rel in ("mars-test.py", "install.sh", "plugin/__init__.py", "plugin/plugin.yaml", "README.md"):
        assert (OPS_DIR / rel).is_file(), f"missing ops/selfcheck/{rel}"
    assert PLUGIN_DIR.is_dir()
