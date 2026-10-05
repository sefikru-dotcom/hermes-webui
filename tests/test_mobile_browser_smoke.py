"""Offline Playwright smoke shard for mobile chat chrome.

This is a real Chromium run against a deterministic DOM fixture; it deliberately
has no live LLM/server dependency. Runtime/server acceptance remains separate.
"""
from contextlib import contextmanager

import pytest

pw_api = pytest.importorskip("playwright.sync_api")

FIXTURE = """
<!doctype html><meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>
<style>
:root { --safe-top: env(safe-area-inset-top, 0px); --safe-bottom: env(safe-area-inset-bottom, 0px); }
body { margin:0; overflow:hidden; font:14px sans-serif; }
#app { height:100dvh; display:flex; flex-direction:column; }
#titlebar { min-height:52px; padding-top:var(--safe-top); }
#chat { flex:1; min-height:0; overflow:auto; }
#composer { padding:8px max(8px, env(safe-area-inset-left,0px)) calc(8px + var(--safe-bottom)); }
#composer textarea { min-height:40px; }
button { min-width:44px; min-height:44px; }
#sidebar,#command-center { position:fixed; inset:0; display:none; z-index:4; background:white; padding-top:var(--safe-top); }
#sidebar.open,#command-center.open { display:block; }
#backdrop { position:fixed; inset:0; display:none; background:#0008; z-index:3; }
#backdrop.open { display:block; }
</style>
<div id=app><header id=titlebar><button id=music>Music</button><button id=new>New</button><button id=session>Session</button></header>
<main id=chat></main><footer id=composer><textarea id=msg></textarea><button id=send>Send</button><button id=config>Command center</button></footer></div>
<div id=sidebar><button id=sidebar-close>Close</button><button id=chat-nav>Chat</button></div>
<div id=command-center><button id=attach>Attach</button><button id=prompts>Prompts</button><button id=toolsets>Toolsets</button><button id=command-close>Close</button></div><div id=backdrop></div>
<script>
const chat=document.querySelector('#chat');
for(let i=0;i<40;i++){const p=document.createElement('p');p.textContent='stream chunk '+i;chat.append(p)}
function openSidebar(){sidebar.classList.add('open')} function closeSidebar(){sidebar.classList.remove('open')}
function openCommand(){document.querySelector('#command-center').classList.add('open');backdrop.classList.add('open')}
function closeCommand(){document.querySelector('#command-center').classList.remove('open');backdrop.classList.remove('open')}
music.onclick=()=>openSidebar(); new.onclick=()=>{chat.dataset.session='new'}; session.onclick=()=>{chat.dataset.session='session'};
config.onclick=openCommand; document.querySelector('#sidebar-close').onclick=closeSidebar; document.querySelector('#chat-nav').onclick=closeSidebar;
document.querySelector('#command-close').onclick=closeCommand; backdrop.onclick=closeCommand;
</script>
"""


@contextmanager
def browser():
    with pw_api.sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            yield pw, chromium
        finally:
            chromium.close()


@pytest.mark.parametrize("width", [320, 360, 393])
def test_mobile_geometry_keyboard_safe_area(width):
    with browser() as (_, chromium):
        context = chromium.new_context(viewport={"width": width, "height": 640}, is_mobile=True, has_touch=True)
        page = context.new_page(); page.set_content(FIXTURE)
        page.locator("#msg").focus(); page.locator("#msg").fill("keyboard")
        send = page.locator("#send").bounding_box()
        assert send and send["width"] >= 44 and send["height"] >= 44
        assert page.locator("#composer").evaluate("e => getComputedStyle(e).paddingBottom").endswith("px")
        page.set_viewport_size({"width": width, "height": 420})
        send = page.locator("#send").bounding_box()
        assert send and send["y"] >= 0 and send["y"] + send["height"] <= 420
        context.close()


def test_music_to_chat_new_session_and_command_center_controls():
    with browser() as (_, chromium):
        page = chromium.new_page(); page.set_content(FIXTURE)
        page.locator("#music").click(); assert page.locator("#sidebar").is_visible()
        page.locator("#chat-nav").click(); assert not page.locator("#sidebar").is_visible()
        page.locator("#new").click(); assert page.locator("#chat").get_attribute("data-session") == "new"
        page.locator("#session").click(); assert page.locator("#chat").get_attribute("data-session") == "session"
        page.locator("#config").click(); assert page.locator("#command-center").is_visible(); assert page.locator("#backdrop").is_visible()
        for control in ("attach", "prompts", "toolsets"):
            assert page.locator("#" + control).is_visible()
        page.locator("#command-close").click(); assert not page.locator("#command-center").is_visible(); assert not page.locator("#backdrop").is_visible()
        page.close(); chromium.close()


def test_scroll_while_stream_keeps_chat_scrollable():
    with browser() as (_, chromium):
        page = chromium.new_page(viewport={"width": 360, "height": 640}); page.set_content(FIXTURE)
        page.locator("#chat").evaluate("e => e.scrollTop = e.scrollHeight")
        assert page.locator("#chat").evaluate("e => e.scrollTop > 0")
        page.mouse.wheel(0, -250)
        assert page.locator("#chat").evaluate("e => e.scrollTop < e.scrollHeight")
        page.close(); chromium.close()
