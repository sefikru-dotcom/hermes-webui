# Mars Architecture Map

> Working map for the Mars product, built on the Hermes WebUI base.
> This is a product boundary document, not a claim that every inherited module is removable.
> Updated 2026-10-04.

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
- `api/login_locale.py` — pure locale-tag resolution for the login page.
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

## Sixth structural slice

The read-only LLM Wiki status, browse, and page handlers now live in the
dedicated api/route_wiki.py module. The read-only dispatcher passes route-level
dependencies at call time, preserving existing monkeypatch seams for wiki path
resolution and allowlisted entries. URL contracts and TOCTOU protections are
unchanged. The focused wiki/read-only regression set passed 46/46.

## Seventh structural slice

The read-only /api/logs handler now lives in api/route_logs.py. The legacy
api.routes._handle_logs facade remains and injects the active-profile home,
response helpers, and logger at call time, preserving the endpoint contract and
existing route-level seams. The focused read-only regression set passed 58/58.

## Eighth structural slice

The /api/insights read-only analytics aggregation now lives in
api/route_insights.py. api.routes._handle_insights remains as a compatibility
facade and injects the session directory, response helper, and logger at call
time; state-db resolution remains runtime-patchable through api.models. The
payload contract is unchanged. Focused insights/cache/parameter-validation
tests passed 28/28.

## Refactoring rules and remaining boundaries

- `api/routes.py` remains the entry dispatcher and compatibility facade. New
  implementations own one domain, not a copied route namespace.
- Inject route-owned functions, policy constants, caches, and locks explicitly at
  call time. Do not synchronize module globals or capture active-profile values at
  import time. Existing `api.routes.<name>` monkeypatch seams remain supported.
- Preserve state ownership: moving a function must not duplicate its cache or
  lock, reset a limiter, or change where the active profile is resolved.
- Source-location checks must follow the actual implementation or become behavior
  tests; do not add placeholder source markers to make a check pass.
- Profiles, streaming lifecycle, session hydration, and anchor-scene reconciliation
  remain later refactor targets. Do not combine structural extraction with runtime
  cleanup or upstream merges.

## Fifth structural slice

api/route_readonly.py now owns the read-only system/provider GET cluster:
Wiki browse/page/status, logs and health, model and live-model reads, dashboard
status/config, providers, plugins, provider quota and cost history.
api/routes.py remains the public handle_get dispatcher and supplies all
dependencies at call time, preserving profile scoping and monkeypatch targets.
Static route-audit markers remain in routes.py without duplicating handlers.
The extraction reduced api/routes.py by 155 lines; focused regression suites
passed 125/125.

Supporting pure modules also include api/media_headers.py and
api/origin_helpers.py, each re-exported from api.routes for compatibility.

## Ninth structural slice

Pure cron output formatting now lives in `api/route_cron_output.py`: response-window
truncation, response snippets, and usage metadata parsing. `api/routes.py` keeps
compatibility wrappers for the existing helper names and route-level tests. Cron
execution, profile context, file validation, and endpoint payloads remain unchanged.
Focused route/source checks passed 15/15; no deploy or restart was performed.

## Tenth structural slice

Dashboard plugin discovery and opt-in settings checks now live in
`api/route_dashboard_plugins.py`. `api/routes.py` keeps compatibility wrappers
and the existing route-level fallback behavior; plugin visibility payloads,
HTTP routing, and server-side enable gates remain unchanged. Focused plugin
helper tests passed 4/4. The broader shard still has two pre-existing static
failures unrelated to this slice (index text and system-health source marker).



## Eleventh structural slice

The POST `/api/tts` handler now lives in `api/route_tts.py`, covering Edge,
ElevenLabs and OpenAI speech synthesis without changing request, error or audio
response contracts. `api.routes._handle_tts` remains the facade and owns its
`_tts_limiter` attribute; body/prosody, network opener, buffering and URL-validation
seams are injected at call time. Auth, configuration and profile imports remain
lazy, and pinned-address/no-redirect protections stay in the existing helpers.
The extraction reduces `api/routes.py` by 303 lines (28,477 to 28,174).

All 59 original focused tests pass. Three additional behavioral tests cover
facade-owned limiter persistence/reset, current body/prosody dependencies and
auth rejection before limiter creation. The broader speech/media/upload and
model-picker verification covers 244 tests. Handler AST equivalence and Ruff
checks pass.

## Release compatibility checks — 2026-10-04

All three model-dropdown row paths tolerate an unavailable optional Mars
reasoning decorator. Existing test expectations were aligned with the already
shipped Mars UI: Edge is the settings-panel TTS fallback; reasoning labels are
`Off` and `Dynamic` rather than inherited `None` and `Default`. The reasoning
label test executes the actual formatter, including aliases, instead of matching
source strings. No product defaults or labels were changed by this release.

## Wiki discovery and status domain — 2026-10-04

`api/route_wiki_helpers.py` owns Wiki location discovery, environment/config
precedence, bounded filesystem walks, cached page allowlists, inode-checked status
files, last-writer detection, and the private-safe status payload. The existing
`api/route_wiki.py` owns read-only HTTP browse/page dispatch.

`api/routes.py` preserves helper names and passes current policy constants,
scanner callbacks, and the route-owned allowlist cache/lock on every call. TTL
changes, active-profile changes, and test substitutions continue to take effect.
All 16 original helper bodies remain AST-equivalent. Tests now execute the walk
limit and forbidden-root guards instead of depending on their location in a
monolithic source file. Four additional compatibility tests cover resolver
precedence, cache ownership/current scanners, status dependencies, and allowlists.

## Memory and project context domain — 2026-10-04

`api/route_memory.py` owns profile memory read/write and project context discovery,
frontmatter removal, source priority, shadowed-file metadata, and context limits.
It does not own profile switching or session lifecycle. Active profile lookup
remains lazy; request-time configuration snapshots, trusted workspace resolution,
redaction, and response helpers come through the existing route facades.

All seven original function bodies remain AST-equivalent. Four additional tests
cover profile changes between calls, changed configuration permissions, trusted
workspace resolution, and current context scanner/limit callbacks. Existing tests
retain symlink-write protection, read-only errors, secret redaction, non-git walk
boundaries, and cross-profile configuration isolation.

## Domain navigation

| Work area | Implementation | Stable entry surface |
|---|---|---|
| Wiki location, scans, cache, status | `api/route_wiki_helpers.py` | `api.routes._llm_wiki_*`, `_build_llm_wiki_status` |
| Wiki browse/page HTTP | `api/route_wiki.py` | `api.routes.handle_get`, `api/route_readonly.py` |
| Memory read/write and project context | `api/route_memory.py` | `api.routes._handle_memory_*`, `_project_context_*` |
| Speech synthesis | `api/route_tts.py` | `api.routes._handle_tts` |

## Workspace Git domain — 2026-10-04

`api/route_git.py` owns all 21 workspace Git route helpers and handlers, including
session/workspace lookup, destructive-action and active-stream guards, Git error
sanitization, stage/unstage/discard/commit/checkout/remote requests, and LLM commit
message generation. `api/workspace_git.py` remains the lower-level Git engine.
HTTP dispatch and route-level monkeypatch names remain in `api/routes.py`.
The active-stream lookup and Agent runtime check retain their original lazy
imports. No Git subprocess semantics, permissions, or payloads changed.

All 21 original function bodies are AST-equivalent; 98 original Git and workspace
panel tests pass, plus four new call-time seam tests. Agent-runtime tests still
exercise both commit-message routes; inherited English restart-text assertions
fail against Mars's already shipped Russian diagnostics before and after this
refactor, and were not weakened.

## Skills domain — 2026-10-04

`api/route_skills.py` owns all 20 helpers for profile-scoped skills discovery,
category resolution, disabled-state normalization, lookup, views/linked files,
save/delete/toggle, and names-list normalization. Agent skill-utils imports remain
lazy. `_skill_path_within` is re-exported with function identity preserved for
Wiki callers. Config path, loaders, reload callback, config lock and stats cache
remain current at call time through the route facade.

All 20 original bodies are AST-equivalent. Additional tests cover per-call
profile config, bounded missing-skill listings, the route-owned lock/cache and
reload outside the lock, and containment function identity. Agent-dependent tests
are reported as skipped where the isolated test runner cannot discover the Agent.

## Files and anchored responses — 2026-10-04

`api/route_files.py` owns 27 file/folder/escape-access and workspace registration
helpers/handlers. It covers directory listing, escape authorization, file reads
and mutations, office saves, ZIP collection/download, path/reveal/VS Code actions,
and workspace add/remove/rename/reorder. It does not own session creation,
profile switching, media-access policy, TTS, or the HTTP method dispatchers.

`api/file_response_helpers.py` owns eight lower-level file response helpers:
anchored FD opening/closure, immutable ETag snapshots, byte/range responses,
HTML base-target injection/preview, and bounded anchored reads. Existing caps,
MIME policy, sanitization, CSRF and anchored filesystem functions come through
call-time facade dependencies; no duplicate caches/locks or namespace copying.

All 35 original bodies are AST-equivalent. Existing security/source audits now
inspect the real modules while route-registration checks stay on `api/routes.py`.
Five new behavior tests cover anchored saves, raw-route resolver/sender seams,
current ETag cap/digest, workspace storage callbacks, and ZIP limits/symlink escape
exclusion. The focused files/Git/skills seam gate passes 329 tests.

## Twelfth structural slice — anchor activity scenes

api/anchor_activity_scene.py now owns anchor-activity scene normalization, message/content
row projection, tool-row matching/merging, live-row settlement, and persisted-scene hydration.
The HTTP persistence handler, session materialization, profile visibility guard, and message-ref
facade remain in api/routes.py; the facade injects the route-owned message-ref callback at
call time. This keeps scene behavior isolated without moving session lifecycle or security policy.

The scene module preserves the existing payload and row contracts. Its policy limits and digest
implementation are local to the domain; no caches, profile state, or namespace copies are
created at import time.

The 2026-10-04 combined structural pass moves 99 original functions into six
cohesive implementation modules. `api/routes.py` shrinks from 28,174 to 26,260
lines (1,914 fewer; about 6.8%). Compatible wrappers intentionally remain: callers
and monkeypatch tests do not need to migrate at the same time as implementations.
The result is navigable domain boundaries, not a performance or memory-use claim.

Additional domain entry points:

| Work area | Implementation | Stable entry surface |
|---|---|---|
| Workspace Git | `api/route_git.py` | `api.routes._git_*`, `_handle_git_*` |
| Skills discovery/views/mutations | `api/route_skills.py` | Existing skill helper/handler names in `api.routes` |
| File/folder/escape/workspace operations | `api/route_files.py` | Existing file/workspace handler names in `api.routes` |
| Anchored reads, ETag/ranges, HTML preview | `api/file_response_helpers.py` | Existing file response helper names in `api.routes` |

## Combined verification — 2026-10-04

Detailed scope, cleanup and the exact original failure list:
`MARS-RESTRUCTURE-20261004.md`.

The large comparison covered 3,248 final tests: 3,116 passed, 74 failed and 58
skipped. The same 74 node IDs fail on baseline `8fdcebfc` (3,100 passed, 74 failed,
58 skipped). There are zero new failure node IDs after source-audit adaptation.
A separate gate deselecting only those 74 confirmed original failures reports
3,116 passed, 58 skipped, 74 deselected. AST equivalence covers all 99 moved
functions; legacy facade signatures/exports and zero `static/` changes were also
verified. All six new modules and five new test modules pass Ruff; the diff-scoped
gate reports zero new violations.

Changes are committed on `mars-restructure-20261004`; production remains on
`8fdcebfc` without a restart or deploy. Worker scratch worktrees and session
artifacts were removed; existing runtime data/backups were not touched.
