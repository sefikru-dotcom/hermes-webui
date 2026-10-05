# Mobile chat UX contract

This contract is the acceptance matrix for the mobile stability shard. It
covers the existing production shell and does not change message, locale, or
i18n contracts.

## Acceptance matrix

| Area | 320 px | 360 px | 393 px | Evidence |
|---|---|---|---|---|
| Keyboard / short viewport | Focused composer remains inside viewport after height reduction | Same | Same | Playwright production shell |
| Safe area | Titlebar/composer use safe-area variables without changing touch target size | Same | Same | CSS inspection + Chromium shell run |
| Chat scroll | `.messages-inner` must not create an accidental inner vertical scroller; transcript remains scrollable while streaming | Same | Same | mobile layout shard + shell test |
| Music → chat | Music/sidebar navigation opens mobile drawer; selecting Chat returns to chat | Same | Same | production shell Chromium smoke |
| New / session | New and Session preserve their respective navigation/session actions | Same | Same | production shell test; mocked API |
| Command center | Attach, Prompts, and Toolsets are reachable and meet target dimensions | Same | Same | production shell Chromium smoke |
| Command center close | Escape clears mobile command-center open and accessibility state | Same | Same | production shell Chromium smoke |
| Sidebar overlay | Full-screen sidebar does not require the generic panel backdrop; closing sidebar removes sidebar state only | Same | Same | `static/boot.js` review + layout tests |
| Other panels | Right workspace panel and command center retain their own backdrop/close behavior; sidebar close must not hide those backdrops | Same | Same | panel tests + implementation review |
| Streaming + scroll | User can scroll existing transcript while stream content is appended | Same | Same | shell scroll structure only; live SSE not exercised |

## Overlay ownership

The mobile overlay is not a global “close every panel” switch. The full-screen
sidebar is a drawer state (`.sidebar.mobile-open`) and has its own close path.
Workspace and command-center surfaces own their backdrop state. A sidebar close
must not remove a backdrop belonging to another panel; a panel close must not
leave the full-screen sidebar open.

## Test boundary

`tests/test_mobile_browser_smoke.py` is an executable Playwright run against the
checked-in production `static/index.html` and its real CSS/JS. A localhost
harness serves those assets unchanged; only API responses and the music iframe
are deterministic mocked data. It proves Chromium layout and interaction
wiring for 320/360/393 widths, keyboard-like focus/short viewport behavior,
touch targets, overflow, navigation, command-center controls, close behavior,
and scrolling. This is not live API/session or SSE verification, device IME
behavior, or production browser/device rendering.

`tests/test_mobile_runtime_acceptance.py` is the existing opt-in real-browser
harness. It runs only with `MARS_MOBILE_SMOKE_URL` pointed at an isolated test
server; it must never target production. No live runtime or production server
was used for this smoke.

## Scope guard

Allowed implementation files for this shard are `static/index.html`,
`static/style.css`, `static/boot.js`, and panel modules plus tests/docs. Do not
modify `ui/`, `messages/`, or `i18n/`.
