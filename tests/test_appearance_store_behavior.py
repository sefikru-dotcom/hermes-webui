"""Поведенческие тесты единого appearance-состояния и автосейва.

Запускают tests/js/appearance_store_behavior.js через node: реальный
static/appearance_store.js + реальные функции panels.js в изолированном
vm-контексте, с фейковым POST /api/settings. Живые настройки и браузер не
нужны; при отсутствии node тест помечается skip (в CI node есть — см.
.github/workflows/tests.yml, шаг node --check).
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

TESTS_DIR = pathlib.Path(__file__).parent.resolve()
REPO_ROOT = TESTS_DIR.parent
STATIC = REPO_ROOT / "static"
HARNESS = TESTS_DIR / "js" / "appearance_store_behavior.js"

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
def test_appearance_store_behavior_harness_passes():
    proc = _run_harness()
    assert proc.returncode == 0, (
        "поведенческие тесты appearance упали\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "OK:" in proc.stdout
    assert "FAILED" not in proc.stderr


def test_store_is_classic_script_with_single_mode_dictionary():
    src = (STATIC / "appearance_store.js").read_text(encoding="utf-8")
    # Классический скрипт: без ESM-экспортов, регистрируется на window.
    assert "export " not in src.splitlines()[0]
    assert "window.AppearanceStore" in src or "root.AppearanceStore=store" in src
    # Единственный словарь режимов: легаси-имена маппятся в канон.
    assert "transparent_stream:'stream_full'" in src
    assert "compact_worklog:'stream_window'" in src
    assert "hide_all_activity:'stream_quiet'" in src


def test_header_mode_path_does_not_rebuild_transcript():
    """P1: кнопка режима в шапке не перерисовывает ленту и не шлёт снапшот."""
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    start = panels.index("function _pickChatActivityDisplayMode(")
    body = panels[start:panels.index("\nif(typeof window!=='undefined') window._pickChatActivityDisplayMode", start)]
    # Быстрый путь — store (применение + diff одного ключа) и выход без rebuild.
    store_at = body.index("S.set('chat_activity_display_mode',next")
    render_at = body.index("renderMessages(")
    assert store_at < render_at, "store-путь должен идти до legacy-rebuild"
    store_branch = body[store_at:render_at]
    assert "return;" in store_branch, "store-путь обязан выходить до rebuild"
    # Legacy-rebuild остаётся только фолбэком, когда store не подключён.
    assert "if(S){" in body


def test_appearance_payload_is_store_diff_not_dom_snapshot():
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    start = panels.index("function _appearancePayloadFromUi(")
    body = panels[start:panels.index("function _appearancePayloadFromUiLegacy(", start)]
    # Пока контролы не гидратированы, DOM не является источником значений.
    assert "_settingsAppearanceControlsBound" in body
    assert "S.diff()" in body
    # Старый полный снапшот вынесен в отдельную функцию-фолбэк.
    assert "return _appearancePayloadFromUiLegacy()" in body


def test_settings_open_hydrates_store_before_controls():
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    start = panels.index("async function loadSettingsPanel(")
    body = panels[start:panels.index("async function ", start + 10)]
    hydrate_at = body.index(".hydrate(settings)")
    theme_at = body.index("const themeSel=$('settingsTheme')")
    assert hydrate_at < theme_at, "store должен гидратироваться до заполнения контролов"
    assert "_settingsAppearanceControlsBound=true" in body, "контролы помечаются связанными после гидратации"
    assert body.index("_settingsAppearanceControlsBound=true") > theme_at


def test_theme_has_single_signal_from_boot():
    """Тема применяется только через boot._applyTheme: класс .dark на <html>.

    Ни panels.js, ни store не вводят вторую тему (класс .light, data-theme):
    иначе состояние страницы стало бы двойным и расходилось бы с CSS, где светлая
    палитра — это `:root`, а тёмная — `:root.dark`.
    """
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    store_src = (STATIC / "appearance_store.js").read_text(encoding="utf-8")
    for src, name in ((panels, "panels.js"), (store_src, "appearance_store.js")):
        assert "dataset.theme=" not in src, f"{name} не должен писать data-theme"
        assert "classList.add('light')" not in src, f"{name} не должен вводить класс .light"
        assert "classList.toggle('light'" not in src, f"{name} не должен вводить класс .light"
    # Применение — вызовом существующей boot-функции с одним аргументом-именем.
    assert "_applyTheme(value)" in panels


def test_theme_apply_checks_actual_page_state_not_only_memo():
    """`_appliedAppearance` — памятка panels, boot её не обновляет.

    Поэтому решение «применять ли тему» обязано сверяться с фактическим
    состоянием страницы: иначе выбор видимой темы после старта из localStorage
    не доезжает до DOM.
    """
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    start = panels.index("function _applyAppearanceKeyValue(")
    body = panels[start:panels.index("case 'skin':", start)]
    assert "_appliedThemeName()" in body, "сверка с реально применённой темой"
    assert "_resolvedThemeBaseDark" in panels, "источник истины — разрешённая тема boot"
    assert "localStorage.setItem('hermes-theme'" not in body, (
        "применение темы не должно подменять hermes-theme: boot читает его на старте "
        "как последний выбор пользователя"
    )


def test_settings_open_reconciles_page_theme_before_hydrate():
    """Расхождение «страница ≠ сервер» чинится до гидратации, иначе снимок сервера
    перетирает видимую тему, а повторный выбор видимой темы не даёт diff."""
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    start = panels.index("async function loadSettingsPanel(")
    body = panels[start:panels.index("async function ", start + 10)]
    assert "_reconcileStoreThemeWithAppliedTheme()" in body
    assert body.index("_reconcileStoreThemeWithAppliedTheme()") < body.index(".hydrate(settings)")


def test_handoff_documents_index_and_boot_wiring():
    handoff = (REPO_ROOT / "HANDOFF-appearance.md").read_text(encoding="utf-8")
    # Точный тег подключения и порядок (store до panels.js).
    assert '<script src="/static/appearance_store.js"></script>' in handoff
    assert handoff.index("appearance_store.js") < handoff.index("panels.js")
    # Точка гидратации в boot.js — сразу после загрузки настроек.
    assert "AppearanceStore.hydrate(s)" in handoff
    # Контракт с автором ui.js: callback point рендер-слоя.
    assert "_registerActivityModeRenderer" in handoff
    assert "_marsActivityModeChanged" in handoff, "совместимость со старым хуком"


def test_renderer_registration_is_exposed_by_panels():
    """Callback point для live-rerender объявлен в panels.js и не требует ui.js."""
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    assert "window._registerActivityModeRenderer=_registerActivityModeRenderer" in panels
    # Рендерер вызывается синхронно внутри быстрого применения режима.
    start = panels.index("function _applyActivityModeFast(")
    body = panels[start:panels.index("let _legacyActivityRenderTimer", start)]
    assert "renderer(next,{reason:" in body
    assert "catch(e){ console.warn('[appearance] activity mode renderer failed'" in body


def test_store_exposes_explicit_rollback_and_revisions():
    src = (STATIC / "appearance_store.js").read_text(encoding="utf-8")
    assert "rollback:rollback" in src, "rollback доступен наружу"
    assert "rev:new Map()" in src, "ревизии ключей для защиты поздних правок"
    # adoptSaved принимает снимок ревизий и не перетирает более новую правку.
    start = src.index("function adoptSaved(saved,keys,revs)")
    body = src[start:src.index("function revert()", start)]
    assert "state.rev.get(key)||0)!==revs[key]" in body


def test_store_module_is_not_yet_in_index_html():
    """Скрипт подключает интегратор; пока его нет, panels.js работает по-старому.

    Тест фиксирует отсутствие «полуподключённого» состояния: если тег появился,
    HANDOFF должен быть уже не нужен — тогда этот тест удаляется вместе с
    пунктом handoff-чеклиста.
    """
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    if "appearance_store.js" in index:
        pytest.skip("appearance_store.js уже подключён в index.html — wiring выполнен")
    handoff = (REPO_ROOT / "HANDOFF-appearance.md").read_text(encoding="utf-8")
    assert "не подключён" in handoff or "не подключен" in handoff
