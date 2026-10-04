"""Call-time compatibility and security boundaries for file route extraction."""
import os
from pathlib import Path
from types import SimpleNamespace


def test_file_save_uses_current_anchor_and_route_session(monkeypatch, tmp_path):
    from api import routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("old")
    seen = []
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: SimpleNamespace(workspace=str(workspace)))
    monkeypatch.setattr(routes, "safe_resolve", lambda root, rel: target)
    monkeypatch.setattr(routes, "j", lambda handler, payload: payload)

    def write(root, path):
        seen.append((root, path))
        return os.open(path, os.O_WRONLY | os.O_TRUNC)

    monkeypatch.setattr(routes, "open_anchored_write_fd", write)
    assert routes._handle_file_save(object(), {"session_id": "session", "path": "file.txt", "content": "new"})["ok"] is True
    assert target.read_text() == "new"
    assert seen == [(workspace, target)]


def test_raw_file_handler_uses_current_resolver_and_sender(monkeypatch, tmp_path):
    from api import routes

    target = tmp_path / "file.txt"
    calls = []
    monkeypatch.setattr(routes, "get_session_for_file_ops", lambda sid: SimpleNamespace(workspace=str(tmp_path)))
    monkeypatch.setattr(routes, "_file_raw_target", lambda session, sid, rel: (tmp_path, target))
    monkeypatch.setattr(routes, "_serve_file_bytes", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    assert routes._handle_file_raw(object(), SimpleNamespace(query="session_id=s&path=file.txt")) is True
    assert calls[0][0][1] == target
    assert calls[0][1]["anchor_root"] == tmp_path


def test_etag_snapshot_uses_current_cap_and_digest(monkeypatch, tmp_path):
    from api import routes

    target = tmp_path / "file.txt"
    target.write_bytes(b"snapshot")
    fd = os.open(target, os.O_RDONLY)
    try:
        monkeypatch.setattr(routes, "_ETAG_SIZE_CAP", 100)
        monkeypatch.setattr(routes, "_bytes_etag", lambda data: "test-digest")
        assert routes._etag_and_snapshot(fd, file_size=8) == ("test-digest", b"snapshot", 8)
        monkeypatch.setattr(routes, "_ETAG_SIZE_CAP", 1)
        assert routes._etag_and_snapshot(fd, file_size=8) == (None, None, 8)
    finally:
        os.close(fd)


def test_workspace_rename_uses_current_storage_callbacks(monkeypatch, tmp_path):
    from api import routes

    workspaces = [{"path": str(tmp_path), "name": "old"}]
    writes = []
    monkeypatch.setattr(routes, "load_workspaces", lambda: workspaces)
    monkeypatch.setattr(routes, "save_workspaces", lambda current: writes.append(current))
    monkeypatch.setattr(routes, "j", lambda handler, payload: payload)
    result = routes._handle_workspace_rename(object(), {"path": str(tmp_path), "name": "new"})
    assert result["ok"] is True
    assert workspaces[0]["name"] == "new"
    assert writes == [workspaces]


def test_folder_collect_preserves_caps_and_excludes_symlink_escapes(tmp_path):
    from api import routes

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "one.txt").write_text("123")
    (workspace / "two.txt").write_text("456")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace / "escape.txt").symlink_to(outside)
    files, size, reason = routes._folder_download_collect(workspace, workspace, 100, 10)
    assert {Path(name).name for path, name in files} == {"one.txt", "two.txt"}
    assert size == 6
    assert reason is None
    assert routes._folder_download_collect(workspace, workspace, 100, 1)[2] == "max_files"
    assert routes._folder_download_collect(workspace, workspace, 1, 10)[2] == "max_bytes"
