"""The browser-guard classifier must never mark a managed Hermes browser as killable.

This is the one invariant that protects live chat sessions: the janitor kills
anything ``is_test_browser`` accepts, so a false positive would kill a legitimate
browser tool run. These tests pin the discriminator (temp profile) without
touching /proc.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = ROOT / "ops" / "browser-guard" / "guard_lib.py"


def _load():
    spec = importlib.util.spec_from_file_location("mars_guard_lib", GUARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mars_guard_lib"] = module
    spec.loader.exec_module(module)
    return module


guard_lib = _load()

WEBUI = "/system.slice/hermes-webui.service"
SLICE = "/mars.slice/mars-testbrowsers.slice/mars-tb-x-1.scope"
BROWSER_SVC = "/system.slice/mars-browser.service"


def info(cgroup: str, profile: str | None, headless: bool = True) -> dict:
    return {"pid": 4242, "exe": "/opt/google/chrome/chrome", "profile": profile,
            "port": 0, "headless": headless, "cgroup": cgroup, "age": 3600.0}


def test_incident_shape_is_killable():
    """Exactly the 2026-11-15 process: /tmp profile inside the WebUI cgroup."""
    assert guard_lib.is_test_browser(info(WEBUI, "/tmp/kv-pop-chrome-JGW3Zs"))


def test_guard_launched_browser_in_own_slice_is_killable():
    assert guard_lib.is_test_browser(info(SLICE, "/tmp/guard-kv-abc123"))


def test_managed_hermes_browser_is_never_killable():
    """browser tools keep their profile under $HERMES_HOME, not /tmp."""
    managed = info(WEBUI, "/root/.hermes/browser-profile/google-chrome", headless=True)
    assert not guard_lib.is_test_browser(managed)


def test_browser_without_profile_is_never_killable():
    assert not guard_lib.is_test_browser(info(WEBUI, None))


def test_profile_outside_tmp_is_never_killable():
    assert not guard_lib.is_test_browser(info(WEBUI, "/root/profiles/test"))


def test_persistent_cdp_browser_service_is_out_of_scope():
    """:9222 browser runs in mars-browser.service and must be invisible to us."""
    assert not guard_lib.is_test_browser(info(BROWSER_SVC, "/tmp/looks-like-a-test"))


def test_root_profile_under_tmp_path_prefix_is_not_mistaken_for_hermes():
    assert guard_lib.is_test_browser(info(WEBUI, "/tmp/hermes-like-profile"))
