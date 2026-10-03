"""Read-only system and provider GET route cluster."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs
from api.route_wiki import handle_wiki_get


def handle_readonly_get(handler, parsed, *, deps):
    """Handle read-only wiki, health, model, dashboard and provider routes.

    Dependencies are supplied at call time so routes.py monkeypatch targets
    remain authoritative for tests and profile-scoped request behavior.
    """
    _LLM_WIKI_MAX_PAGE_BYTES = deps['_LLM_WIKI_MAX_PAGE_BYTES']
    _handle_health = deps['_handle_health']
    _handle_live_models = deps['_handle_live_models']
    _handle_llm_wiki_status = deps['_handle_llm_wiki_status']
    _handle_logs = deps['_handle_logs']
    _handle_plugins = deps['_handle_plugins']
    _llm_wiki_allowlisted_entries = deps['_llm_wiki_allowlisted_entries']
    _llm_wiki_resolve_path = deps['_llm_wiki_resolve_path']
    _skill_path_within = deps['_skill_path_within']
    bad = deps['bad']
    build_agent_health_payload = deps['build_agent_health_payload']
    build_system_health_payload = deps['build_system_health_payload']
    gateway_chat_config_status = deps['gateway_chat_config_status']
    get_available_models = deps['get_available_models']
    get_available_models_for_session_visit = deps['get_available_models_for_session_visit']
    get_provider_cost_history = deps['get_provider_cost_history']
    get_provider_quota = deps['get_provider_quota']
    get_providers = deps['get_providers']
    j = deps['j']
    logger = deps['logger']
    profile_env_for_active_request = deps['profile_env_for_active_request']
    profile_env_for_active_request_readonly = deps['profile_env_for_active_request_readonly']
    RequestDiagnostics = deps['RequestDiagnostics']

    _wiki_result = handle_wiki_get(
        handler,
        parsed,
        deps={
            "_LLM_WIKI_MAX_PAGE_BYTES": _LLM_WIKI_MAX_PAGE_BYTES,
            "_handle_llm_wiki_status": _handle_llm_wiki_status,
            "_llm_wiki_allowlisted_entries": _llm_wiki_allowlisted_entries,
            "_llm_wiki_resolve_path": _llm_wiki_resolve_path,
            "_skill_path_within": _skill_path_within,
            "bad": bad,
            "j": j,
        },
    )
    if _wiki_result is not None:
        return _wiki_result
    if parsed.path == "/api/logs":
        return _handle_logs(handler, parsed)

    if parsed.path == "/health":
        return _handle_health(handler, parsed)

    if parsed.path == "/api/health/agent":
        payload = build_agent_health_payload()
        payload["gateway_chat"] = gateway_chat_config_status()
        j(handler, payload)
        return True

    if parsed.path == "/api/system/health":
        j(handler, build_system_health_payload())
        return True

    if parsed.path == "/api/models":
        # Profile-scoping for non-default profiles (#3957) is handled INSIDE
        # get_available_models() — it binds the active profile's env + TLS on
        # the detached rebuild worker (and the legacy synchronous rebuild),
        # which the request-thread wrapper could not reach. See
        # api.config.get_available_models cold path + profile_scope_for_detached_worker.
        freshness = parse_qs(parsed.query or "").get("freshness", [""])[0].strip().lower()
        diag = RequestDiagnostics.maybe_start("GET", parsed.path, logger=logger, print_fn=getattr(handler, '_safe_webui_print', None))
        try:
            diag.stage(f"enter:freshness={freshness or 'default'}") if diag else None
            if freshness == "session_visit":
                result = get_available_models_for_session_visit()
                diag.stage("response_serialize") if diag else None
                return j(handler, result)
            if freshness:
                return bad(handler, f"unknown models freshness: {freshness}", status=400)
            return j(handler, get_available_models())
        finally:
            if diag:
                diag.finish()

    if parsed.path == "/api/models/live":
        with profile_env_for_active_request("/api/models/live", logger_override=logger):
            return _handle_live_models(handler, parsed)

    # ── Auxiliary models (GET/POST) ──
    if parsed.path == "/api/model/auxiliary":
        from api.config import get_auxiliary_models
        return j(handler, get_auxiliary_models())

    if parsed.path == "/api/dashboard/status":
        from api import dashboard_probe

        j(handler, dashboard_probe.get_dashboard_status())
        return True

    if parsed.path == "/api/dashboard/config":
        from api import dashboard_probe

        try:
            j(handler, dashboard_probe.get_dashboard_config())
        except ValueError as exc:
            bad(handler, str(exc), status=400)
        return True

    # ── Providers (GET) ──
    if parsed.path == "/api/providers":
        # Apply the active per-request profile's env so provider auth probes
        # resolve against that profile's credentials, not the process-default
        # profile's (#3957). Without this, get_auth_status() probes on a
        # non-default profile resolve the wrong/empty creds and can stall past
        # the 30s frontend timeout. No-op for the default profile.
        with profile_env_for_active_request_readonly("/api/providers", logger_override=logger):
            return j(handler, get_providers())

    # ── Plugins/hooks visibility (read-only, no callback/source internals) ──
    if parsed.path == "/api/plugins":
        return _handle_plugins(handler, parsed)
    if parsed.path == "/api/provider/quota":
        query = parse_qs(parsed.query)
        provider_id = (query.get("provider", [""])[0] or None)
        refresh = (query.get("refresh", [""])[0] or "").strip().lower() in {"1", "true", "yes", "on"}
        # Bind the active request's profile env (matches /api/providers and
        # /api/models/live). #4365 added a credential_pool.load_pool() path in
        # get_provider_quota for all pooled providers; without this wrapper that
        # read/write runs under the process-default profile, so a multi-profile
        # client would see (and seed) the default profile's pool instead of its
        # own (#4247/#4067 profile-isolation class).
        with profile_env_for_active_request_readonly("/api/provider/quota", logger_override=logger):
            return j(handler, get_provider_quota(provider_id, refresh=refresh))

    if parsed.path == "/api/provider/cost-history":
        query = parse_qs(parsed.query)
        provider_id = (query.get("provider", [""])[0] or None)
        days_raw = (query.get("days", ["7"])[0] or "7").strip()
        try:
            days = max(1, min(int(days_raw), 365))
        except (ValueError, TypeError):
            days = 7
        return j(handler, get_provider_cost_history(provider_id, days))

    return None
