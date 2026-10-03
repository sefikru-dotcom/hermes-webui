"""Plugin visibility helpers extracted from api.routes.

Pure sanitizing/categorization helpers plus the Settings payload builder
for issue #539. api.routes keeps _get_plugin_manager_for_visibility and
the thin _plugin_visibility_payload wrapper because tests patch the
manager getter on the routes module; the wrapper injects it here at call
time.
"""

from __future__ import annotations


_PLUGIN_VISIBILITY_HOOKS = (
    "pre_tool_call",
    "post_tool_call",
    "pre_llm_call",
    "post_llm_call",
)
_PLUGIN_VISIBILITY_HOOK_SET = set(_PLUGIN_VISIBILITY_HOOKS)


def _clean_plugin_visibility_text(value, *, limit=240) -> str:
    """Return bounded display text without path/callback-like internals."""
    if value is None:
        return ""
    text = str(value).replace("\x00", "").strip()
    # Display metadata should be plain labels/descriptions. Drop multiline text
    # and common path separators rather than risk leaking local plugin paths.
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _plugin_visibility_category_from_key(key: str) -> str:
    """Return the config category prefix for nested plugin keys."""
    raw = str(key or "").strip().replace("\\", "/")
    if "/" not in raw:
        return ""
    category = raw.split("/", 1)[0].strip()
    return category if category and category not in {".", ".."} else ""


def _plugin_visibility_selected_provider(category: str) -> str:
    """Read ``<category>.provider`` without surfacing config errors."""
    if not category:
        return ""
    try:
        from api.config import get_config as _get_cfg

        cfg = _get_cfg() or {}
    except Exception:
        return ""
    category_cfg = cfg.get(category, {}) if isinstance(cfg, dict) else {}
    if not isinstance(category_cfg, dict):
        return ""
    return str(category_cfg.get("provider") or "").strip().lower()


def build_plugin_visibility_payload(manager=None, *, manager_getter) -> dict:
    """Build a sanitized plugin/hook visibility payload for Settings.

    The Hermes Agent manager stores manifests and callback objects internally.
    This endpoint intentionally exposes only safe, user-facing metadata and the
    four lifecycle hook names called out by the Settings visibility MVP. It
    never includes plugin source paths, callback names, callback reprs, or raw
    load errors because those can contain private filesystem details.

    Exclusive plugins (e.g. memory providers) are activated through their
    category's ``<category>.provider`` config, not through ``plugins.enabled``.
    Their ``loaded.enabled`` stays False by design and they register hooks
    outside the four visibility hooks below. The payload surfaces ``kind``
    and ``activation`` plus ``is_active_provider`` when the plugin key carries a
    category prefix so the panel can render the selected provider distinctly
    instead of mislabeling it as "Disabled" with no hooks (issue #2659), while
    still showing unselected exclusive providers as disabled. Flat-key exclusive
    plugins omit the new field so older activation-based badge semantics remain
    intact when the category cannot be inferred.
    """
    manager = manager or manager_getter()
    manager.discover_and_load(force=False)

    plugins = []

    # Hermes Agent lifecycle-hook plugins
    raw_plugins = getattr(manager, "_plugins", {}) or {}
    for key, loaded in sorted(raw_plugins.items(), key=lambda item: str(item[0])):
        manifest = getattr(loaded, "manifest", None)
        if manifest is None:
            continue
        plugin_key = _clean_plugin_visibility_text(
            getattr(manifest, "key", None) or key or getattr(manifest, "name", ""),
            limit=120,
        )
        name = _clean_plugin_visibility_text(getattr(manifest, "name", "") or plugin_key, limit=120)
        version = _clean_plugin_visibility_text(getattr(manifest, "version", ""), limit=80)
        description = _clean_plugin_visibility_text(getattr(manifest, "description", ""), limit=280)
        kind = _clean_plugin_visibility_text(getattr(manifest, "kind", "") or "standalone", limit=40)
        enabled_flag = bool(getattr(loaded, "enabled", False))
        category = _plugin_visibility_category_from_key(plugin_key)
        selected_provider = _plugin_visibility_selected_provider(category)
        plugin_slug = plugin_key.rsplit("/", 1)[-1].strip().lower()
        if kind == "exclusive":
            activation = "exclusive"
        elif kind == "model-provider" and enabled_flag:
            activation = "provider"
        else:
            activation = "enabled" if enabled_flag else "disabled"
        include_active_provider = True
        if kind == "exclusive":
            if category:
                is_active_provider = bool(selected_provider) and plugin_slug == selected_provider
            else:
                include_active_provider = False
                is_active_provider = False
        else:
            is_active_provider = kind == "model-provider" and enabled_flag
        registered = []
        for hook in list(getattr(manifest, "provides_hooks", []) or []) + list(getattr(loaded, "hooks_registered", []) or []):
            hook_name = str(hook or "").strip()
            if hook_name in _PLUGIN_VISIBILITY_HOOK_SET and hook_name not in registered:
                registered.append(hook_name)
        registered.sort(key=_PLUGIN_VISIBILITY_HOOKS.index)
        plugin_payload = {
            "name": name,
            "key": plugin_key or name,
            "version": version,
            "description": description,
            # `enabled` is preserved for back-compat with older WebUI clients
            # that key off it directly. New clients should prefer `activation`.
            "enabled": enabled_flag,
            "kind": kind,
            "activation": activation,
            "hooks": registered,
        }
        if include_active_provider:
            plugin_payload["is_active_provider"] = bool(is_active_provider)
        plugins.append(plugin_payload)

    return {
        "plugins": plugins,
        "empty": not bool(plugins),
        "supported_hooks": list(_PLUGIN_VISIBILITY_HOOKS),
        "read_only": True,
    }
