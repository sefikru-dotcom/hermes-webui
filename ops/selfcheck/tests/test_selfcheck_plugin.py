"""Regression tests for the mars-selfcheck plugin and its installer.

Pinned behaviour: the plugin must never ask the runner to copy a scratch tree
(/root/workspace), must pass explicit copy budgets, and must report a refusal or
a timeout as an informational skip instead of a red test failure.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent.parent
RUNNER = OPS_DIR / "mars-test.py"
PLUGIN_DIR = OPS_DIR / "plugin"
INSTALLER = OPS_DIR / "install.sh"


class _FakeProc:
    """Minimal Popen stand-in: code + output, and a pid that does not exist."""

    def __init__(self, code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = code
        self.pid = 999_999  # os.getpgid() raises ProcessLookupError for it
        self._out = stdout
        self._err = stderr

    def communicate(self, timeout=None):
        return self._out, self._err

    def poll(self):
        return self.returncode


def test_plugin_ignores_markerless_scratch_tree(load_plugin, tmp_path) -> None:
    plugin = load_plugin()
    scratch = tmp_path / "workspace"
    (scratch / "tests").mkdir(parents=True)
    target = scratch / "tests" / "test_any.py"
    target.write_text("def test_x():\n    assert True\n")

    assert plugin._find_project_root(target.parent) is None
    assert plugin._run_autotests(target) is None


def test_plugin_finds_real_project_marker(load_plugin, tmp_path) -> None:
    plugin = load_plugin()
    project = tmp_path / "real-project"
    (project / "pkg").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='x'\n")
    target = project / "pkg" / "mod.py"
    target.write_text("x = 1\n")

    assert plugin._find_project_root(target.parent) == project.resolve()


def test_plugin_refuses_denied_root_without_spawning(load_plugin, tmp_path, monkeypatch) -> None:
    plugin = load_plugin()
    project = tmp_path / "denied-project"
    (project / "tests").mkdir(parents=True)
    (project / ".git").mkdir()
    target = project / "tests" / "test_ok.py"
    target.write_text("def test_ok():\n    assert True\n")

    monkeypatch.setenv("MARS_SELFCHECK_DENY_ROOTS", str(project))
    monkeypatch.setenv("MARS_TEST_BIN", str(RUNNER))

    def _boom(*_args, **_kwargs):
        raise AssertionError("runner must not be spawned for a denied root")

    monkeypatch.setattr(plugin.subprocess, "Popen", _boom)
    assert plugin._root_refusal(project)
    assert plugin._run_autotests(target) is None


def test_plugin_command_carries_budgets(load_plugin, tmp_path, monkeypatch) -> None:
    plugin = load_plugin()
    project = tmp_path / "budgeted"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.setenv("MARS_TEST_BIN", str(RUNNER))

    cmd = plugin._autotest_command(project)
    assert cmd is not None
    assert cmd[0] == str(RUNNER)
    assert "--dir" in cmd and str(project) in cmd
    for flag in ("--timeout", "--copy-timeout", "--max-bytes", "--max-files"):
        assert flag in cmd, f"{flag} missing from {cmd}"
    max_bytes = int(cmd[cmd.index("--max-bytes") + 1])
    assert max_bytes <= 512 * 1024 * 1024


def test_plugin_budget_refusal_is_a_skip(load_plugin, tmp_path, monkeypatch) -> None:
    plugin = load_plugin()
    project = tmp_path / "big"
    (project / "tests").mkdir(parents=True)
    (project / ".git").mkdir()
    target = project / "tests" / "test_ok.py"
    target.write_text("def test_ok():\n    assert True\n")
    monkeypatch.setenv("MARS_TEST_BIN", str(RUNNER))
    monkeypatch.setenv("MARS_SELFCHECK_DENY_ROOTS", str(tmp_path / "nope"))

    monkeypatch.setattr(
        plugin.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(3, "", "⏭️ mars-test: пропуск — объём источника превысил лимит"),
    )
    block = plugin._run_autotests(target)
    assert block is not None
    assert "пропущены" in block and "❌" not in block


def test_plugin_timeout_is_a_skip_not_a_failure(load_plugin, tmp_path, monkeypatch) -> None:
    plugin = load_plugin()
    project = tmp_path / "slow"
    (project / "tests").mkdir(parents=True)
    (project / ".git").mkdir()
    target = project / "tests" / "test_slow.py"
    target.write_text("def test_slow():\n    pass\n")
    monkeypatch.setenv("MARS_TEST_BIN", str(RUNNER))
    monkeypatch.setenv("MARS_SELFCHECK_DENY_ROOTS", str(tmp_path / "nope"))

    class _TimeoutProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(-9, "", "")
            self._calls = 0

        def communicate(self, timeout=None):
            self._calls += 1
            if self._calls == 1:
                raise subprocess.TimeoutExpired(cmd="mars-test", timeout=timeout)
            return "", ""

    monkeypatch.setattr(plugin.subprocess, "Popen", lambda *a, **k: _TimeoutProc())
    block = plugin._run_autotests(target)
    assert block is not None
    assert "пропущены" in block and "❌" not in block


def test_plugin_test_failure_is_reported(load_plugin, tmp_path, monkeypatch) -> None:
    plugin = load_plugin()
    project = tmp_path / "failing"
    (project / "tests").mkdir(parents=True)
    (project / ".git").mkdir()
    target = project / "tests" / "test_bad.py"
    target.write_text("def test_bad():\n    assert False\n")
    monkeypatch.setenv("MARS_TEST_BIN", str(RUNNER))
    monkeypatch.setenv("MARS_SELFCHECK_DENY_ROOTS", str(tmp_path / "nope"))

    monkeypatch.setattr(
        plugin.subprocess,
        "Popen",
        lambda *a, **k: _FakeProc(1, "1 failed", "AssertionError"),
    )
    block = plugin._run_autotests(target)
    assert block is not None
    assert "❌" in block and "провалены" in block


def test_plugin_manifest_version_and_hook() -> None:
    text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    assert 'version: "1.2.0"' in text
    assert "transform_tool_result" in text
    assert "bounded" in text


def test_plugin_syntax_compiles() -> None:
    source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    compile(source, str(PLUGIN_DIR / "__init__.py"), "exec")


def test_installer_dry_run_changes_nothing(tmp_path) -> None:
    target = tmp_path / "tools" / "mars-test.py"
    link = tmp_path / "bin" / "mars-test"
    plugin_dir = tmp_path / "plugins" / "mars-selfcheck"

    env = os.environ.copy()
    env.update(
        {
            "MARS_SELFCHECK_TARGET": str(target),
            "MARS_SELFCHECK_LINK": str(link),
            "MARS_SELFCHECK_PLUGIN_DIR": str(plugin_dir),
        }
    )
    result = subprocess.run(
        ["bash", str(INSTALLER), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "would:" in result.stdout
    assert not target.exists(), "dry-run installed the runner"
    assert not link.exists(), "dry-run created the symlink"
    assert not plugin_dir.exists(), "dry-run installed the plugin"


def test_installer_is_dry_run_first_and_backs_up() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert ".bak-$STAMP" in text, "installer must back up live files before replacing them"
    assert "set -euo pipefail" in text
