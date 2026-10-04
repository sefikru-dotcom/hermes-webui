"""Real mobile editor and navigation acceptance on an isolated WebUI."""
import os
from pathlib import Path

import pytest


BASE = os.environ.get("MARS_MOBILE_SMOKE_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="requires isolated mobile smoke server")


@pytest.mark.parametrize("width,height", [(390, 844), (412, 915), (915, 412), (1280, 800)])
def test_editor_split_and_touch_runtime(width, height, tmp_path):
    from playwright.sync_api import sync_playwright

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "edit.txt"
    target.write_text("original\n", encoding="utf-8")
    (workspace / "other.txt").write_text("second\n", encoding="utf-8")
    evidence = Path(os.environ.get("MARS_MOBILE_EVIDENCE", str(tmp_path / "evidence")))
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height},
                                      is_mobile=width < 1000, has_touch=width < 1000)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function("()=>S._bootReady===true", timeout=60000)
        sid = page.evaluate("""async workspace=>{
          const data=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace,worktree:false})});
          await loadSession(data.session.session_id);
          return data.session.session_id;
        }""", str(workspace))
        page.evaluate("toggleWorkspacePanel(true)")
        page.locator(".file-name", has_text="edit.txt").click()
        page.locator("#btnEditFile").click()
        editor = page.locator("#previewEditArea")
        editor.fill("cancelled change\n")
        cancel = page.locator("#btnCancelFileEdit")
        assert cancel.is_visible()
        cancel.click()
        assert not editor.is_visible()
        assert target.read_text(encoding="utf-8") == "original\n"
        page.locator("#btnEditFile").click()
        editor.fill("saved on mobile\n")
        page.locator("#btnEditFile").click()
        editor.wait_for(state="hidden")
        assert target.read_text(encoding="utf-8") == "saved on mobile\n"
        page.locator("#btnEditFile").click()
        editor.fill("keep unsaved\n")
        page.locator("#btnClearPreview").click()
        dialog = page.locator(".app-dialog-overlay")
        dialog.wait_for(state="visible")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        assert editor.input_value() == "keep unsaved\n"
        assert editor.is_visible()
        page.screenshot(path=str(evidence / f"editor-{width}.png"))
        if width <= 900:
            page.locator("#btnWorkspaceSplit").click()
            assert page.locator(".layout").evaluate("el=>el.classList.contains('mobile-workspace-split')")
            assert page.evaluate("S.session.session_id") == sid
            page.screenshot(path=str(evidence / f"split-{width}.png"))
            page.locator(".mobile-workspace-navigation").get_by_role("button", name="Chat", exact=False).click()
            assert editor.input_value() == "keep unsaved\n"
            page.evaluate("toggleWorkspacePanel(true)")
            assert editor.is_visible()
        page.locator("#btnCancelFileEdit").click()
        page.evaluate("closeWorkspacePanel()")
        # Render the existing board contract: no agent/kanban package is needed for this UI fixture.
        page.evaluate("""()=>{
          _kanbanBoard={columns:[{name:'ready',tasks:[{id:'touch-1',title:'First task',status:'ready'}]},
            {name:'done',tasks:[{id:'touch-2',title:'Second task',status:'done'}]}]};
          _kanbanRenderBoard();switchPanel('kanban');
        }""")
        if width < 1000:
            assert page.locator("#kanbanBoard").evaluate("el=>el.classList.contains('kanban-board-mobile-list')")
            assert page.locator("#kanbanBoard [draggable=true]").count() == 0
        else:
            assert not page.locator("#kanbanBoard").evaluate("el=>el.classList.contains('kanban-board-mobile-list')")
        page.screenshot(path=str(evidence / f"kanban-{width}.png"))
        assert not errors, errors
        context.close()
        browser.close()
