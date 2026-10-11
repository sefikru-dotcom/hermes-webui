"""Call-time facade contracts for workspace Git routes."""
from types import SimpleNamespace


def test_git_session_workspace_follows_current_session_lookup(monkeypatch, tmp_path):
    from api import routes

    current = [tmp_path / "first"]
    monkeypatch.setattr(routes, "get_session", lambda sid: SimpleNamespace(workspace=str(current[0])))
    assert routes._git_session_workspace(object(), "session") == current[0]
    current[0] = tmp_path / "second"
    assert routes._git_session_workspace(object(), "session") == current[0]


def test_git_handler_uses_current_workspace_and_response(monkeypatch, tmp_path):
    from api import routes, workspace_git

    current = [tmp_path / "first"]
    seen = []
    monkeypatch.setattr(routes, "_git_session_workspace", lambda handler, sid: current[0])
    monkeypatch.setattr(workspace_git, "git_status", lambda workspace: seen.append(workspace) or {"root": str(workspace)})
    monkeypatch.setattr(routes, "j", lambda handler, payload: payload)
    parsed = SimpleNamespace(query="session_id=session")
    assert routes._handle_git_status(object(), parsed)["git"]["root"] == str(current[0])
    current[0] = tmp_path / "second"
    assert routes._handle_git_status(object(), parsed)["git"]["root"] == str(current[0])
    assert seen == [tmp_path / "first", tmp_path / "second"]


def test_git_safety_resolves_current_stream_ownership(monkeypatch):
    from api import routes, workspace_git

    rejected = []
    session = SimpleNamespace(read_only=False, source_tag="webui")
    monkeypatch.setattr(workspace_git, "workspace_git_destructive_enabled", lambda: True)
    monkeypatch.setattr(routes, "_git_bad", lambda handler, error, **kwargs: rejected.append(str(error)))
    monkeypatch.setattr(routes, "_git_locked_by_active_stream", lambda current: True)
    assert routes._git_reject_destructive_if_unsafe(object(), session) is True
    assert rejected
    monkeypatch.setattr(routes, "_git_locked_by_active_stream", lambda current: False)
    assert routes._git_reject_destructive_if_unsafe(object(), session) is False


def test_git_error_uses_current_sanitizer_and_response(monkeypatch):
    from api import routes, workspace_git

    monkeypatch.setattr(routes, "_sanitize_error", lambda message: "redacted")
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: (payload, status))
    payload, status = routes._git_bad(object(), workspace_git.GitWorkspaceError("secret"), status=409)
    assert payload["error"] == "redacted"
    assert status == 409
