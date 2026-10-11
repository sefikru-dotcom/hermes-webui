"""Compatibility seams for profile memory and project-context extraction."""
from types import SimpleNamespace


def test_memory_reads_follow_current_profile_between_calls(monkeypatch, tmp_path):
    from api import profiles, routes

    first = tmp_path / "first"
    second = tmp_path / "second"
    for home, text in ((first, "first-memory"), (second, "second-memory")):
        (home / "memories").mkdir(parents=True)
        (home / "memories" / "MEMORY.md").write_text(text)
    active = [first]
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active[0])
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: {})
    monkeypatch.setattr(routes, "_memory_project_context_workspace", lambda parsed: None)
    monkeypatch.setattr(routes, "_external_notes_sources_enabled", lambda cfg: False)
    monkeypatch.setattr(routes, "j", lambda handler, payload: payload)

    assert routes._handle_memory_read(object())["memory"] == "first-memory"
    active[0] = second
    assert routes._handle_memory_read(object())["memory"] == "second-memory"


def test_memory_writes_follow_current_config_without_cached_permissions(monkeypatch, tmp_path):
    from api import profiles, routes

    cfg = {"memory": {"memory_enabled": True}}
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(routes, "get_config_snapshot", lambda: cfg)
    monkeypatch.setattr(routes, "j", lambda handler, payload: payload)
    monkeypatch.setattr(routes, "bad", lambda handler, error, status=400: (error, status))

    body = {"section": "memory", "content": "allowed"}
    assert routes._handle_memory_write(object(), body)["ok"] is True
    cfg["memory"]["memory_enabled"] = False
    rejected = routes._handle_memory_write(object(), {**body, "content": "blocked"})
    assert rejected[1] == 403
    assert (tmp_path / "memories" / "MEMORY.md").read_text() == "allowed"


def test_project_context_workspace_uses_current_trust_resolver(monkeypatch, tmp_path):
    from api import routes

    seen = []
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr(routes, "get_last_workspace", lambda: "selected")

    def resolve(workspace):
        seen.append(workspace)
        return str(tmp_path / workspace)

    monkeypatch.setattr(routes, "resolve_trusted_workspace", resolve)
    assert routes._memory_project_context_workspace(SimpleNamespace(query="")) == tmp_path / "selected"
    assert routes._memory_project_context_workspace(SimpleNamespace(query="workspace=explicit")) == tmp_path / "explicit"
    assert seen == ["selected", "explicit"]


def test_project_context_reader_uses_current_candidates_and_limits(monkeypatch, tmp_path):
    from api import routes

    page = tmp_path / "AGENTS.md"
    page.write_text("abcdefgh")
    monkeypatch.setattr(routes, "_project_context_candidates", lambda workspace: [page])
    monkeypatch.setattr(routes, "_strip_project_context_frontmatter", lambda content: content.upper())
    monkeypatch.setattr(routes, "_PROJECT_CONTEXT_MAX_BYTES", 3)

    assert routes._read_active_project_context(tmp_path)["content"] == "ABC"
    monkeypatch.setattr(routes, "_project_context_candidates", lambda workspace: [])
    assert routes._read_active_project_context(tmp_path)["content"] == ""
