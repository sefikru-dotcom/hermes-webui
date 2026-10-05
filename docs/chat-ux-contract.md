# Mobile chat UX contract

This contract is the acceptance matrix for the mobile stability shard. It
covers the existing three-panel chat shape and does not change message,
locale, or i18n contracts.

## Acceptance matrix

| Area | 320 px | 360 px | 393 px | Evidence |
|---|---|---|---|---|
| Composer controls | Send/config controls remain at least 44×44; compact gutters may reduce spacing, never targets | Same | Same | `tests/test_mobile_layout.py`; Playwright fixture smoke |
| Keyboard / short viewport | Focused composer remains inside viewport after height reduction | Same | Same | Playwright fixture smoke; isolated runtime smoke when `MARS_MOBILE_SMOKE_URL` is set |
| Safe area | Titlebar/composer use safe-area variables without changing touch target size | Same | Same | CSS inspection + Playwright fixture |
| Chat scroll | `.messages-inner` must not create an accidental inner vertical scroller; transcript remains scrollable while streaming | Same | Same | mobile layout shard + browser smoke |
| Music → chat | Music/sidebar navigation opens a full-screen mobile sidebar and Chat closes it | Same | Same | browser smoke; runtime/manual evidence required for live server |
| New / session | New and Session preserve their respective navigation/session actions | Same | Same | browser smoke fixture; live runtime is separate |
| Command center | Attach, Prompts, and Toolsets are reachable from the mobile command center | Same | Same | browser smoke fixture; runtime smoke where available |
| Command center close | Close removes command center and its backdrop | Same | Same | browser smoke |
| Sidebar overlay | Full-screen sidebar does not require the generic panel backdrop; closing sidebar removes sidebar state only | Same | Same | `static/boot.js` review + layout tests |
| Other panels | Right workspace panel and command center retain their own backdrop/close behavior; sidebar close must not hide those backdrops | Same | Same | browser fixture + panel runtime tests |
| Streaming + scroll | User can scroll existing transcript while stream content is appended | Same | Same | browser smoke fixture; live SSE not exercised |

## Overlay ownership

The mobile overlay is not a global “close every panel” switch. The full-screen
sidebar is a drawer state (`.sidebar.mobile-open`) and has its own close path.
Workspace and command-center surfaces own their backdrop state. A sidebar close
must not remove a backdrop belonging to another panel; a panel close must not
leave the full-screen sidebar open.

## Test boundary

`tests/test_mobile_browser_smoke.py` is an executable Playwright run against an
offline deterministic DOM fixture. It proves Chromium layout and interaction
wiring for 320/360/393 widths, keyboard-like focus/short viewport behavior,
safe-area CSS application, navigation, command-center controls, close behavior,
and scrolling. It does **not** prove live API/session creation, real SSE
streaming, device IME behavior, or production browser/device rendering.

`tests/test_mobile_runtime_acceptance.py` is the existing opt-in real-browser
harness. It runs only with `MARS_MOBILE_SMOKE_URL` pointed at an isolated test
server; it must never target production. Its results are reported separately
from the offline fixture results.

## Scope guard

Allowed implementation files for this shard are `static/index.html`,
`static/style.css`, `static/boot.js`, and panel modules plus tests/docs. Do not
modify `ui/`, `messages/`, or `i18n/`.
