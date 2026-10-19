"""The embedded /dashboard/ and /music/ sections load lazily and degrade visibly.

A local runtime pack has no reverse proxy behind those paths, so an eager
iframe would sit forever on a JSON 404. The shell must therefore:

* ship both iframes without a ``src`` (nothing is fetched at boot),
* expose a first-class "unavailable" state with a retry action,
* hide the navigation tab only when the section is confirmed not mounted,
* never mark a timeout/network failure as "missing".
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
FRAMES_JS = (REPO / "static" / "embedded_frames.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
SW_JS = (REPO / "static" / "sw.js").read_text(encoding="utf-8")


def _iframe_tag(frame_id: str) -> str:
    m = re.search(r'<iframe[^>]*id="%s"[^>]*>' % re.escape(frame_id), INDEX_HTML)
    assert m, f"{frame_id} iframe missing"
    return m.group(0)


def test_embedded_iframes_have_no_eager_src():
    for frame_id, site in (("projectsIframe", "/dashboard/"), ("musicIframe", "/music/")):
        tag = _iframe_tag(frame_id)
        assert not re.search(r"\ssrc=", tag), f"{frame_id} must not be fetched at boot"
        assert f'data-src="{site}"' in tag, f"{frame_id} must declare its site for lazy load"
        assert "title=" in tag, f"{frame_id} needs an accessible title"


def test_embedded_frames_loader_is_shipped_and_precached():
    assert '<script src="static/embedded_frames.js' in INDEX_HTML
    assert "./static/embedded_frames.js' + VQ" in SW_JS
    assert "window.ensureEmbeddedFrame = ensureEmbeddedFrame;" in FRAMES_JS
    assert "window.retryEmbeddedFrame = retryEmbeddedFrame;" in FRAMES_JS


def test_unavailable_state_is_explicit_and_retryable():
    for prefix in ("projects", "music"):
        assert f'id="{prefix}FrameUnavailable"' in INDEX_HTML
        assert f"onclick=\"retryEmbeddedFrame('{'dashboard' if prefix == 'projects' else 'music'}')\"" in INDEX_HTML
    assert 'data-i18n="embedded_frame_unavailable_title"' in INDEX_HTML
    assert 'data-i18n="embedded_frame_retry"' in INDEX_HTML


def test_probe_only_treats_missing_mount_as_unavailable():
    # 404/501/502/503 and a non-HTML 200 body mean "not mounted here".
    for status in ("404", "501", "502", "503"):
        assert f"res.status === {status}" in FRAMES_JS
    # Timeouts keep the retry affordance and must not hide the tab.
    assert "'timeout'" in FRAMES_JS
    assert "if(unavailable && typeof _markSidebarTabHidden === 'function')" in FRAMES_JS


def test_panel_switch_uses_the_lazy_loader():
    assert "window.ensureEmbeddedFrame('dashboard')" in PANELS_JS
    assert "window.ensureEmbeddedFrame('music')" in PANELS_JS
    # The unconditional eager assignment in switchPanel() must be gone.
    assert "(!f.src || f.src.indexOf('/dashboard') === -1)" not in PANELS_JS
    assert "(!f.src || f.src.indexOf('/music') === -1)" not in PANELS_JS


def test_reload_buttons_go_through_retry():
    assert "window.retryEmbeddedFrame('music')" in PANELS_JS
    assert "window.retryEmbeddedFrame('dashboard')" in PANELS_JS


def test_embedded_frame_styles_exist():
    for selector in (
        ".embedded-frame-host",
        ".embedded-frame-state",
        ".embedded-frame-unavailable",
        ".embedded-frame-spinner",
        ".nav-tab.nav-tab-unavailable",
    ):
        assert selector in STYLE_CSS, f"missing style for {selector}"


def test_probe_uses_origin_safe_comparison():
    # `location.origin` is undefined in some embedded webviews; compare the
    # resolved protocol/host against the document base instead.
    assert "function _isSameOrigin(url)" in FRAMES_JS
    assert "url.protocol === base.protocol && url.host === base.host" in FRAMES_JS
    assert not re.search(r"location\.origin", FRAMES_JS)
