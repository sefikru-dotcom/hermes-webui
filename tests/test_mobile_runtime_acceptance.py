"""Opt-in real-browser mobile acceptance against an isolated WebUI server.

Run with MARS_MOBILE_SMOKE_URL pointing at an isolated server, never production.
"""
import os
from pathlib import Path

import pytest


BASE = os.environ.get("MARS_MOBILE_SMOKE_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="requires isolated mobile smoke server")


@pytest.mark.parametrize("width,height", [(390, 844), (412, 915)])
def test_mobile_runtime_acceptance(width, height, tmp_path):
    from playwright.sync_api import sync_playwright

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "mobile file acceptance\n"
    (workspace / "mobile-smoke.txt").write_text(content, encoding="utf-8")
    evidence = Path(os.environ.get("MARS_MOBILE_EVIDENCE", str(tmp_path / "evidence")))
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": height},
                                      is_mobile=True, has_touch=True,
                                      permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function("typeof api==='function' && typeof loadSession==='function'")
        page.wait_for_function("()=>S._bootReady===true", timeout=60000)
        page.evaluate("""async workspace=>{
          const data=await api('/api/session/new',{method:'POST',body:JSON.stringify({workspace,worktree:false})});
          await loadSession(data.session.session_id);
        }""", str(workspace))
        page.locator("#composerMobileConfigBtn").click()
        panel = page.locator("#composerMobileConfigPanel")
        assert panel.is_visible()
        assert not panel.evaluate("el=>el.inert")
        assert page.locator("#composerMobileSavedPromptsAction").inner_text().strip() != "▣"
        page.screenshot(path=str(evidence / f"command-center-{width}.png"))
        for action, target in (("composerMobileModelAction", "composerModelDropdown"),
                               ("composerMobileSavedPromptsAction", "savedPromptsPopup"),
                               ("composerMobileToolsetsAction", "composerToolsetsDropdown")):
            if not panel.is_visible():
                page.locator("#composerMobileConfigBtn").click()
            page.locator("#" + action).click()
            assert page.locator("#" + target).is_visible(), target
            page.locator("#msg").press("Escape")
            page.evaluate("closeMobileComposerConfig();closeModelDropdown();closeToolsetsDropdown()")
        page.evaluate("toggleWorkspacePanel()")
        filename = page.locator(".file-name", has_text="mobile-smoke.txt")
        filename.wait_for(state="visible")
        row = filename.locator("..")

        def long_press():
            row.dispatch_event("pointerdown", {"pointerType": "touch", "clientX": 80, "clientY": 120})
            page.locator(".file-ctx-menu-mobile").wait_for(state="visible")
            row.dispatch_event("pointerup", {"pointerType": "touch"})
            row.dispatch_event("click")
            assert page.locator(".file-ctx-menu-mobile").is_visible()
            assert not page.locator("#btnDownloadFile").is_visible()

        long_press()
        menu = page.locator(".file-ctx-menu-mobile")
        page.screenshot(path=str(evidence / f"workspace-sheet-{width}.png"))
        menu.get_by_text("Copy file path", exact=True).click()
        page.wait_for_function("async expected=>(await navigator.clipboard.readText())===expected", arg=str(workspace / "mobile-smoke.txt"))
        long_press()
        with page.expect_download(timeout=10000) as download:
            menu.get_by_text("Download", exact=True).click()
        assert Path(download.value.path()).read_text(encoding="utf-8") == content
        long_press()
        menu.get_by_text("Rename", exact=True).click()
        page.locator(".app-dialog-overlay input").fill("renamed.txt")
        page.locator(".app-dialog-overlay").get_by_role("button", name="Rename", exact=True).click()
        page.locator(".file-name", has_text="renamed.txt").wait_for(state="visible")
        assert (workspace / "renamed.txt").read_text(encoding="utf-8") == content
        filename = page.locator(".file-name", has_text="renamed.txt")
        row = filename.locator("..")
        long_press()
        menu.get_by_text("Delete", exact=True).click()
        page.locator(".app-dialog-overlay").get_by_role("button", name="Delete", exact=True).click()
        page.locator(".file-name", has_text="renamed.txt").wait_for(state="detached")
        assert not (workspace / "renamed.txt").exists()
        page.evaluate("toggleWorkspacePanel()")
        page.locator("#msg").fill("keyboard geometry check")
        page.set_viewport_size({"width": width, "height": 480})
        page.locator("#msg").focus()
        send = page.locator("#btnSend")
        box = send.bounding_box()
        assert box and box["y"] >= 0 and box["y"] + box["height"] <= 480
        assert box["height"] >= 44
        page.locator("#composerMobileConfigBtn").click()
        assert panel.is_visible()
        assert panel.evaluate("el=>getComputedStyle(el).overflowY") == "auto"
        page.screenshot(path=str(evidence / f"keyboard-emulated-{width}.png"))
        assert not errors, errors
        context.close()
        browser.close()
