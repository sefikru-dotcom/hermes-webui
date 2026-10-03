"""Dashboard plugin discovery helpers for the Mars WebUI."""

from __future__ import annotations


def dashboard_plugin_enabled(plugin_name: str, *, load_settings) -> bool:
    """Return whether a dashboard plugin is explicitly enabled."""
    try:
        prefs = (load_settings() or {}).get("dashboard_plugins", {}) or {}
        return bool(prefs.get(plugin_name, False))
    except Exception:
        return False


def webui_plugin_payload(*, get_plugin_metadata) -> list[dict]:
    """Return manifest-discovered WebUI plugin metadata with safe fallback."""
    try:
        return get_plugin_metadata()
    except Exception:
        return []
