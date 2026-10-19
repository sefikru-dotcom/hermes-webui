"""api() must only replay requests whose effects are safe to repeat.

A POST/PUT/PATCH that times out mid-flight may already have been applied by the
server; retrying it duplicates work (double-sent messages, double-created
tasks). Callers that know their endpoint is replay-safe opt in explicitly.
"""
from __future__ import annotations

import pathlib
import re

WORKSPACE_JS = (pathlib.Path(__file__).parent.parent / "static" / "workspace.js").read_text(encoding="utf-8")
_API = WORKSPACE_JS.split("async function api(", 1)[1][:3000]


def test_retry_is_gated_on_idempotent_methods():
    assert "const _method=String(opts.method||'GET').toUpperCase();" in _API
    assert "_method==='GET'||_method==='HEAD'||opts.retryUnsafe===true" in _API


def test_non_idempotent_requests_get_a_single_attempt():
    m = re.search(r"const maxAttempts=(.*?);\n", _API)
    assert m, "maxAttempts must be derived from the method"
    expr = m.group(1)
    assert "!_retryAllowed?1:" in expr, f"non-idempotent calls must default to one attempt: {expr}"


def test_retry_options_are_ignored_for_unsafe_methods():
    assert "const retryTimeouts=_retryAllowed&&opts.retryTimeouts===true;" in _API
    assert "const retryStatuses=_retryAllowed&&Array.isArray(opts.retryStatuses)" in _API


def test_opt_in_flag_is_not_forwarded_to_fetch():
    assert "delete fetchOpts.retryUnsafe;" in _API


def test_get_retry_behaviour_is_preserved():
    # GET keeps the historical 3 attempts (1 + 2 retries) on network errors.
    assert ":3);" in _API
