"""ops/notify/mars-notify.py must resolve a live Python for Web Push.

The live host file used `/opt/hermes-webui/.venv/bin/python3` as its fallback
interpreter; that symlink dangled after `uv` cleaned its managed CPython, so the
fallback pointed at nothing. These tests pin the versioned copy's behaviour: a
broken candidate is skipped, and the chosen interpreter is actually executable.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "ops" / "notify" / "mars-notify.py"

pytestmark = pytest.mark.skipif(
    not Path("/usr/local/lib/hermes-agent").exists(),
    reason="Mars host layout (/usr/local/lib/hermes-agent) is not present",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("mars_notify_under_test", NOTIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # no re-exec: that only runs under __main__
    return module


def test_notify_copy_exists():
    assert NOTIFY.is_file()


def test_push_python_is_executable():
    module = _load_module()
    interpreter = module._push_python()
    assert os.path.isfile(interpreter), interpreter
    assert os.access(interpreter, os.X_OK), interpreter


def test_push_python_skips_dangling_webui_venv(monkeypatch, tmp_path):
    """With the runtime glob empty, the dangling venv link must not be returned."""
    module = _load_module()
    dangling = tmp_path / "python3"
    dangling.symlink_to(tmp_path / "deleted")
    monkeypatch.setattr(module, "RUNTIME_PY_GLOB", str(tmp_path / "no-such-glob-*"))
    monkeypatch.setattr(module, "WEBUI_VENV_PY", str(dangling))

    interpreter = module._push_python()
    assert interpreter != str(dangling)
    assert os.access(interpreter, os.X_OK)


def test_push_python_fails_loudly_when_nothing_is_usable(monkeypatch, tmp_path):
    module = _load_module()
    monkeypatch.setattr(module, "RUNTIME_PY_GLOB", str(tmp_path / "no-such-glob-*"))
    monkeypatch.setattr(module, "WEBUI_VENV_PY", str(tmp_path / "missing-python3"))
    monkeypatch.setattr(module, "_executable", lambda path: False)

    with pytest.raises(RuntimeError):
        module._push_python()


def test_push_pythonpath_entries_exist():
    module = _load_module()
    missing = [p for p in module.PUSH_PYTHONPATH if not os.path.isdir(p)]
    assert missing == []


def test_push_pythonpath_has_no_hardcoded_python_version():
    text = NOTIFY.read_text(encoding="utf-8")
    assert ".venv/lib/python3.12/site-packages" not in text
