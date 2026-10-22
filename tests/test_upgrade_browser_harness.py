"""pytest entry point for the WebUI upgrade browser regression suite.

The real work lives in scripts/upgrade_browser_harness.py (mock HTTP/SSE backend
on an ephemeral port + real Chromium against the real static app). This wrapper
only makes the suite discoverable by the repo's pytest run and enforces the
workspace rules for heavy checks: a lock, an isolated HOME, and no live venv
dependency.

Run directly:
    python3 -m pytest tests/test_upgrade_browser_harness.py -q

Tuning:
    UPGRADE_VIEWPORTS=desktop        # faster local loop (default: desktop,mobile)
    UPGRADE_APP_ROOT=../integration  # re-run the same suite on the merged tree
    UPGRADE_ONLY=stream_reading_scroll
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "scripts" / "upgrade_browser_harness.py"
BASELINE = REPO / "tests" / "test_upgrade_browser_baseline.json"
LOCK = "/tmp/mars-webui-upgrade-tests.lock"

# A browser and a driver must exist; otherwise the suite is "not applicable"
# rather than "passing". Never install anything from a test.
def _browser_available() -> tuple[bool, str]:
    if not shutil.which("node"):
        return False, "node not found"
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/root/.cache/ms-playwright")
    if not cache.is_dir() or not any(cache.glob("chromium-*/chrome-linux*/chrome")):
        return False, f"no chromium under {cache}"
    if not Path("/tmp/upgrade-browser-node/node_modules/playwright-core").is_dir():
        return False, "playwright-core not installed in /tmp/upgrade-browser-node"
    return True, ""


@pytest.mark.skipif(not _browser_available()[0], reason=_browser_available()[1] or "browser runtime missing")
def test_upgrade_browser_suite():
    """Whole suite must be green: no new failures beyond the checked-in baseline."""
    out = Path(tempfile.mkdtemp(prefix="upgrade-browser-")) / "report.json"
    env = dict(os.environ)
    env.setdefault("UPGRADE_VIEWPORTS", "desktop,mobile")
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/root/.cache/ms-playwright")
    env["HOME"] = tempfile.mkdtemp(prefix="upgrade-home-")
    env["HERMES_HOME"] = tempfile.mkdtemp(prefix="upgrade-hermes-")
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    cmd = [
        sys.executable, str(HARNESS),
        "--app-root", env.get("UPGRADE_APP_ROOT", str(REPO)),
        "--viewports", env["UPGRADE_VIEWPORTS"],
        "--baseline", str(BASELINE),
        "--out", str(out),
    ]
    if env.get("UPGRADE_ONLY"):
        cmd += ["--only", env["UPGRADE_ONLY"]]

    # Heavy checks are serialised across the wave so parallel authors cannot
    # fight over CPU or a shared browser cache.
    lock_cmd = ["flock", LOCK, *cmd] if shutil.which("flock") else cmd
    proc = subprocess.run(lock_cmd, cwd=str(REPO), env=env, capture_output=True, text=True, timeout=1800)
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-40:])
    assert proc.returncode == 0, f"browser suite failed (rc={proc.returncode})\n{tail}"

    report = json.loads(out.read_text(encoding="utf-8"))
    totals = report["totals"]
    assert totals["scenarios"] >= 1, totals
    if not env.get("UPGRADE_ONLY"):
        assert totals["scenarios"] >= 2, totals
        viewports = {r["viewport"] for r in report["results"]}
        assert {"desktop", "mobile"} <= viewports or env["UPGRADE_VIEWPORTS"] != "desktop,mobile", viewports
    assert totals["passed"] == totals["scenarios"], totals
    assert totals["baseline_fixed"] == 0, (
        "baseline entries now pass — remove them from tests/test_upgrade_browser_baseline.json: " + tail
    )
    # Non-negotiable safety properties of the harness itself.
    for res in report["results"]:
        names = {c["name"]: c for c in res["checks"]}
        assert names["no external network requests (no LLM calls)"]["ok"], (res["name"], res["viewport"])
        assert names["no 5xx responses from the mock backend"]["ok"], (res["name"], res["viewport"])
