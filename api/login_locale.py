"""Pure locale-key resolution for the self-contained login page."""

from __future__ import annotations


def resolve_login_locale_key(raw_lang: str | None, locale_keys) -> str:
    """Resolve a language tag to one of the available login locale keys."""
    keys = tuple(locale_keys or ())
    if not raw_lang:
        return "en"
    lang = str(raw_lang).strip()
    if not lang:
        return "en"
    if lang in keys:
        return lang
    normalized = lang.replace("_", "-")
    lower = normalized.lower()
    for key in keys:
        if key.lower() == lower:
            return key
    if lower == "zh" or lower.startswith(("zh-cn", "zh-sg", "zh-hans")):
        return "zh"
    if lower.startswith(("zh-tw", "zh-hk", "zh-mo", "zh-hant")):
        return "zh-Hant" if "zh-Hant" in keys else "zh"
    base = lower.split("-", 1)[0]
    for key in keys:
        if key.lower() == base:
            return key
    return "en"
