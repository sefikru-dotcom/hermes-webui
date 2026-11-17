"""Stage-326 hardening tests for #1956 composer-draft input validation.

Opus advisor flagged that POST /api/session/draft accepted text/files of
arbitrary size and type. A misbehaving or malicious client could persist
multi-MB strings on every keystroke via the debounced auto-save. The hardening
(kept when drafts moved to the sidecar store):

- text: must be str; clamped to 50 KB
- files: must be list; clamped to 50 entries

The persistence contract also changed: the autosave must NOT run a full
get_session()/Session.save() (4-5s on large sessions). These source-level
checks pin the validation order and the absence of a full save; behavioural
coverage lives in tests/test_draft_sidecar.py.
"""
from pathlib import Path


ROUTES_SRC = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")


def _draft_route_block() -> str:
    """The POST /api/session/draft block inside handle_post.

    ``/api/session/draft`` also appears in handle_get (the read used to be an
    unreachable branch in handle_post), so the search must start at handle_post.
    """
    post_start = ROUTES_SRC.index("def handle_post(")
    start = ROUTES_SRC.index('    if parsed.path == "/api/session/draft":', post_start)
    end = ROUTES_SRC.index('    if parsed.path == "/api/session/update":', start)
    return ROUTES_SRC[start:end]


def test_draft_text_clamped_to_50kb():
    """Posting a >50KB text field should be silently truncated to 50_000 chars."""
    src = ROUTES_SRC
    assert "_MAX_DRAFT_TEXT = 50_000" in src or "_MAX_DRAFT_TEXT=50_000" in src.replace(" ", ""), (
        "routes.py must define _MAX_DRAFT_TEXT clamp for the composer-draft POST handler"
    )
    assert "text = text[:_MAX_DRAFT_TEXT]" in src, (
        "routes.py must truncate over-large draft text to _MAX_DRAFT_TEXT"
    )


def test_draft_files_clamped_to_50_entries():
    """Posting a >50-entry files list should be silently truncated."""
    assert "_MAX_DRAFT_FILES = 50" in ROUTES_SRC, (
        "routes.py must define _MAX_DRAFT_FILES clamp"
    )
    assert "files = files[:_MAX_DRAFT_FILES]" in ROUTES_SRC, (
        "routes.py must truncate over-large draft files list"
    )


def test_draft_text_type_coerced_to_string():
    """Non-string text must be coerced to empty string, not stored as-is."""
    assert 'if text is not None and not isinstance(text, str):' in ROUTES_SRC, (
        "routes.py must coerce non-string text to empty string before persist"
    )


def test_draft_files_type_coerced_to_list():
    """Non-list files must be coerced to empty list."""
    assert 'if files is not None and not isinstance(files, list):' in ROUTES_SRC, (
        "routes.py must coerce non-list files to empty list before persist"
    )


def test_draft_validation_appears_before_sidecar_write():
    """The validation must run BEFORE the sidecar write, not after."""
    block = _draft_route_block()
    marker_idx = block.find("Stage-326 hardening (per Opus advisor)")
    write_idx = block.find("_write_draft(")
    assert marker_idx != -1 and write_idx != -1, (
        "could not locate validation marker or sidecar write site"
    )
    assert marker_idx < write_idx, "validation block must run before the draft write"


def test_draft_route_uses_sidecar_and_never_saves_the_session():
    """Autosave must not parse/re-serialise the conversation or lock the agent.

    This is the regression that made draft autosave take 4-5s on large
    sessions: get_session() + _get_session_agent_lock() + s.save() on every
    debounced keystroke burst.
    """
    block = _draft_route_block()
    code = "\n".join(
        line for line in block.splitlines() if not line.strip().startswith("#")
    )
    assert "from api.drafts import write_draft" in code or "_write_draft" in code
    assert "get_session(" not in code, (
        "draft autosave must not call get_session(); it is a full conversation load"
    )
    assert "_get_session_agent_lock(" not in code, (
        "draft autosave must not take the agent lock"
    )
    assert ".save(" not in code, (
        "draft autosave must not call Session.save(); the sidecar is the store"
    )


def test_draft_unchanged_payload_still_short_circuits():
    """Duplicate debounced draft POSTs should not rewrite the sidecar."""
    block = _draft_route_block()
    assert "unchanged" in block
    assert 'payload["unchanged"] = True' in block
