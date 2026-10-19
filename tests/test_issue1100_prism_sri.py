"""Tests for #1100 — Prism.js SRI integrity check no longer blocks theme CSS.

The theme CSS is now self-hosted (`static/vendor/prismjs@1.29.0/`), so the
edge-node digest drift that motivated #1100 cannot happen: one local file,
one sha384. SRI is therefore kept for every vendored Prism asset.
"""
import re


def _prism_theme_tag() -> str:
    with open("static/index.html", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'<link[^>]*id="prism-theme"[^>]*>', src)
    assert m, "prism-theme link must exist"
    return m.group(0)


def test_prism_theme_link_is_vendored_and_has_integrity():
    """Theme CSS is local, so it must carry a stable SRI hash and no CORS mode."""
    link_tag = _prism_theme_tag()
    assert "cdn.jsdelivr.net" not in link_tag
    assert "static/vendor/prismjs" in link_tag
    assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]+"', link_tag), \
        "vendored prism-theme link must carry an SRI hash"
    assert "crossorigin" not in link_tag, \
        "a same-origin stylesheet must not be requested in CORS mode"


def test_prism_theme_version_pinned():
    """The prism CSS URL must pin the version to prevent breaking changes."""
    href = re.search(r'href="([^"]*)"', _prism_theme_tag()).group(1)
    assert "/1.29.0/" in href, f"Prism resources must be pinned to 1.29.0, found href: {href}"


def test_prism_js_still_has_integrity():
    """Prism JS files keep SRI — now against the vendored copies."""
    with open("static/index.html", encoding="utf-8") as f:
        src = f.read()
    assert re.search(r'static/vendor/prismjs[^"]*prism-core\.min\.js[^>]*integrity=', src), \
        "vendored prism-core.min.js should carry an integrity attribute"
    assert re.search(r'static/vendor/prismjs[^"]*prism-autoloader\.min\.js[^>]*integrity=', src), \
        "vendored prism-autoloader.min.js should carry an integrity attribute"


def test_boot_js_set_resolved_theme_uses_vendored_theme():
    """_setResolvedTheme must swap the local Prism theme CSS, not a CDN URL."""
    with open("static/boot.js", encoding="utf-8") as f:
        src = f.read()
    assert "_setResolvedTheme" in src, "_setResolvedTheme function must exist"
    body = src.split("function _setResolvedTheme", 1)[1][:1500]
    assert "cdn.jsdelivr.net" not in body, \
        "_setResolvedTheme must not point at a CDN"
    assert "static/vendor/prismjs" in body
    assert re.search(r"link\.integrity\s*=\s*wantIntegrity", body), \
        "_setResolvedTheme should keep link.integrity in sync with the vendored theme"
