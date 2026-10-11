"""Behavioral RED test for the post-render scroll-suppression defect.

Companion wrapper around ``scripts/scroll_harness.mjs`` — a node:vm harness
that loads the REAL scroll-handling code cut out of ``static/ui.js`` (unique
content anchors, no copied logic) into a hand-rolled DOM stub and exercises
the ``#messages`` 'scroll' listener with real event objects.

Defect (base 53af756a, static/ui.js ~6820-6834): inside the post-render
artifact window (``_recentMessageRenderArtifactWindow(1400)``) an upward
scroll event with no recorded gesture intent is swallowed by
``_lastScrollTop=top; return;`` WITHOUT setting ``_messageUserUnpinned``.
Right after a reply finishes (``renderMessages()`` stamps
``_lastMessageRenderAt=performance.now()`` unconditionally, ui.js:17378) the
reader cannot escape auto-follow by scrolling up; only a page reload cures
it. The existing scroll tests are static (string/order assertions on the
source) and stay green while this bug is live — hence this behavioral gate.

Contract asserted here (the DESIRED behavior):
  (a) a user scroll-up shortly after render completion must NOT be
      suppressed -> on the buggy base this test is RED, which IS the proof
      that the defect is reproduced behaviorally;
  (b) a real gesture (gentle wheel / small touchmove inside the pane) must
      neutralize the suppression window regardless of render recency
      (fidelity anchor — green on base: the harness also sees accepted
      scrolls, so (a) is not red because everything is red);
  (c) with the window expired an unchanged input generation must not be
      suppressed, and a grown generation (real input) inside the window must
      be accepted (fidelity anchor — green on base).

No test server / repo fixtures are needed: the only external dependency is
``node`` running the harness as a subprocess.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "scripts" / "scroll_harness.mjs"
RESULT_PREFIX = "[scroll-harness] RESULT_JSON "


@pytest.fixture(scope="module")
def harness_result() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the scroll behavior harness")
    assert node is not None  # narrow Optional for type checkers
    proc = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
    )
    # Exit codes: 0 all PASS, 1 at least one FAIL (expected on buggy base),
    # 2 extraction/load failure (harness itself broken — never acceptable).
    assert proc.returncode in (0, 1), (
        f"harness crashed (rc={proc.returncode}); stderr:\n{proc.stderr[-2000:]}"
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith(RESULT_PREFIX)]
    assert lines, f"harness printed no RESULT_JSON line; stdout:\n{proc.stdout[-3000:]}"
    return json.loads(lines[-1][len(RESULT_PREFIX):])


def _check(harness_result: dict, name: str) -> dict:
    checks = harness_result.get("checks", {})
    assert name in checks, f"check {name!r} missing from harness output: {sorted(checks)}"
    return checks[name]


def _verdict(harness_result: dict, name: str) -> str:
    verdicts = harness_result.get("verdicts", {})
    assert name in verdicts, f"verdict {name!r} missing: {sorted(verdicts)}"
    return verdicts[name]


def test_harness_extracts_real_ui_js(harness_result):
    """Guard against a silently no-op harness: it must have cut the real
    fragments out of static/ui.js (behavioral test, not a source grep)."""
    extraction = harness_result["extraction"]
    assert extraction["ok"] is True, f"extraction failed: {extraction}"
    assert extraction["ui_js"] == "static/ui.js"
    a_lo, a_hi = extraction["fragment_a_lines"]
    b_lo, b_hi = extraction["fragment_b_lines"]
    assert a_hi - a_lo > 200, f"fragment A suspiciously small: {extraction['fragment_a_lines']}"
    assert b_hi - b_lo > 100, f"fragment B suspiciously small: {extraction['fragment_b_lines']}"
    # The suppression branch lives inside fragment B (the scroll listener IIFE).
    assert b_lo < 6835 <= b_hi or b_hi - b_lo > 100
    assert extraction["render_stamp_stmt"] == "_lastMessageRenderAt=performance.now();"
    assert extraction["render_stamp_line"] > 0


def test_scenario_a_post_render_user_scroll_up_not_suppressed(harness_result):
    """RED on base 53af756a — the proof the defect is reproduced.

    Desired contract: 300 ms after render completion an upward user scroll
    (bare scroll event a1, or keyboard PageUp with focus outside the pane a2)
    must move the reader away from auto-follow: _messageUserUnpinned=true and
    _scrollPinned=false. On the buggy base the artifact-window branch
    silently discards both (unpinned stays false)."""
    a1 = _check(harness_result, "a1_bare_scroll_up_300ms_after_render")
    a2 = _check(harness_result, "a2_keyboard_pageup_300ms_after_render")
    verdict = _verdict(harness_result, "a_post_render_user_scroll_up_not_suppressed")
    assert verdict == "PASS", (
        "DEFECT REPRODUCED (expected RED on buggy base): a user scroll-up right after a "
        "completed reply is swallowed by the post-render artifact window "
        "(static/ui.js ~6820-6834 `_lastScrollTop=top; return;`) without setting "
        f"_messageUserUnpinned. a1={json.dumps(a1['numbers'])} a2={json.dumps(a2['numbers'])}"
    )
    assert a1["numbers"]["unpinned"] is True and a1["numbers"]["pinned"] is False
    assert a2["numbers"]["unpinned"] is True and a2["numbers"]["pinned"] is False


def test_scenario_b_real_gesture_resets_suppression_window(harness_result):
    """Fidelity anchor (green on base): a real gesture inside the pane must
    neutralize the suppression window regardless of render recency — gentle
    wheel at +100 ms, small touchmove at +50 ms, wheel at the window edge
    (+1390 ms). If this ever goes red, the harness lost touch with the real
    listeners (or a fix broke accepted-input handling)."""
    for name in (
        "b1_gentle_wheel_up_100ms_after_render",
        "b2_touchmove_50ms_after_render",
        "b3_wheel_up_at_render_age_1390ms",
    ):
        check = _check(harness_result, name)
        assert check["pass"] is True, f"{name} failed: {json.dumps(check['numbers'])}"
    assert _verdict(harness_result, "b_real_gesture_resets_suppression_window") == "PASS"
    b1 = _check(harness_result, "b1_gentle_wheel_up_100ms_after_render")
    assert b1["numbers"]["generationAfterGesture"] > b1["numbers"]["generationBefore"]


def test_scenario_c_generation_input_acceptance(harness_result):
    """Fidelity anchor (green on base): window expired + generation unchanged
    -> processed, not suppressed (c1); generation grew via real input inside
    the window -> accepted (c2). Documents the input-generation numbers a
    generation-based fix must keep honoring."""
    for name in (
        "c1_expired_window_generation_unchanged",
        "c2_generation_grew_real_input_inside_window",
    ):
        check = _check(harness_result, name)
        assert check["pass"] is True, f"{name} failed: {json.dumps(check['numbers'])}"
    assert _verdict(harness_result, "c_generation_input_acceptance") == "PASS"
    c1 = _check(harness_result, "c1_expired_window_generation_unchanged")
    assert c1["numbers"]["renderArtifactWindow"] is False
    assert c1["numbers"]["generationAtRender"] == c1["numbers"]["generationAfter"]
