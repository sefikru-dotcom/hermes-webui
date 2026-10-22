"""Поведенческие тесты резолва reasoning-пина (audit: pin «Auto» bug).

Запускают tests/js/reasoning_pin_behavior.js через node: реальный
static/model_identity.js + реальные функции ui.js в изолированном vm-контексте.
Браузер и живые настройки не нужны; при отсутствии node тест помечается skip.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

TESTS_DIR = pathlib.Path(__file__).parent.resolve()
REPO_ROOT = TESTS_DIR.parent
STATIC = REPO_ROOT / "static"
HARNESS = TESTS_DIR / "js" / "reasoning_pin_behavior.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(
    NODE is None,
    reason="node не установлен — поведенческие JS-тесты не запускаются",
)


def _run_harness() -> subprocess.CompletedProcess:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(HARNESS)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


@requires_node
def test_reasoning_pin_behavior_harness_passes():
    proc = _run_harness()
    assert proc.returncode == 0, (
        "поведенческие тесты reasoning-пина упали\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "OK:" in proc.stdout
    assert "FAILED" not in proc.stderr


def test_ui_delegates_pin_lookup_to_model_identity():
    """Единственное правило ключа пина — api/model_identity (+ его браузерный близнец)."""
    ui = (STATIC / "ui.js").read_text(encoding="utf-8")
    start = ui.index("function _marsReasoningOverrideFor(")
    body = ui[start:ui.index("\nfunction _marsReasoningLabel(", start)]
    assert "MarsModelIdentity.findPin(overrides,target,lane)" in body
    # Полоса (provider) — часть идентичности пина, а не догадка по голому id.
    assert "function _marsReasoningOverrideFor(model,provider)" in body
    # Фолбэк остаётся, чтобы старый кэш/харнесс без модуля не терял легаси-ключи.
    assert "typeof MarsModelIdentity!=='undefined'" in body


def test_ui_does_not_strip_qualified_model_from_chip_payload():
    ui = (STATIC / "ui.js").read_text(encoding="utf-8")
    start = ui.index("function _marsReasoningChipPayload(")
    body = ui[start : ui.index("\n}\n", start) + 3]
    assert "payload.model=" not in body, "квалифицированный model не переписывается"
    assert "_marsReasoningOverrideFor(model,provider)" in body
    assert "payload.scope='model'" in body
    # Единственная точка сборки payload: определение + вызов из обработчика чипа.
    assert ui.count("_marsReasoningChipPayload(effort)") == 2
    # Старый стрип квалификации не вернулся ни в одной ветке.
    assert "_marsBareModelId(_ctxModel)" not in ui


def test_effort_pills_pass_provider_lane():
    ui = (STATIC / "ui.js").read_text(encoding="utf-8")
    start = ui.index("function _marsPaintEffortList(")
    body = ui[start:ui.index("\nfunction ", start + 10)]
    assert "function _marsPaintEffortList(list,status,pill,bare,provider)" in body
    assert "status.provider||status.model_provider" in body
    assert "_marsReasoningOverrideFor(bare,lane)" in body
    # Все вызовы передают полосу: без неё пин на qualified-ключе снова станет «Auto».
    assert "_marsPaintEffortList(list,status,pill,bare,provider)" in ui
    assert "_marsPaintEffortList(list,status,pill,model,provider)" in ui
