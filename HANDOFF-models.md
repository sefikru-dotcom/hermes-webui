# HANDOFF — models zone (provider-aware model identity + reasoning runtime)

Worktree: `/root/workspace/webui-upgrade-20261022/models` (base `ebb0f0da6`, branch
`upgrade-20261022-models`).

Files owned/edited: `api/config.py`, `api/routes.py`, `api/streaming.py`, new
`api/model_identity.py`. New tests: `tests/test_upgrade_models_identity.py`,
`tests/test_upgrade_models_reasoning.py`, `tests/test_upgrade_models_runtime.py`.

Not touched: `static/**` (frontend attribution is the coordinator's job — see §6),
live `config.yaml`, live sessions, `:kiro` allow-listing, deploy/restart.

---

## 1. The one idea

A model is **(model id, provider)**, not a string. `api/model_identity.py` is now the
only place that decides what that pair means:

```python
import api.model_identity as mi
mi.split_model_identity("@openai:gpt-5", None)      # ModelIdentity("gpt-5", "openai")
mi.split_model_identity("openai/gpt-5", "openrouter")  # slash prefix opt-in only
mi.identities_match("gpt-5", "anthropic", "gpt-5", "openai")   # False
mi.reasoning_pin_key("gpt-5", "openai")             # "@openai:gpt-5"
mi.resolve_reasoning_effort(...)                    # session > pin > global
```

Rules encoded there (all tested):

- the id itself is **never rewritten**: `vendor/model`, `:free`, `:kiro`, `gpt-5.4` all
  survive verbatim. Only whitespace is trimmed.
- `@provider:model` splits on the first colon for a known provider, on the last for
  named custom providers, and never inside a `custom:host:port` slug.
- provider aliases (`claude`→`anthropic`, `google`→`gemini`, `glm`→`zai`, …) compare
  equal, so a spelling difference never reads as a model switch.
- an unknown provider on either side degrades to a bare-model comparison (the legacy
  shape), so old sessions do not look "changed" on reload.

## 2. Write boundaries — empty ids are refused

`POST /api/session/new`, `POST /api/session/update`, `POST /api/chat/start`,
`POST /api/chat/goal` (kickoff), `POST /api/default-model`, `POST /api/model/set`.

Accepted: `vendor/model`, `qwen3:free`, `:kiro` (identity is legal — whether a provider
serves it is a catalog question, never decided here), `@provider:model`,
`@custom:host:port:model`, `@custom:<named>:model`.

Refused with **400** (`error` + `field` + `code`):

| input | `code` |
|---|---|
| `""`, `"   "` | `model_empty` |
| `"@custom:plusvibe-api:"` (empty tail) | `model_empty_tail` |
| `123`, `{}`, `[]` | `model_not_a_string` |
| `"gpt-5\n"` / control chars | `model_control_chars` |

`None` still means "not mentioned" and is legal (a new session inherits the profile
default). An auxiliary slot still accepts `""` = "auto" — that path keeps its own
message (`provider-qualified auxiliary model must …`).

Blank `model` is **not** silently turned into `"auto"`: the empty default
(`DEFAULT_MODEL = ""`) is unchanged behaviour, only client-supplied junk is rejected.

## 3. Reasoning: writer / status / runtime now agree

Priority is **session > per-model pin > global**, resolved once in
`mi.resolve_reasoning_effort` and used by all three call sites:

| surface | function |
|---|---|
| pin writer | `api.config.set_model_reasoning_override(effort, model_id=…, provider_id=…)` |
| status | `api.config.get_reasoning_status(model_id=…, provider_id=…)` |
| runtime | `api.streaming._resolve_turn_reasoning_effort(cfg, session, model, provider)` |

- The writer stores a **provider-qualified** key (`@openai:gpt-5`) when it knows the
  provider, so `gpt-5` on OpenAI and on Anthropic keep independent levels.
- Legacy bare keys (`gpt-5`, written by older builds and by the CLI) are still read by
  every provider — they are the any-provider fallback and are never deleted by a
  qualified write.
- Lookup is spelling-tolerant within one lane (`gpt-5.4` ≡ `gpt-5-4`), and
  `GET /api/reasoning` accepts the qualified form the picker sends
  (`@openai:gpt-5` → lane `gpt-5`/`openai`).

`GET /api/reasoning` gained read-only diagnostics (all additive):

```json
{
  "model": "gpt-5", "provider": "openai",
  "requested_model": "@openai:gpt-5", "requested_provider": "openai",
  "reasoning_effort": "high", "source": "model",
  "reasoning_pin_key": "@openai:gpt-5",
  "reasoning_pin_candidates": ["@openai:gpt-5", "gpt-5"],
  "reasoning_pin_effort": "high",
  "global_reasoning_effort": "low", "session_reasoning_effort": null
}
```

`source` stays `session | model | default` — unchanged, safe for the existing chip.

## 4. Context metadata from a superseded turn

A turn can be overtaken while it streams (`/api/session/update` from another tab).
When the session's CURRENT `(model, provider)` no longer matches the lane that turn
ran on, the turn **no longer writes** `context_length`, `threshold_tokens` or
`last_prompt_tokens` onto the session. It logs once:

```
[webui] context metadata write skipped for stream <id>: session now 'gpt-5'/'anthropic', turn ran 'gpt-5'/'openai'
```

Blank session model (no identity yet) stays writable; a comparison failure falls back
to the old write behaviour, so turn completion can never break on this.

## 5. Error payloads carry identity

Provider errors (both the mid-stream `apperror` and the terminal one) now include:

```json
{
  "message": "…", "type": "auth_mismatch", "hint": "…",
  "model": "gpt-5", "provider": "openai",
  "model_identity": {
    "model": "gpt-5", "provider": "openai",
    "requested_model": "@anthropic:gpt-5", "requested_provider": "anthropic",
    "reasoning_effort": "high", "reasoning_source": "model",
    "reasoning_pin_key": "@openai:gpt-5",
    "fallback": "activated",
    "model_substituted": true, "provider_substituted": true
  }
}
```

`model`/`provider` at top level are flat mirrors of the effective lane (additive, for
log greps and old clients). `fallback` is `"activated"` only when the agent actually
switched lanes. Nothing new is persisted and nothing secret is included — model ids and
provider slugs are already public in the picker.

## 6. For the JS integrator (what is left on the frontend)

Backend now tells the truth; these are the client-side follow-ups, all optional and
additive:

1. **Reasoning chip label** — use `provider`/`model` from `GET /api/reasoning` instead
   of the raw picker value so `@openai:gpt-5` renders as `gpt-5 · OpenAI`.
2. **Pin write** — keep sending `provider` in `POST /api/reasoning`
   (`{scope:"model", model, provider, effort}`); it is now honoured. Sending it is
   what makes two providers with the same model id keep two levels.
3. **400 handling** — on a model-identity 400 the body is
   `{error, field, code, model?}`; `code` is one of `model_empty`,
   `model_empty_tail`, `model_not_a_string`, `model_control_chars`. Show `error`
   (already human-readable) and mark the model control, not the whole form.
4. **Error attribution** — prefer `payload.model_identity.model/provider` over the
   picker's current value when labelling a failed turn, and surface
   `model_substituted`/`provider_substituted` as "answered by <fallback lane>".
5. Nothing in `static/**` was edited by this zone; `static/ui.js` line ~4508 already
   sends `provider`, so pin parity works without a JS change.

## 7. Checks run (this worktree)

```
HOME=$(mktemp -d) HERMES_HOME=$(mktemp -d) PYTHONPATH=. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  /opt/hermes-webui/.venv/bin/python3 -m pytest \
  tests/test_upgrade_models_identity.py tests/test_upgrade_models_reasoning.py \
  tests/test_upgrade_models_runtime.py -q            # 85 passed
```

Neighbouring regression sweep (same command):

```
tests/test_issue3256_context_length_default_only_guard.py tests/test_issue3660_context_window_stale.py
tests/test_pr1341_context_window_persistence.py tests/test_pr1318_context_length_fallback.py
tests/test_issue3717_context_length_provider_overrides.py tests/test_issue1896_context_length_fallback_args.py
tests/test_chat_start_provider_fallback.py tests/test_issue2565_reasoning_accumulation.py
tests/test_issue3958_reasoning_post_session_context.py tests/test_auxiliary_model_provider_prefix.py
tests/test_auxiliary_models_settings.py
  → all passed except one pre-existing failure (below)
```

Pre-existing at base `ebb0f0da6` (verified with `git show HEAD:…`, not caused here,
not fixed here):

- `tests/test_issue1771_session_model_switch_sync.py` — 6 failed (`node driver failed`,
  no `node` in this sandbox).
- `tests/test_custom_provider_bare_model_reasoning.py` — 4 failed
  (`test_custom_nested_gemini_pre_2_5_routes_exclude_reasoning`).
- `tests/test_auxiliary_models_settings.py::test_model_auxiliary_route_exists` — asserts
  `"/api/model/auxiliary"` in `api/routes.py`, but the route lives in
  `api/route_readonly.py` at base too. Test is stale, not the code.

## 8. Known limits

- `:kiro` is accepted as an **identity**, not endorsed as a servable model. Whether a
  provider serves it is only answerable from that provider's catalog; nothing here
  caches or invents that answer.
- A legacy bare pin still applies to every provider (it carries no provider info).
  Writing a qualified pin does not migrate or delete it — no live-config migration was
  done, by contract.
- `_context_metadata_identity_guard` compares against the session object at turn end;
  a model switch that lands *after* the write is not undone (the write already
  belonged to the correct lane at that moment).
- Frontend attribution (chip label, 400 field highlighting, fallback badge) is
  intentionally left to the coordinator — backend fields are in place (§6).
