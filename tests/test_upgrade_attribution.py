"""Durable fallback attribution: behavioural + wiring tests.

Layer 1 (behavioural) runs ``scripts/test_upgrade_attribution.js`` under Node,
which loads ``static/turn_model_attribution.js`` directly and extracts the real
attribution helpers out of ``static/messages.js`` (same brace-matched extraction
style as ``test_issue6068_used_model_footer.py``).

Layer 2 (wiring) asserts the source-level invariants that keep the disclosure
durable in the shipped file: the marker is recorded *before* the 4-second
composer status is painted, it is persisted on the inflight record, it is
re-attached at settle, it survives the transcript replace via
``_EPHEMERAL_TURN_FIELDS``, and the badge is rendered as text only.

Run: pytest tests/test_upgrade_attribution.py
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = ROOT / "static" / "messages.js"
MODULE = ROOT / "static" / "turn_model_attribution.js"
NODE_TEST = ROOT / "scripts" / "test_upgrade_attribution.js"


def _messages_src():
    return MESSAGES.read_text(encoding="utf-8")


def test_behavioural_node_suite_passes():
    result = subprocess.run(
        ["node", str(NODE_TEST)], cwd=str(ROOT), text=True, capture_output=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checks passed" in result.stdout
    failed = re.search(r"^(\d+)/(\d+) checks passed", result.stdout, re.M)
    assert failed, result.stdout
    passed, total = int(failed.group(1)), int(failed.group(2))
    assert passed == total
    # A floor, so an extraction regression cannot silently shrink the suite.
    assert total >= 80, f"expected the full suite, ran only {total} checks"


def test_module_is_standalone_and_never_uses_innerHTML():
    src = MODULE.read_text(encoding="utf-8")
    assert "innerHTML" not in src
    assert "insertAdjacentHTML" not in src
    assert "document.write" not in src
    # UMD-ish export so the coordinator can include it as a plain <script>.
    assert "window.TurnModelAttribution" in src or "module.exports" in src


def test_marker_is_recorded_before_the_transient_status():
    src = _messages_src()
    branch = src.index("if(d.type==='fallback'){")
    build = src.index("_buildTurnAttributionMarker(d)", branch)
    status = src.index("setComposerStatus(`${d.message||'Warning'}`", branch)
    assert build < status, "the durable marker must be written before the 4s toast"


def test_marker_is_persisted_on_the_inflight_record():
    src = _messages_src()
    assert "INFLIGHT[activeSid].turnModelAttribution=_pendingTurnAttribution;" in src
    # A reconnect rebuilds the stream closure; the pending marker is rehydrated.
    assert "INFLIGHT[activeSid].turnModelAttribution)||null" in src


def test_marker_is_attached_to_the_current_assistant_turn_at_settle():
    src = _messages_src()
    assert "_finalizeTurnAttribution(S.messages,lastAsst,_pendingTurnAttribution);" in src
    # ... and cleared afterwards so the next turn cannot inherit it.
    assert "_pendingTurnAttribution=null;" in src
    assert "INFLIGHT[activeSid].turnModelAttribution=null;" in src


def test_marker_survives_the_transcript_replace():
    src = _messages_src()
    fields = re.search(r"const _EPHEMERAL_TURN_FIELDS=\[([^\]]*)\];", src)
    assert fields, "ephemeral field list not found"
    assert "'_turnModelAttribution'" in fields.group(1)


def test_no_synthetic_assistant_message_is_added():
    src = _messages_src()
    start = src.index("function _finalizeTurnAttribution(")
    end = src.index("function _errorModelAttributionMarker(")
    block = src[start:end]
    assert "S.messages.push" not in block
    assert "role:'assistant'" not in block and 'role:"assistant"' not in block


def test_badge_is_rendered_as_text_through_the_existing_footer():
    src = _messages_src()
    start = src.index("function _decorateTurnModelAttribution(")
    end = src.index("(function _wireTurnModelAttributionRender(", start)
    block = src[start:end]
    assert "badge.textContent=api.badgeText(" in block
    assert "innerHTML" not in block
    # Reuses the existing footer style hook instead of adding new CSS.
    assert "gateway-failover-inline" in block
    # Decorates the turn the renderer already produced.
    assert "data-msg-idx" in block
    assert "renderMessages" in src[end : end + 600]
