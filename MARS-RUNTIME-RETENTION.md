# Mars Runtime Retention Candidates

> Inventory only. No files or services are removed by this document.
> Updated 2026-10-03.

## Keep by default

- `/root/data/music_cache` — active Mars Music data; needs an explicit cache TTL,
  not blind deletion.
- `/root/.hermes/webui/sessions` and state databases — product data.
- active AI tunnels, VPN and proxy services.
- active profile data and MCP configuration used by current workflows.

## Measure before cleanup

- `/root/.browser-profile` (~1.9G): browser cache/profile; determine whether CDP,
  music and login flows share it before pruning.
- `/root/.cache` (~1.7G): separate package/model/browser caches; apply per-subdir TTL.
- `/root/.hermes/patches` (~76M): development rollback artifacts; retain only recent
  patches after confirming no active rollback depends on them.
- `/root/.hermes/logs` and terminal-output: rotate by age and size, preserving recent
  incident evidence.
- `/root/tools/mars_music/*.bak*` and WebUI backup copies: archive outside the source
  tree only after checking rollback references.

## Runtime complexity to verify

- LSP process memory and whether it is required continuously in production.
- MCP child processes attached to the primary WebUI cgroup.
- kernel runner TTL after interrupted agent runs.
- whether office/sergey WebUI profiles need to be always-on or can be on-demand.

## Safety rule

Do not stop services, delete caches, or remove backup copies as part of a code
refactor. First capture ownership, last-access/last-modified evidence and a retention
period; then make cleanup a separate reviewed change.
