"""Regression coverage for self-hosted (vendored) front-end dependencies.

The desktop pack must work with no network at all, so xterm, its addons, and
Prism are served from ``static/vendor/`` (taken from npm tarballs) instead of
cdn.jsdelivr.net. Every vendored asset keeps an SRI hash computed from the file
that actually ships — these tests recompute it, so a bad copy fails loudly.
"""
from __future__ import annotations

import base64
import hashlib
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
STATIC = REPO / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
BOOT_JS = (STATIC / "boot.js").read_text(encoding="utf-8")
SW_JS = (STATIC / "sw.js").read_text(encoding="utf-8")

VENDORED = [
    "vendor/xterm/5.3.0/xterm.css",
    "vendor/xterm/5.3.0/xterm.js",
    "vendor/xterm-addon-fit/0.8.0/xterm-addon-fit.js",
    "vendor/xterm-addon-web-links/0.9.0/xterm-addon-web-links.js",
    "vendor/prismjs/1.29.0/prism-core.min.js",
    "vendor/prismjs/1.29.0/plugins/autoloader/prism-autoloader.min.js",
    "vendor/prismjs/1.29.0/themes/prism-tomorrow.min.css",
    "vendor/prismjs/1.29.0/themes/prism.min.css",
]


def _sri(path: pathlib.Path) -> str:
    digest = hashlib.sha384(path.read_bytes()).digest()
    return "sha384-" + base64.b64encode(digest).decode("ascii")


def test_vendored_files_are_present():
    missing = [rel for rel in VENDORED if not (STATIC / rel).is_file()]
    assert not missing, f"missing vendored assets: {missing}"


def test_index_html_has_no_cdn_dependency():
    assert "cdn.jsdelivr.net" not in INDEX_HTML, (
        "index.html must not reference cdn.jsdelivr.net — the offline desktop "
        "pack loses the terminal and syntax highlighting without the network"
    )


def test_vendored_script_tags_carry_matching_sri():
    tags = re.findall(r"<(?:script|link)[^>]*static/vendor/[^>]*>", INDEX_HTML)
    checked = 0
    for tag in tags:
        # `@1.29.0` in a vendored href is a version marker, not part of the path.
        src = re.search(r'(?:src|href)="static/([^"?]+)', tag)
        integrity = re.search(r'integrity="(sha384-[^"]+)"', tag)
        if not src or not integrity:
            continue
        path = STATIC / src.group(1).replace("@", "/")
        assert path.is_file(), f"tag points at a missing file: {src.group(1)}"
        assert integrity.group(1) == _sri(path), (
            f"SRI mismatch for {src.group(1)} — recompute sha384 after updating the file"
        )
        checked += 1
    # index.html ships 7 SRI-pinned tags (xterm css/js + 2 addons + prism core/
    # autoloader/theme); prism.min.css is only needed for light theme at runtime.
    assert checked >= 7, f"expected the xterm/prism tags to be SRI-checked, got {checked}"


def test_boot_theme_swap_uses_vendored_prism_css():
    assert "static/vendor/prismjs/" in BOOT_JS
    assert "cdn.jsdelivr.net" not in BOOT_JS


def test_shell_assets_precache_vendored_bundles():
    for rel in VENDORED:
        assert f"./static/{rel}' + VQ" in SW_JS, f"{rel} missing from SHELL_ASSETS"
