# Mars Architecture Map

> Working map for the Mars product, built on the Hermes WebUI base.
> This is a product boundary document, not a claim that every inherited module is removable.
> Updated 2026-10-03.

## Product rule

Mars is the maintained product. Hermes is an implementation base and a source of
selective upstream fixes. Do not merge upstream wholesale. Every adopted upstream
change must be reviewed against Mars custom commits, tested, and recorded separately.

## Mars Core

These areas define the chat product and have the highest compatibility risk:

- `server.py` — HTTP/server bootstrap and request dispatch shell.
- `api/routes.py` — current route orchestration; still a large mixed module and a
  later refactor target, but URL and JSON contracts must remain stable.
- `api/streaming.py` — agent execution, SSE lifecycle, replay/reconnect, reasoning,
  compression and context writeback. Mars fast-path changes live here.
- `api/models.py` — sessions, state-db bridges and message alignment.
- `api/profiles.py` and `api/config.py` — profile-scoped state and runtime configuration.
- `static/messages.js`, `static/ui.js`, `static/sessions.js` — chat rendering,
  streaming UI, virtualized messages and session behavior.

**Policy:** preserve public API, event shapes, session persistence and profile isolation
when extracting code from Core.

## Mars Product Layer

These are user-facing Mars changes layered over Core:

- Mars branding, true-black/terracotta/neon styling and planet assets in `static/`.
- Mobile navigation, activity modes, return-to-chat and panel isolation in
  `static/boot.js`, `static/panels.js`, `static/ui.js` and `static/index.html`.
- Reasoning/anchor-scene UX and fast rendering paths in `static/messages.js`,
  `static/ui.js` and the related server streaming state.
- Music shell integration and mini-player hooks in the WebUI static panels; the
  streaming service itself is `/root/tools/mars_music` and is operationally separate.
- Web Push integration in `api/push.py` and `static/sw.js`.

**Policy:** Mars UX is product behavior. Do not replace it with upstream UI changes
without a visual/mobile regression check.

## Mars Integrations

These are lower-level product integrations with narrower boundaries:

- `api/kanban_bridge.py` — projects/Kanban bridge to the agent database.
- `api/kanban_route_helpers.py` — stale-client diagnostics for unmatched Kanban routes.
- `api/dashboard_probe.py` — dashboard status probe.
- `api/gateway_chat.py`, `api/gateway_watcher.py`, `api/gateway_restart.py` — gateway/messaging integration.
- `api/office_documents.py` and office profile service — office workflow integration.
- MCP and OSINT/retail processes — external tools, not WebUI Core.

**Policy:** integrations are candidates for separate lifecycle and route modules, but
first verify active callers and profile usage.

## Inherited Hermes Base

The following remain useful infrastructure but are not Mars product goals by default:

- onboarding/update compatibility;
- generic auth/passkeys/OIDC;
- workspace/git/worktree helpers;
- generic extensions/plugins/skills/cron plumbing;
- generic upload/media/share helpers;
- agent compatibility adapters and state-db bridges.

Inherited code is retained while Mars uses it. It becomes a removal candidate only
when usage is demonstrated absent and the replacement path is tested.

## First completed structural slice

`api/message_deduplication.py` now owns pure message identity and context
de-duplication algorithms. `api/streaming.py` retains compatibility wrappers and
Mars-specific policy callbacks. This keeps compression markers, workspace prefixes,
provider replay sidecars and empty partial identities unchanged while reducing the
streaming monolith.

## Second completed structural slice

`api/route_gateway_helpers.py` now owns the messaging identity cache and the
profile-scoped `hermes gateway` CLI runner. `api/routes.py` keeps
`/api/gateway/status` and `/api/gateway/{start,stop,restart}` handlers plus the
status payload so URL/JSON contracts and existing monkeypatch tests stay stable.

## Third and fourth completed structural slices

`api/project_os_helpers.py` now owns the pure Project OS helpers: workspace
read/json, truth board slugs, repo matching/resolution, goal summary and
onboarding context. The `_handle_project_os_dashboard` HTTP handler stays in
`api/routes.py`, which re-exports the helpers with identity preserved.

`api/plugin_visibility_helpers.py` now owns the #539 plugin visibility
constants, sanitizing/categorization helpers and the payload builder
(`build_plugin_visibility_payload` with an injected `manager_getter`).
`api/routes.py` keeps `_get_plugin_manager_for_visibility` and a thin
`_plugin_visibility_payload` wrapper because `tests/test_plugins_panel.py`
patches the getter on the routes module.

Both slices keep URL/JSON contracts unchanged; `api/routes.py` shrank from
29,468 to 29,162 lines.

## Next candidates

1. Extract further narrow helpers from `api/routes.py` (~29.2k lines) one
   limited diff at a time, without changing endpoints or payloads. Leave HTTP
   handlers and monkeypatch targets (functions patched as
   `api.routes.<name>`) in `routes.py`; inject them into extracted builders at
   call time when needed.
2. Separate reasoning/anchor-scene rendering from `static/messages.js` only after
   current mobile and streaming tests are mapped.
3. Treat profiles and streaming lifecycle as late refactor targets.
