"""Behavioural tests for the unified viewport controller (layout zone).

Runs the real static/viewport_controller.js under Node with fake window and
document objects — no browser, no jsdom, no server. The controller is the single
owner of visualViewport/resize listeners, so these tests pin:

  * geometry math (keyboard occlusion, pinch zoom, browser chrome, no-vv hosts),
  * the CSS variables and data attributes style.css/boot.js consume,
  * one listener registration per event even across repeated init,
  * rAF coalescing and de-duplicated subscriber delivery.

Run:
    HOME=$(mktemp -d) HERMES_HOME=$(mktemp -d) PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
      /opt/hermes-webui/.venv/bin/python3 -m pytest tests/test_upgrade_layout_viewport.py -q
"""

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "test_upgrade_layout_viewport.js"
MODULE = ROOT / "static" / "viewport_controller.js"


def _node_available():
    return shutil.which("node") is not None


@pytest.fixture(scope="module")
def harness_output():
    if not _node_available():
        pytest.skip("node is required for the viewport harness")
    result = subprocess.run(
        ["node", str(HARNESS)],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_viewport_geometry_harness_passes(harness_output):
    assert "PASS" in harness_output
    assert "FAIL" not in harness_output


def test_module_exposes_classic_script_api():
    """The integrator wires this as a plain <script> before boot.js."""
    source = MODULE.read_text(encoding="utf-8")
    for symbol in (
        "root.MarsViewport = api",
        "function init(",
        "function flush(",
        "function onChange(",
        "computeViewportState",
    ):
        assert symbol in source, f"viewport_controller.js must expose {symbol}"
    assert "typeof module !== 'undefined'" in source, "Node harness needs the CommonJS export"
    # No pointer-type gating: a phone that reports a fine pointer (stylus, DeX,
    # bluetooth mouse) still gets keyboard geometry.
    assert "pointer:coarse" not in source
    assert "hover:none" not in source


def test_module_does_not_register_competing_listeners_on_window_resize():
    """Exactly one resize/scroll registration set lives in the module."""
    source = MODULE.read_text(encoding="utf-8")
    assert source.count("this._listen(") == 6, "one registration helper, six event sources"
    assert source.count("this._listen(vv, 'resize'") == 1
    assert source.count("this._listen(vv, 'scroll'") == 1
    assert source.count("this._listen(this.win, 'resize'") == 1
    assert "requestAnimationFrame" in source
    # One pending frame at a time; refresh() must not queue timers per event.
    assert "if (this._frame) return;" in source
