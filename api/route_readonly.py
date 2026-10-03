"""Read-only system and provider GET route cluster."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs


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

    if parsed.path == "/api/wiki/status":
        return _handle_llm_wiki_status(handler, parsed)
    if parsed.path == "/api/wiki/browse":
        wiki_root, _, _ = _llm_wiki_resolve_path()
        if not wiki_root or not os.path.isdir(wiki_root):
            return bad(handler, "Wiki not configured or directory not found", status=404)
        allowlisted_entries = _llm_wiki_allowlisted_entries(Path(wiki_root))
        pages = []
        for rel_path, (fp, identity) in sorted(allowlisted_entries.items(), key=lambda item: item[0].lower()):
            try:
                st = fp.stat()
            except OSError:
                continue
            if (st.st_dev, st.st_ino) != identity:
                continue
            pages.append({"name": Path(rel_path).name, "path": rel_path, "size": st.st_size, "mtime": int(st.st_mtime)})
        return j(handler, {"pages": pages})
    if parsed.path == "/api/wiki/page":
        wiki_root, _, _ = _llm_wiki_resolve_path()
        page_path = parse_qs(parsed.query or "").get("path", [""])[0]
        if not wiki_root or not page_path:
            return bad(handler, "Wiki not configured or path not provided", status=400)
        if "\\" in page_path:
            return bad(handler, "Invalid path", status=400)
        # Reject a real `..` path SEGMENT (or absolute path), not the bare
        # substring — a legitimate listed filename like `v1..v2.md` contains
        # ".." without being traversal. Containment + the resolved-allowlist
        # membership check below are the actual security boundary.
        requested_key = page_path.replace("\\", "/")
        _page_parts = requested_key.split("/")
        if os.path.isabs(page_path) or any(part == ".." for part in _page_parts):
            return bad(handler, "Invalid path", status=400)
        if any(part in ("", ".") for part in _page_parts):
            return bad(handler, "Invalid path", status=400)
        full_path = Path(os.path.join(wiki_root, page_path))
        if not _skill_path_within(Path(wiki_root), full_path):
            return bad(handler, "Invalid path", status=400)
        try:
            wiki_real = Path(wiki_root).resolve()
        except OSError:
            return bad(handler, "Page not found", status=404)
        # Only serve files the browse/list path would surface (same allowlist:
        # *.md under the wiki page-dirs, no dotfiles, forbidden-roots guard).
        # Without this the read endpoint could return ANY file inside the wiki
        # root (e.g. .env / .git/config / non-.md), since containment alone
        # doesn't constrain which files are readable (Opus review finding).
        # Capture each allowlisted page's STABLE IDENTITY (st_dev, st_ino) so the
        # post-open fstat below can detect a file/parent-dir swapped in after the
        # allowlist check (TOCTOU write-race, Codex finding) — a pathname re-open
        # alone can't, since O_NOFOLLOW only guards the final component, not a
        # swapped parent directory.
        allowed_identity = _llm_wiki_allowlisted_entries(wiki_real)
        try:
            resolved_target = full_path.resolve()
        except OSError:
            return bad(handler, "Page not found", status=404)
        requested_entry = allowed_identity.get(requested_key)
        if requested_entry is None:
            return bad(handler, "Page not found", status=404)
        allowlisted_target, allowlisted_identity = requested_entry
        if resolved_target != allowlisted_target:
            return bad(handler, "Page not found", status=404)
        # Read the ALREADY-RESOLVED, allowlisted real path with O_NOFOLLOW so a
        # symlink swapped in for the final component between the allowlist check
        # and the read is refused rather than followed. Then fstat the open fd
        # and require its (st_dev, st_ino) to match the identity captured during
        # allowlisting — this closes a parent-directory swap that O_NOFOLLOW
        # would otherwise follow. Any mismatch / vanished / swapped page returns
        # a clean 404, never a 500.
        try:
            fd = os.open(str(resolved_target), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                st_open = os.fstat(fd)
                if (st_open.st_dev, st_open.st_ino) != allowlisted_identity:
                    return bad(handler, "Page not found", status=404)
                raw = os.read(fd, _LLM_WIKI_MAX_PAGE_BYTES + 1)
            finally:
                os.close(fd)
            if len(raw) > _LLM_WIKI_MAX_PAGE_BYTES:
                raw = raw[:_LLM_WIKI_MAX_PAGE_BYTES]
            content = raw.decode("utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            return bad(handler, "Page not found", status=404)
        except OSError:
            # ELOOP (symlink swapped in under O_NOFOLLOW) or any other read
            # failure → clean 404, never a 500.
            return bad(handler, "Could not read page", status=404)
        return j(handler, {"content": content, "path": page_path})
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
