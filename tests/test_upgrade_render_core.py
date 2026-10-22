"""Behavioural gate for the render/mode/scroll core (upgrade 2026-10-22).

Wrapper around ``scripts/test_upgrade_render_core.mjs``. That script drives the
REAL modules (``static/chat_render_patch.js``, ``static/chat_scroll_controller.js``,
``static/chat_render_activity_block.js``) against a dependency-free DOM
(``scripts/lib/mini_dom.mjs``) and asserts observable behaviour:

  * a re-render preserves the DOM nodes of unchanged transcript rows (the keyed
    patch replaced ``#msgInner.innerHTML=''``), swaps a changed row in its own
    slot, and drops a vanished row — i.e. no transcript wipe on a normal update;
  * ``stream_quiet`` HIDES the live activity rows instead of destroying them, and
    ``quiet -> full`` restores the same nodes with no further event;
  * window and full share one disclosure shell, and a disclosure decision made on
    the live turn still applies after the turn settles (one stable turn key);
  * programmatic scroll writes coalesce to at most one per frame, no-op when the
    viewport is already at the bottom, are refused for an unpinned reader, and a
    superseded/cancelled settle can never write.

Nothing here touches the production server, config or services; the harness runs
entirely in a subprocess with an in-memory DOM and in-memory storage.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "scripts" / "test_upgrade_render_core.mjs"


@pytest.fixture(scope="module")
def harness_run() -> subprocess.CompletedProcess:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the render-core behaviour harness")
    proc = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
    )
    return proc


def test_render_core_behaviour_is_green(harness_run):
    """Every behavioural assertion in the harness must pass.

    The harness prints ``ok <n> assertions`` on success and one ``✗`` line per
    failure with exit code 1; a crash (module load error, syntax error) exits
    with a different code and is never acceptable.
    """
    assert harness_run.returncode in (0, 1), (
        f"harness crashed (rc={harness_run.returncode}); stderr:\n{harness_run.stderr[-2000:]}"
    )
    assert harness_run.returncode == 0, (
        "render-core behaviour regressions:\n"
        f"{harness_run.stdout[-3000:]}\n{harness_run.stderr[-2000:]}"
    )
    assert "ok " in harness_run.stdout and "assertions" in harness_run.stdout


def test_render_core_assertion_count(harness_run):
    """Guard against a silently shrinking harness (an empty run must not pass)."""
    if harness_run.returncode != 0:
        pytest.skip("behaviour failures reported by test_render_core_behaviour_is_green")
    marker = [ln for ln in harness_run.stdout.splitlines() if ln.startswith("ok ")]
    assert marker, harness_run.stdout[-2000:]
    count = int(marker[-1].split()[1])
    assert count >= 45, f"only {count} assertions ran — harness lost coverage"
