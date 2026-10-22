# HANDOFF — model P1 fixes (independent review) + durable fallback attribution

**Commit:** `09c9660d8` — `fix(webui): keep named custom lanes canonical + persist fallback attribution`
**Branch:** `upgrade-20261022-integration`
**Files:** `api/model_identity.py`, `api/config.py`, `api/routes.py`, `api/streaming.py`,
`tests/test_named_custom_model_lane.py` (new), `tests/test_turn_model_attribution_persistence.py` (new),
`tests/test_upgrade_models_identity.py`

## What was wrong (one root cause, three symptoms)

`_resolve_custom_provider_runtime_overrides` rewrites `custom:<slug>` → bare `custom` for SDK
routing. Every identity decision downstream was fed the rewritten value, so a `custom:plusvibe-api`
session compared unequal to its own turn:

| Symptom | Effect |
|---|---|
| context-metadata guard | every write blocked → context ring + auto-compress trigger stale |
| reasoning pin lookup | pin `@custom:plusvibe-api:gpt-6.1-sol` never matched → pinned effort silently lost to global |
| error diagnostics | `provider_substituted: true` on a turn that never left its lane |

## Fixes

1. **Canonical vs transport provider split.** `_turn_provider_identity(requested, resolved)` returns
   the canonical (session-stored) id. Guard, pin lookup, context-length config lookup, gateway
   attribution and both error payloads use it; SDK routing still gets the transport id. The
   credential-heal / refresh paths recompute it. Pre-initialised to `None` near the top of
   `_run_agent_streaming` with a defensive fallback to the transport id.
2. **`allow_slash_prefix` no longer invents a provider.** `google/gemini-3-flash-preview` on the
   `custom:plusvibe-api` lane used to split into provider `google`; an explicit provider is now
   authoritative and the prefix is only inferred when none was sent or when it agrees.
3. **One `custom:<rest>` grammar.** `api.config._custom_slug_rest_looks_like_host_port` delegates to
   `api.model_identity._default_custom_endpoint_slug` (IP / localhost / DNS `host:port` = endpoint,
   anything else = name). All call sites (config, routes, streaming) funnel through it — the
   `@custom:localhost:8080:Qwen3` → model `8080:Qwen3` corruption is gone.
4. **Blank model on `/api/session/update` no longer 400s.** Workspace switch and profile switch send
   the session's own `model: ""`; that legacy shape keeps pre-identity behaviour. Non-string,
   control chars and an empty `@provider:` tail are still refused.
5. **Durable fallback attribution (backend half of `c388bafd7`).** Turn-settle stamps
   `_dm['_turnModelAttribution']` next to `_usedModel`. `build_turn_model_attribution()` fills it
   only from observed state: pre-run requested lane, post-run `agent.model` / `agent.provider`, and
   the agent's own notice (parsed by `parse_fallback_notice` for reason + retry cooldown). Returns
   `None` when nothing proves a fallback. A named-custom → `custom` transport rewrite is **not**
   reported as a provider substitution (`_same_transport_lane`). Writes both the flat keys the
   handoff specified and the nested `requested`/`effective`/`reasoning` lanes the client renderer
   already reads.

## Verification (targeted only, isolated `HOME`)

```
tests/test_upgrade_models_identity.py
tests/test_upgrade_models_reasoning.py
tests/test_upgrade_models_runtime.py
tests/test_named_custom_model_lane.py          (new)
tests/test_turn_model_attribution_persistence.py (new)
→ 113 passed

tests/test_custom_provider_model_identity.py
tests/test_upgrade_attribution.py
→ 10 passed
```

## Remaining gaps (exact, not audited further)

1. **Broad suite not run** (per instruction). One failure surfaced in the aborted broad run and is
   **not from this commit**: `tests/test_5021_boot_model_redirect_budget.py::test_boot_model_401_consumes_budget_when_profile_load_succeeds`
   → `assert 'fallback' == 'resolved'`. Untriaged; likely HOME/config-sensitive. Owner needed.
2. **Reload round-trip not exercised end to end.** Verified: builder output shape, allowlist entry
   `_turnModelAttribution` present in `_SESSION_MESSAGE_DISPLAY_METADATA_KEYS`, client renderer reads
   the same nested lanes. Not verified: a real save → process restart → `GET /api/session` → hydrate
   with the marker intact.
3. **`parse_fallback_notice` is English-shape only.** A localized or reworded agent notice records
   no lanes (by design — no guessing); the marker then carries only the raw `message`. If the agent
   notice format changes, the notice-derived fields silently degrade to message-only.
4. **`retry_seconds` only when the notice carries `retry eligible in ~N s`.** Absent otherwise; the
   client renders without the retry line.
5. **Provider-only fallback within one named endpoint is not observable server-side.** If the agent
   falls back model→model on the same `custom:plusvibe-api` lane, `effective_provider` stays
   canonical and only `effective_model` differs — correct, but a fallback to a *different* named
   custom endpoint is only recorded if the agent's notice names it.
6. **`api/models.py` allowlist + script tag/SW** — handled by the attribution worker (allowlist
   already present at `api/models.py:9014`); not touched here.
