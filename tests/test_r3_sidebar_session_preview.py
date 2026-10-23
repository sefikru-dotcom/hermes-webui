"""R3: `/api/sessions` must expose a `preview` snippet on every session row.

Native clients render a session row's subtitle from this field (Android
`SessionModels.kt`: ``val preview: String?``, populated from the
``preview``/``last_message``/``summary`` lookup in `SessionRepository`). The
server never emitted it: the list row is filtered through
``_SIDEBAR_SESSION_RESPONSE_FIELDS`` and only ``match_preview`` — filled
exclusively by the search branch — was allowed through, so the Android subtitle
was always blank. The web sidebar does not read the field.

The snippet must come from messages that are already loaded: the session index
is the list source, and re-reading a session file per row is the N-file read
storm /api/sessions was already fixed for (#4662 Phase 3).
"""
import json
import pathlib
import sys
import time
import urllib.request
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import api.models as models
import api.routes as routes

_needs_server = pytest.mark.usefixtures("test_server")
from tests._pytest_port import BASE  # noqa: E402  (needs conftest env first)

_SECRET = "sk-" + ("B" * 44)
_LONG_MESSAGE = "alpha beta gamma delta " * 40  # 920 chars, many word boundaries


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read())


def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _write_session(messages, *, title="preview probe", created=None):
    """Persist a real session through the model layer (updates the index)."""
    from tests.conftest import TEST_WORKSPACE

    sid = "r3_preview_" + uuid.uuid4().hex[:8]
    now = time.time() if created is None else created
    models.Session(
        session_id=sid,
        title=title,
        workspace=str(TEST_WORKSPACE),
        model="test-model",
        created_at=now,
        updated_at=now,
        profile="default",
        messages=messages,
        tool_calls=[],
    ).save(touch_updated_at=False)
    return sid


def _session_row(sid):
    data = _get("/api/sessions")
    for row in data.get("sessions", []) or []:
        if row.get("session_id") == sid:
            return row
    raise AssertionError(f"session {sid} missing from /api/sessions: {sorted(data)}")


# ── (a) the field exists, is single-line and bounded ──────────────────────────


def test_compact_emits_single_line_preview_from_last_substantive_message():
    now = time.time()
    session = models.Session(
        session_id="r3compact",
        title="t",
        workspace="/tmp",
        messages=[
            {"role": "user", "content": "older user line", "timestamp": now},
            {
                "role": "assistant",
                "content": "Here is the summary:\n\n  the release ships  preview\n support.",
                "timestamp": now + 1,
            },
            {"role": "tool", "content": '{"ok": true}', "timestamp": now + 2},
        ],
    )

    preview = session.compact()["preview"]

    # Tool rows are machine noise; the snippet is the last human-visible line,
    # with every whitespace run (including newlines) collapsed to one space.
    assert preview == "Here is the summary: the release ships preview support."


def test_compact_truncates_long_preview_on_a_word_boundary():
    now = time.time()
    session = models.Session(
        session_id="r3long",
        title="t",
        workspace="/tmp",
        messages=[{"role": "assistant", "content": _LONG_MESSAGE, "timestamp": now}],
    )

    preview = session.compact()["preview"]
    normalized = " ".join(_LONG_MESSAGE.split())

    assert preview.endswith("..."), preview
    assert len(preview) <= models._SIDEBAR_PREVIEW_MAX_CHARS + 3, len(preview)
    # Word-boundary cut: everything before the ellipsis is an exact prefix and
    # does not end mid-word.
    assert normalized.startswith(preview[:-3]), preview
    assert not preview[:-3].endswith(" "), preview


def test_compact_preview_is_none_for_an_empty_session():
    session = models.Session(session_id="r3empty", title="t", workspace="/tmp", messages=[])
    assert session.compact()["preview"] is None


@_needs_server
def test_api_sessions_returns_preview(cleanup_test_sessions):
    now = time.time()
    sid = _write_session(
        [
            {"role": "user", "content": "make the sidebar show a subtitle", "timestamp": now},
            {"role": "assistant", "content": _LONG_MESSAGE, "timestamp": now + 1},
        ]
    )
    cleanup_test_sessions.append(sid)

    row = _session_row(sid)

    assert "preview" in row, f"/api/sessions dropped the preview field: {sorted(row)}"
    preview = row["preview"]
    assert isinstance(preview, str) and preview.strip(), preview
    assert "\n" not in preview, preview
    assert preview.endswith("..."), preview
    assert len(preview) <= models._SIDEBAR_PREVIEW_MAX_CHARS + 3, len(preview)
    assert " ".join(_LONG_MESSAGE.split()).startswith(preview[:-3]), preview


def test_sidebar_response_item_keeps_preview_and_still_drops_unknown_fields():
    assert "preview" in routes._SIDEBAR_SESSION_RESPONSE_FIELDS, (
        "preview is missing from _SIDEBAR_SESSION_RESPONSE_FIELDS, so "
        "_sidebar_session_response_item() filters it out of /api/sessions"
    )
    row = routes._sidebar_session_response_item(
        {
            "session_id": "s1",
            "title": "t",
            "preview": "last line of the conversation",
            "not_allowed_field": "x",
        },
        redact_enabled=False,
    )
    assert row["preview"] == "last line of the conversation"
    assert "not_allowed_field" not in row


# ── (b) preview is user content: it must be redacted like title ───────────────


@_needs_server
def test_api_sessions_preview_is_redacted(cleanup_test_sessions):
    _post("/api/settings", {"api_redact_enabled": True})
    now = time.time()
    sid = _write_session(
        [
            {"role": "user", "content": "wire up the billing client", "timestamp": now},
            {"role": "assistant", "content": f"done, I used {_SECRET} for auth", "timestamp": now + 1},
        ]
    )
    cleanup_test_sessions.append(sid)

    row = _session_row(sid)

    assert row.get("preview"), f"preview missing/empty: {row.get('preview')!r}"
    assert _SECRET not in json.dumps(row), f"credential leaked into preview: {row['preview']!r}"


def test_preview_is_redacted_through_the_shared_sidebar_redactor(monkeypatch):
    """The shared helper is the chokepoint: search rows and list rows must agree."""
    monkeypatch.setattr("api.config.load_settings", lambda: {"api_redact_enabled": True})
    monkeypatch.setattr("api.helpers.load_settings", lambda: {"api_redact_enabled": True}, raising=False)
    monkeypatch.setattr(routes, "_session_list_cache_overlay_runtime_rows", lambda rows: rows)

    payload = {
        "sessions": [{"session_id": "s1", "title": "clean", "preview": f"key {_SECRET}"}],
        "cli_count": 0,
    }
    resp = routes._session_list_payload_to_response(payload)

    preview = resp["sessions"][0]["preview"]
    assert preview, "preview dropped from the /api/sessions response"
    assert _SECRET not in preview, f"credential leaked into preview: {preview!r}"


# ── (c) legacy index rows without `preview` must not break the list ──────────


def _isolate_models_state(monkeypatch, tmp_path, rows):
    """Point api.models at a temp session dir holding `rows` as index entries."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    now = time.time()
    for i, row in enumerate(rows):
        sid = row["session_id"]
        payload = {
            "session_id": sid,
            "title": row.get("title") or f"Session {i}",
            "workspace": str(tmp_path),
            "model": "test-model",
            "profile": "default",
            "messages": [
                {"role": "user", "content": f"question {i}", "timestamp": now},
                {"role": "assistant", "content": f"answer {i}", "timestamp": now + 1},
            ],
            "created_at": now + i,
            "updated_at": now + i,
        }
        (session_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")
    (session_dir / "_index.json").write_text(json.dumps(rows), encoding="utf-8")
    return session_dir


def _legacy_row(sid, ts):
    """An index row written before R3: every sidebar field except `preview`."""
    return {
        "session_id": sid,
        "title": f"legacy {sid}",
        "workspace": "/tmp",
        "model": "test-model",
        "profile": "default",
        "message_count": 2,
        "user_message_count": 1,
        "created_at": ts,
        "updated_at": ts,
        "last_message_at": ts,
    }


def test_legacy_index_row_without_preview_is_listed_and_backfilled(monkeypatch, tmp_path):
    ts = time.time()
    _isolate_models_state(monkeypatch, tmp_path, [_legacy_row("r3legacy1", ts)])

    rows = models.all_sessions(sidebar_metadata_only=True)

    assert [row["session_id"] for row in rows] == ["r3legacy1"]
    assert rows[0]["preview"] == "answer 0"

    # The rebuilt row is persisted, so the next request needs no session load.
    index = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    assert index[0]["preview"] == "answer 0"


def test_preview_backfill_is_bounded_per_call_and_converges(monkeypatch, tmp_path):
    ts = time.time()
    total = 40
    legacy = [_legacy_row(f"r3bulk{i}", ts - i) for i in range(total)]
    _isolate_models_state(monkeypatch, tmp_path, legacy)

    loads = {"n": 0}
    real_load = models.Session.load.__func__

    def _counting_load(cls, *args, **kwargs):
        loads["n"] += 1
        return real_load(cls, *args, **kwargs)

    monkeypatch.setattr(models.Session, "load", classmethod(_counting_load))

    limit = models._SIDEBAR_PREVIEW_BACKFILL_MAX_ROWS
    assert limit < total  # otherwise this test proves nothing about bounding

    first = models.all_sessions(sidebar_metadata_only=True)
    first_loads = loads["n"]
    assert first_loads <= limit, (
        f"first /api/sessions loaded {first_loads} sessions; the backfill must be "
        f"bounded to {limit} per call to stay off the N+1 path"
    )
    assert len(first) == total, "legacy rows must still be listed"
    with_preview = [row for row in first if "preview" in row]
    assert with_preview, "the bounded slice must produce previews immediately"
    assert all(row["preview"] for row in with_preview), "backfilled preview is empty"

    # Every call pays at most `limit` loads and each row is loaded at most once,
    # so the index converges instead of rescanning.
    prev = loads["n"]
    for _ in range(total):
        models.all_sessions(sidebar_metadata_only=True)
        delta = loads["n"] - prev
        assert delta <= limit, f"one /api/sessions call loaded {delta} sessions; budget is {limit}"
        prev = loads["n"]

    rows = models.all_sessions(sidebar_metadata_only=True)
    missing = [row["session_id"] for row in rows if "preview" not in row]
    assert not missing, f"{len(missing)} rows never got a preview: {missing[:3]}"

    converged_loads = loads["n"]
    models.all_sessions(sidebar_metadata_only=True)
    assert loads["n"] == converged_loads, (
        "a converged index must not load any session file on the sidebar path"
    )
