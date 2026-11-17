"""Fixtures for the ops/selfcheck regression suite.

Everything here is tiny and isolated: the fixtures build throwaway projects under
pytest's ``tmp_path``, point the runner's sandbox at ``tmp_path/sandbox`` and never
touch a real project, a model server or the live plugin installation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

import pytest

HERE = Path(__file__).resolve().parent
OPS_DIR = HERE.parent  # ops/selfcheck
RUNNER = OPS_DIR / "mars-test.py"
PLUGIN_DIR = OPS_DIR / "plugin"
INSTALLER = OPS_DIR / "install.sh"


@pytest.fixture
def sandbox_root(tmp_path: Path) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir()
    return root


@pytest.fixture
def runner_env(sandbox_root: Path) -> Dict[str, str]:
    env = os.environ.copy()
    env["MARS_TEST_TMPDIR"] = str(sandbox_root)
    env["MARS_TEST_DENY_ROOTS"] = "/root/workspace"
    env.pop("MARS_SELFTEST_DISABLE", None)
    env.pop("MARS_SELFCHECK_DISABLE", None)
    return env


@pytest.fixture
def run_runner(runner_env: Dict[str, str]) -> Callable[..., subprocess.CompletedProcess]:
    def _run(
        *args: object,
        timeout: int = 90,
        env_extra: Optional[Dict[str, str]] = None,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess:
        env = dict(runner_env)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(RUNNER), *[str(a) for a in args]],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(cwd) if cwd else None,
        )

    return _run


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[..., Path]:
    """A minimal project: a marker plus one passing unittest/pytest test."""

    def _make(
        name: str = "project",
        marker: Optional[str] = ".git",
        test_body: str = "def test_ok():\n    assert True\n",
        extra_files: Optional[Dict[str, bytes]] = None,
    ) -> Path:
        root = tmp_path / name
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "test_ok.py").write_text(test_body, encoding="utf-8")
        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        if marker == ".git":
            (root / ".git").mkdir()  # empty dir is enough: marker, not a real repo
        elif marker:
            (root / marker).write_text("", encoding="utf-8")
        for rel, payload in (extra_files or {}).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        return root

    return _make


@pytest.fixture
def load_plugin():
    """Import ops/selfcheck/plugin/__init__.py under a unique module name."""

    def _load():
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "mars_selfcheck_plugin_under_test", PLUGIN_DIR / "__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    return _load
