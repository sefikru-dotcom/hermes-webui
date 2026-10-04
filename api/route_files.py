"""Workspace file, folder, escape-access, and workspace registration routes."""

from __future__ import annotations

from pathlib import Path
import json
import os
from urllib.parse import parse_qs
import shutil
import subprocess

def _handle_list_dir(
    handler,
    parsed,
    *,
    WorkspaceBindingPersistenceError,
    _sanitize_error,
    bad,
    dir_signature,
    get_cli_sessions,
    get_last_workspace,
    get_session,
    j,
    list_dir,
    persist_recovered_workspace_binding,
    resolve_implicit_workspace_with_recovery,
    resolve_trusted_workspace,
    serialize_workspace_entries_for_browser,
):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    webui_session = None
    try:
        s = get_session(sid)
        webui_session = s
        workspace = s.workspace
    except KeyError:
        # Fallback for CLI sessions not loaded in WebUI memory
        try:
            cli_meta = None
            for cs in get_cli_sessions():
                if cs["session_id"] == sid:
                    cli_meta = cs
                    break
            if not cli_meta:
                return bad(handler, "Session not found", 404)
            workspace = cli_meta.get("workspace", "")
        except Exception:
            return bad(handler, "Session not found", 404)
    try:
        if webui_session is None:
            workspace = resolve_trusted_workspace(workspace)
            recovered = False
        else:
            stored_workspace = workspace
            workspace, recovered = resolve_implicit_workspace_with_recovery(
                stored_workspace,
                get_last_workspace,
            )
            if recovered:
                persisted = persist_recovered_workspace_binding(
                    webui_session,
                    workspace,
                    expected_workspace=stored_workspace,
                )
                workspace = Path(persisted.workspace)
        rel_path = qs.get("path", ["."])[0]
        entries = list_dir(Path(workspace), rel_path)
        return j(
            handler,
            {
                "entries": serialize_workspace_entries_for_browser(entries),
                "signature": dir_signature(Path(workspace), rel_path, entries),
                "path": rel_path,
                "workspace": str(workspace),
                "workspace_recovered": recovered,
            },
        )
    except WorkspaceBindingPersistenceError as e:
        return bad(handler, _sanitize_error(e), 500)
    except (FileNotFoundError, ValueError) as e:
        return bad(handler, _sanitize_error(e), 404)


def _read_json_request_body(handler, *, max_bytes: int=4096, _safe_content_length, _sanitize_error) -> dict:
    try:
        length = _safe_content_length(handler, max_bytes)
    except (ValueError, OverflowError) as exc:
        raise ValueError(_sanitize_error(exc)) from exc
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid JSON body") from exc
    return payload if isinstance(payload, dict) else {}


def _handle_escape_authorize(
    handler,
    parsed,
    body: dict | None=None,
    *,
    _check_csrf,
    _csrf_rejection_error,
    _read_json_request_body,
    _sanitize_error,
    authorize_escape_target,
    bad,
    get_session_for_file_ops,
    j,
):
    if handler.command != "POST":
        return bad(handler, "method not allowed", 405)
    if not handler.headers.get("Origin"):
        return bad(handler, "browser origin required", 403)
    if not _check_csrf(handler):
        return bad(handler, _csrf_rejection_error(handler), 403)
    if body is None:
        try:
            body = _read_json_request_body(handler)
        except ValueError as exc:
            return bad(handler, _sanitize_error(exc), 400)
    qs = parse_qs(parsed.query)
    sid = str(body.get("session_id") or qs.get("session_id", [""])[0] or "").strip()
    rel = str(body.get("path") or qs.get("path", [""])[0] or "").strip()
    token = str(body.get("token") or qs.get("token", [""])[0] or "").strip()
    if token:
        return bad(handler, "token must not be provided", 400)
    if not sid:
        return bad(handler, "session_id is required")
    if not rel:
        return bad(handler, "path is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        payload = authorize_escape_target(Path(s.workspace), sid, rel)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    return j(handler, payload)


def _handle_escape_list_dir(
    handler,
    parsed,
    *,
    EscapeAuthorizationExpiredError,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    list_authorized_escape_dir,
    serialize_workspace_entries_for_browser,
):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel_path = qs.get("path", ["."])[0]
    try:
        payload = list_authorized_escape_dir(Path(s.workspace), sid, token, rel_path)
        payload["entries"] = serialize_workspace_entries_for_browser(payload.get("entries"))
        return j(handler, payload)
    except FileNotFoundError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)


def _handle_escape_file_read(
    handler,
    parsed,
    *,
    EscapeAuthorizationExpiredError,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    read_authorized_escape_file_content,
):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    try:
        return j(handler, read_authorized_escape_file_content(Path(s.workspace), sid, token, rel))
    except FileNotFoundError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ImportError as exc:
        # Optional Office parsers absent on a lean install — mirror
        # _handle_file_read: a 503 with the install hint, not a 500 traceback.
        return bad(handler, _sanitize_error(exc), 503)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)


def _handle_escape_file_raw(
    handler,
    parsed,
    *,
    EscapeAuthorizationExpiredError,
    MIME_MAP,
    _sanitize_error,
    _serve_file_bytes,
    _serve_inline_html_preview,
    bad,
    get_session_for_file_ops,
    j,
    raw_authorized_escape_target,
):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    token = qs.get("token", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    if not token:
        return bad(handler, "token is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    force_download = qs.get("download", [""])[0] == "1"
    try:
        anchor_root, target = raw_authorized_escape_target(Path(s.workspace), sid, token, rel)
    except FileNotFoundError:
        return j(handler, {"error": "not found"}, status=404)
    except EscapeAuthorizationExpiredError as exc:
        return bad(handler, _sanitize_error(exc), 403)
    except ValueError as exc:
        return bad(handler, _sanitize_error(exc), 404)
    if not target.exists() or not target.is_file():
        return j(handler, {"error": "not found"}, status=404)
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, "application/octet-stream")
    inline_preview = qs.get("inline", [""])[0] == "1"
    dangerous_types = {"text/html", "application/xhtml+xml", "image/svg+xml"}
    html_inline_ok = inline_preview and mime == "text/html"
    disposition = "attachment" if force_download or (mime in dangerous_types and not html_inline_ok) else "inline"
    sandbox_csp = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
    # Content-Security-Policy sandboxing is carried through the csp=sandbox_csp handoff below.
    csp = sandbox_csp if (inline_preview and not force_download and disposition == "inline") else None
    if html_inline_ok:
        return _serve_inline_html_preview(handler, target, "no-store", csp=sandbox_csp, anchor_root=anchor_root)
    return _serve_file_bytes(handler, target, mime, disposition, "no-store", csp=csp, anchor_root=anchor_root)


def _file_raw_target(session, sid: str, rel: str, *, safe_resolve) -> tuple[Path, Path] | None:
    """Resolve /api/file/raw paths from the workspace or this session's uploads."""
    workspace_root = Path(session.workspace)
    try:
        target = safe_resolve(workspace_root, rel)
    except ValueError:
        target = None
    if target and target.exists() and target.is_file():
        return workspace_root, target

    # Chat uploads now live in a per-session attachment inbox outside the
    # workspace. Keep the public URL stable while scoping fallback lookup to
    # the requesting session's own attachment directory.
    try:
        from api.upload import _session_attachment_dir

        attachment_root = _session_attachment_dir(sid)
        attachment_target = safe_resolve(attachment_root, rel)
    except Exception:
        return None
    if attachment_target.exists() and attachment_target.is_file():
        return attachment_root, attachment_target
    return None


def _folder_zip_max_bytes() -> int:
    try:
        mb = int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_MB", "1024"))
    except ValueError:
        mb = 1024
    return max(1, mb) * 1024 * 1024


def _folder_zip_max_files() -> int:
    try:
        return max(1, int(os.getenv("HERMES_WEBUI_FOLDER_ZIP_MAX_FILES", "50000")))
    except ValueError:
        return 50000


def _folder_download_collect(target: Path, workspace_root: Path,
                              max_bytes: int, max_files: int):
    """Walk target dir; return (files, total_bytes, hit_limit_reason_or_None).

    files is a list of (filesystem_path, archive_name) tuples. Each filesystem
    path is reopened through the workspace anchor when streamed into the ZIP.
    Symlinks escaping the workspace are skipped.
    """
    import os as _os
    files = []
    total_bytes = 0
    for root, dirs, names in _os.walk(target, followlinks=False):
        root_path = Path(root)
        try:
            if not root_path.resolve().is_relative_to(workspace_root):
                dirs[:] = []
                continue
        except (ValueError, OSError):
            dirs[:] = []
            continue
        for name in names:
            fp = root_path / name
            if fp.is_symlink():
                try:
                    if not fp.resolve().is_relative_to(workspace_root):
                        continue
                except (ValueError, OSError):
                    continue
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if len(files) >= max_files:
                return files, total_bytes, "max_files"
            if total_bytes + size > max_bytes:
                return files, total_bytes, "max_bytes"
            try:
                arcname = fp.relative_to(target)
            except ValueError:
                continue
            files.append((fp, str(arcname)))
            total_bytes += size
    return files, total_bytes, None


def _handle_folder_download(
    handler,
    parsed,
    *,
    _content_disposition_value,
    _folder_download_collect,
    _folder_zip_max_bytes,
    _folder_zip_max_files,
    bad,
    get_session_for_file_ops,
    j,
    logger,
    open_anchored_fd,
    safe_resolve,
):
    """GET /api/folder/download?session_id=...&path=...

    Streams a zip of <session.workspace>/<path>. Symlinks escaping the
    workspace are skipped. Empty folders return an empty (valid) zip.
    Respects HERMES_WEBUI_FOLDER_ZIP_MAX_MB and HERMES_WEBUI_FOLDER_ZIP_MAX_FILES.
    Pre-flights the walk so size/count failures return a clean 413 with JSON
    body BEFORE any zip bytes are sent.
    """
    import zipfile
    from urllib.parse import parse_qs

    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)

    rel = qs.get("path", [""])[0]
    try:
        target = safe_resolve(Path(s.workspace), rel)
    except ValueError:
        return bad(handler, "invalid path", 400)
    if not target.exists():
        return j(handler, {"error": "not found"}, status=404)
    if not target.is_dir():
        return bad(handler, "path must be a directory; use /api/file/raw for single files", 400)

    workspace_root = Path(s.workspace).resolve()
    max_bytes = _folder_zip_max_bytes()
    max_files = _folder_zip_max_files()

    files, total_bytes, limit_hit = _folder_download_collect(
        target, workspace_root, max_bytes, max_files
    )
    if limit_hit == "max_files":
        return j(handler, {
            "error": "too many files",
            "limit": max_files,
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_FILES",
        }, status=413)
    if limit_hit == "max_bytes":
        return j(handler, {
            "error": "folder too large",
            "limit_bytes": max_bytes,
            "configure": "HERMES_WEBUI_FOLDER_ZIP_MAX_MB",
        }, status=413)

    zip_name = (target.name or "workspace") + ".zip"
    handler.send_response(200)
    handler.send_header("Content-Type", "application/zip")
    handler.send_header(
        "Content-Disposition",
        _content_disposition_value("attachment", zip_name),
    )
    handler.send_header("Cache-Control", "no-store")
    # Under HTTP/1.1 (Handler.protocol_version, see server.py post-#2836)
    # a response with no Content-Length and no Transfer-Encoding requires
    # Connection: close so the client knows the body ends at FIN. The ZIP
    # is built on-the-fly so we cannot send Content-Length up front; mirror
    # the SSE-endpoint pattern #2836 uses. Without this header the client
    # hangs waiting for the next pipelined response after the central
    # directory bytes finish. Caught by Opus pre-release advisor on
    # stage-batch11.
    handler.send_header("Connection", "close")
    handler.end_headers()

    written = 0
    with zipfile.ZipFile(handler.wfile, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for fp, arcname in files:
            fd = None
            try:
                fd = open_anchored_fd(workspace_root, fp.resolve(), want_dir=False)
                info = zipfile.ZipInfo(arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                with os.fdopen(fd, "rb", closefd=True) as src:
                    fd = None
                    with zf.open(info, "w") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                written += 1
            except (ValueError, OSError, PermissionError) as e:
                logger.warning("folder-download: skipping %s: %s", fp, e)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
    logger.info(
        "folder-download: streamed %d/%d files (~%d bytes) from %s",
        written, len(files), total_bytes, target,
    )


def _handle_file_raw(
    handler,
    parsed,
    *,
    MIME_MAP,
    _file_raw_target,
    _serve_file_bytes,
    _serve_inline_html_preview,
    bad,
    get_session_for_file_ops,
    j,
):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    force_download = qs.get("download", [""])[0] == "1"
    resolved = _file_raw_target(s, sid, rel)
    if resolved is None:
        return j(handler, {"error": "not found"}, status=404)
    anchor_root, target = resolved
    ext = target.suffix.lower()
    mime = MIME_MAP.get(ext, "application/octet-stream")
    # Security: force download for dangerous MIME types to prevent XSS.
    # Exception: ?inline=1 permits text/html to be served inline for the
    # sandboxed workspace HTML preview iframe (sandbox="allow-scripts" with no
    # allow-same-origin, so the iframe cannot access parent cookies/storage).
    inline_preview = qs.get("inline", [""])[0] == "1"
    dangerous_types = {"text/html", "application/xhtml+xml", "image/svg+xml"}
    html_inline_ok = inline_preview and mime == "text/html"
    disposition = "attachment" if force_download or (mime in dangerous_types and not html_inline_ok) else "inline"
    # Defense-in-depth for ?inline=1 HTML: even though the workspace.js iframe
    # sets sandbox="allow-scripts", a user could be tricked into opening the
    # ?inline=1 URL directly in a top-level tab (e.g. via a chat link), which
    # would render the HTML in the WebUI's origin without iframe sandbox. The
    # CSP sandbox directive applies the same isolation server-side: without
    # allow-same-origin, the document is treated as a unique opaque origin and
    # cannot read WebUI cookies, localStorage, or postMessage to the parent.
    sandbox_csp = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox"
    csp = sandbox_csp if (inline_preview and not force_download and disposition == "inline") else None
    # _serve_file_bytes sends Content-Security-Policy when csp is set.
    if html_inline_ok:
        return _serve_inline_html_preview(handler, target, "no-store", csp=sandbox_csp, anchor_root=anchor_root)
    return _serve_file_bytes(handler, target, mime, disposition, "no-store", csp=csp, anchor_root=anchor_root)


def _handle_file_read(handler, parsed, *, _sanitize_error, bad, get_session_for_file_ops, j, read_file_content):
    qs = parse_qs(parsed.query)
    sid = qs.get("session_id", [""])[0]
    if not sid:
        return bad(handler, "session_id is required")
    try:
        s = get_session_for_file_ops(sid)
    except KeyError:
        return bad(handler, "Session not found", 404)
    rel = qs.get("path", [""])[0]
    if not rel:
        return bad(handler, "path is required")
    try:
        return j(handler, read_file_content(Path(s.workspace), rel))
    except ImportError as e:
        return bad(handler, str(e), 503)
    except (FileNotFoundError, ValueError) as e:
        return bad(handler, _sanitize_error(e), 404)


def _handle_file_delete(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    require,
    rmtree_anchored,
    safe_resolve,
    unlink_anchored,
):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        # Reject a symlinked entry BEFORE the follow-based exists() check: a
        # dangling symlink resolves to a missing target, so an exists()-first
        # order would misclassify it as 404 "File not found" and leave it
        # permanently undeletable. is_symlink() is a no-follow lstat on the
        # lexically-requested path, so it catches both live and dangling links.
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot delete a symlinked entry")
        if not target.exists():
            return bad(handler, "File not found", 404)
        if target.is_dir():
            if not body.get("recursive"):
                return bad(handler, "Set recursive=true to delete directories")
            rmtree_anchored(ws_root, target)
        else:
            unlink_anchored(ws_root, target)
        return j(handler, {"ok": True, "path": body["path"]})
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_save(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    open_anchored_write_fd,
    require,
    safe_resolve,
):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot save to a symlinked entry")
        if not target.exists():
            return bad(handler, "File not found", 404)
        if target.is_dir():
            return bad(handler, "Cannot save: path is a directory")
        if Path(str(body["path"])).suffix.lower() in {".docx", ".xlsx", ".pptx"}:
            return bad(handler, "Use /api/file/office-save for Office documents")
        data = str(body.get("content", "")).encode("utf-8")
        fd = open_anchored_write_fd(ws_root, target)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
        return j(
            handler, {"ok": True, "path": body["path"], "size": len(data)}
        )
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_office_file_save(
    handler,
    body,
    *,
    _read_anchored_file_bytes,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    open_anchored_write_fd,
    require,
    safe_resolve,
):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot save to a symlinked entry")
        if not target.exists():
            return bad(handler, "File not found", 404)
        if target.is_dir():
            return bad(handler, "Cannot save: path is a directory")
        if Path(str(body["path"])).suffix.lower() not in {".docx", ".xlsx", ".pptx"}:
            return bad(handler, "Office save is only available for .docx, .xlsx, and .pptx files")
        from api.office_documents import save_office_document

        current_bytes = _read_anchored_file_bytes(ws_root, target)
        preview, updated_bytes = save_office_document(body["path"], current_bytes, body.get("content", ""))
        fd = open_anchored_write_fd(ws_root, target)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(updated_bytes)
        preview.update({"ok": True, "path": body["path"], "size": len(updated_bytes)})
        return j(handler, preview)
    except ImportError as e:
        return bad(handler, str(e), 503)
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_create(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    open_anchored_create_fd,
    require,
    safe_resolve,
):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if target.exists():
            return bad(handler, "File already exists")
        data = str(body.get("content", "")).encode("utf-8")
        fd = open_anchored_create_fd(ws_root, target)
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(data)
        return j(
            handler, {"ok": True, "path": target.relative_to(ws_root.resolve()).as_posix()}
        )
    except FileExistsError:
        return bad(handler, "File already exists")
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_rename(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    rename_anchored,
    require,
    safe_resolve,
):
    try:
        require(body, "session_id", "path", "new_name")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        ws_root_resolved = ws_root.resolve()
        source = safe_resolve(ws_root, body["path"])
        # Reject a symlinked entry BEFORE the follow-based exists() check (see
        # _handle_file_delete): a dangling symlink would otherwise 404 and stay
        # unrenameable. is_symlink() is a no-follow lstat on the requested path.
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot rename a symlinked entry")
        if not source.exists():
            return bad(handler, "File not found", 404)
        new_name = body["new_name"].strip()
        if not new_name or "/" in new_name or "\\" in new_name or ".." in new_name:
            return bad(handler, "Invalid file name")
        dest = source.parent / new_name
        if dest.exists():
            return bad(handler, f'A file named "{new_name}" already exists')
        rename_anchored(ws_root, source, dest)
        new_rel = dest.relative_to(ws_root_resolved).as_posix()
        return j(handler, {"ok": True, "old_path": body["path"], "new_path": new_rel})
    except FileExistsError:
        return bad(handler, f'A file named "{body.get("new_name", "")}" already exists')
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_move(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    open_anchored_fd,
    require,
    safe_resolve,
):
    try:
        require(body, "session_id", "path", "dest_dir")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        # safe_resolve() returns paths under the RESOLVED root, so compute
        # returned relative paths against the resolved root too — otherwise a
        # symlinked workspace root (e.g. macOS /tmp -> /private/tmp) makes
        # dest.relative_to(ws_root) raise after a successful on-disk move,
        # returning a confusing 400 for a move that actually happened.
        ws_root_resolved = ws_root.resolve()
        source = safe_resolve(ws_root, body["path"])
        # Reject a symlinked SOURCE entry BEFORE the follow-based exists() check.
        # safe_resolve() follows the final symlink, so source.name/source.parent
        # would point at the link's TARGET, not the dragged entry — moving
        # link.txt would silently move dir/real.txt and leave link.txt dangling.
        # Detect the symlink on the lexically-requested final component (lstat,
        # no-follow) and refuse; running this before exists() also means a
        # dangling symlink is rejected (400) rather than misclassified as 404
        # (matches the delete/rename ordering).
        if (ws_root / body["path"]).is_symlink():
            return bad(handler, "Cannot move a symlinked entry")
        if not source.exists():
            return bad(handler, "File not found", 404)
        dest_dir_raw = (body.get("dest_dir") or ".").strip()
        if not dest_dir_raw:
            dest_dir_raw = "."
        if ".." in dest_dir_raw.split("/"):
            return bad(handler, "Invalid destination")
        dest_parent = safe_resolve(ws_root, dest_dir_raw)
        if not dest_parent.is_dir():
            return bad(handler, "Destination folder not found", 404)
        if source.is_dir():
            try:
                dest_parent.resolve().relative_to(source.resolve())
                return bad(handler, "Cannot move a folder into itself or its subfolder")
            except ValueError:
                pass
        dest = dest_parent / source.name
        if dest.resolve() == source.resolve():
            new_rel = source.relative_to(ws_root_resolved).as_posix()
            return j(
                handler,
                {"ok": True, "old_path": body["path"], "new_path": new_rel},
            )
        # Perform the move race-safely. The path-based checks above can be raced
        # (TOCTOU): between validating dest_parent and renaming, dest_dir could be
        # swapped to a symlink pointing outside the workspace, and a path-based
        # rename would follow it. Open BOTH parent directories as workspace-anchored
        # fds (openat + O_NOFOLLOW — every component verified non-symlink), do the
        # collision check by fd, then rename via src_dir_fd/dst_dir_fd so the kernel
        # operates on the verified directories, not re-resolved pathnames.
        leaf = source.name
        if os.open in getattr(os, "supports_dir_fd", set()):
            src_parent_fd = open_anchored_fd(ws_root, source.parent, want_dir=True)
            try:
                dst_parent_fd = open_anchored_fd(ws_root, dest_parent, want_dir=True)
                try:
                    try:
                        os.stat(leaf, dir_fd=dst_parent_fd, follow_symlinks=False)
                        return bad(
                            handler,
                            f'A file named "{leaf}" already exists in that folder',
                        )
                    except FileNotFoundError:
                        pass
                    os.rename(
                        leaf, leaf,
                        src_dir_fd=src_parent_fd, dst_dir_fd=dst_parent_fd,
                    )
                finally:
                    os.close(dst_parent_fd)
            finally:
                os.close(src_parent_fd)
        else:
            # Windows / no openat: no new race protection available, but creating
            # symlinks needs admin there. Fall back to the path-based rename.
            if dest.exists():
                return bad(
                    handler,
                    f'A file named "{source.name}" already exists in that folder',
                )
            source.rename(dest)
        new_rel = dest.relative_to(ws_root_resolved).as_posix()
        return j(
            handler,
            {"ok": True, "old_path": body["path"], "new_path": new_rel},
        )
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_create_dir(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    make_anchored_dir,
    require,
    safe_resolve,
):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        ws_root = Path(s.workspace)
        target = safe_resolve(ws_root, body["path"])
        if target.exists():
            return bad(handler, "Path already exists")
        make_anchored_dir(ws_root, target)
        return j(
            handler, {"ok": True, "path": target.relative_to(ws_root.resolve()).as_posix()}
        )
    except (ValueError, FileNotFoundError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_reveal(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    platform,
    require,
    safe_resolve,
):
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        target = safe_resolve(Path(s.workspace), body["path"])
        if not target.exists():
            # Include the resolved server-side path in the error message so
            # the frontend toast can show *which* file the system expected.
            # Useful when a stale session row still references a deleted file
            # (#1764 — Cygnus's screenshot showed a "Failed to reveal: not
            # found" toast that dropped the path entirely, leaving no clue
            # what was missing).
            return bad(handler, f"File not found: {target}", 404)

        target_str = str(target)

        # Optional Docker host/container path translation (mirrors _handle_file_open_vscode).
        from api.config import get_config as _get_cfg  # noqa: PLC0415
        vscode_cfg = _get_cfg().get("vscode", {})
        if not isinstance(vscode_cfg, dict):
            vscode_cfg = {}
        container_prefix = vscode_cfg.get("container_path_prefix", "")
        host_prefix = vscode_cfg.get("host_path_prefix", "")
        if container_prefix and host_prefix:
            _norm = container_prefix.rstrip('/') + '/'
            if target_str.startswith(_norm) or target_str == container_prefix.rstrip('/'):
                target_str = host_prefix + target_str[len(container_prefix):]

        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", "-R", target_str])
        elif system == "Windows":
            subprocess.Popen(["explorer.exe", "/select," + target_str])
        else:
            # Linux / other — open parent directory
            subprocess.Popen(["xdg-open", str(Path(target_str).parent)])

        return j(handler, {"ok": True, "path": body["path"]})
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_path(handler, body, *, _sanitize_error, bad, get_session_for_file_ops, j, require, safe_resolve):
    """Resolve a relative workspace-rooted path into an absolute on-disk path.

    The right-click "Copy file path" action (#1764) wants to put the
    absolute path on the user's clipboard so they can paste it into a
    terminal, editor, or anywhere else without having to round-trip through
    the OS file browser. The frontend can't compute the absolute path on
    its own — `safe_resolve` joins against the session's workspace root
    which only the server knows. The handler here is a thin lookup; no
    filesystem mutation, no OS-specific dispatch. We do NOT require the
    target to exist (unlike `_handle_file_reveal`) — copying the path of a
    just-deleted file is still useful, and refusing would force callers
    to special-case 404s for an action that cannot fail destructively.
    """
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        target = safe_resolve(Path(s.workspace), body["path"])
        return j(handler, {"ok": True, "path": str(target)})
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_file_open_vscode(
    handler,
    body,
    *,
    _sanitize_error,
    bad,
    get_session_for_file_ops,
    j,
    require,
    safe_resolve,
):
    """Open a workspace file or folder in VS Code (#2735).

    Reads optional ``vscode`` config block from config.yaml:

        vscode:
          command: code          # executable on PATH; defaults to "code"
          host_path_prefix: /home/user/projects       # Docker host path
          container_path_prefix: /app/workspace       # matching container path

    If ``host_path_prefix`` and ``container_path_prefix`` are both set,
    paths that begin with ``container_path_prefix`` are translated to the
    host prefix before being handed to VS Code.  This lets users running
    Hermes WebUI inside Docker still open files in their local editor.
    """
    try:
        require(body, "session_id", "path")
    except ValueError as e:
        return bad(handler, str(e))
    try:
        s = get_session_for_file_ops(body["session_id"])
    except KeyError:
        return bad(handler, "Session not found", 404)
    try:
        target = safe_resolve(Path(s.workspace), body["path"])
        if not target.exists():
            return bad(handler, f"File not found: {target}", 404)

        target_str = str(target)

        # Optional Docker host/container path translation
        from api.config import get_config as _get_cfg  # noqa: PLC0415
        vscode_cfg = _get_cfg().get("vscode", {})
        if not isinstance(vscode_cfg, dict):
            vscode_cfg = {}
        container_prefix = vscode_cfg.get("container_path_prefix", "")
        host_prefix = vscode_cfg.get("host_path_prefix", "")
        if container_prefix and host_prefix:
            _norm = container_prefix.rstrip('/') + '/'
            if target_str.startswith(_norm) or target_str == container_prefix.rstrip('/'):
                target_str = host_prefix + target_str[len(container_prefix):]

        cmd = vscode_cfg.get("command", "code")
        # Resolve the command to an absolute path so subprocess.Popen finds it
        # even when the server process inherits a minimal PATH (e.g. when
        # launched via start.sh on macOS where /usr/local/bin may be absent).
        resolved_cmd = shutil.which(cmd)
        if resolved_cmd is None:
            # Try common VS Code installation paths as fallback.
            # macOS: /usr/local/bin/code (symlink) or app bundle CLI
            # Linux: /usr/bin/code or snap
            # Windows: user-install under %LOCALAPPDATA%, system-install under %PROGRAMFILES%
            _local_app_data = os.environ.get("LOCALAPPDATA", "")
            _prog_files = os.environ.get("PROGRAMFILES", "C:\\Program Files")
            _prog_files_x86 = os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")
            _vscode_fallbacks = [
                # macOS
                "/usr/local/bin/code",
                "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
                # Linux
                "/usr/bin/code",
                "/snap/bin/code",
                # Windows (user install)
                os.path.join(_local_app_data, "Programs", "Microsoft VS Code", "bin", "code.cmd"),
                # Windows (system install)
                os.path.join(_prog_files, "Microsoft VS Code", "bin", "code.cmd"),
                os.path.join(_prog_files_x86, "Microsoft VS Code", "bin", "code.cmd"),
            ]
            for fb in _vscode_fallbacks:
                if fb and Path(fb).exists():
                    resolved_cmd = fb
                    break
        if resolved_cmd is None:
            return bad(
                handler,
                f"VS Code command not found: {cmd!r}. "
                "Install VS Code and ensure the 'code' CLI is on PATH, "
                "or set vscode.command in config.yaml to the full path.",
            )
        subprocess.Popen([resolved_cmd, target_str])

        return j(handler, {"ok": True, "path": body["path"]})
    except (ValueError, PermissionError, OSError) as e:
        return bad(handler, _sanitize_error(e))


def _handle_workspace_add(
    handler,
    body,
    *,
    _home_path,
    _is_blocked_system_path,
    _is_within,
    _sanitize_error,
    _strip_surrounding_quotes,
    bad,
    j,
    load_workspaces,
    save_workspaces,
    validate_workspace_to_add,
):
    path_str = _strip_surrounding_quotes(body.get("path", "").strip())
    name = body.get("name", "").strip()
    auto_create = body.get("create", False)
    if not path_str:
        return bad(handler, "path is required")
    # Validate the path is NOT a blocked system root BEFORE any filesystem mutation.
    # This prevents creating orphan directories on rejected paths (#782 review).
    # _is_blocked_system_path honours user-tmp carve-outs (e.g. /var/folders on
    # macOS) so pytest's tmp_path_factory paths and other legit user-tmp dirs
    # still register cleanly.
    try:
        candidate = Path(path_str).expanduser().resolve()
    except (ValueError, OSError, RuntimeError) as e:
        # Invalid path (e.g. embedded null byte) — fail closed with a clean 400
        # instead of letting .resolve() raise an uncaught 500.
        return bad(handler, f"Invalid path: {_sanitize_error(e)}")
    if _is_blocked_system_path(candidate):
        # Home-directory carve-out, mirroring the validators
        # (resolve_trusted_workspace / validate_workspace_to_add): a workspace
        # at or under the active user's home must stay allowed even when that
        # home lives under an otherwise-blocked root (e.g. systemd-homed
        # /var/home/<user>/...). Without this the route rejects valid
        # /var/home workspaces before validate_workspace_to_add()'s carve-out
        # can run.
        _home = _home_path()
        if not (_home != Path("/") and (candidate == _home or _is_within(candidate, _home))):
            return bad(handler, f"Path points to a system directory: {candidate}")
    # Now safe to create the directory if requested
    if auto_create:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            return bad(handler, f"Could not create directory: {_sanitize_error(e)}")
    # Full validation (exists, is_dir) — should pass now that dir exists
    try:
        p = validate_workspace_to_add(path_str)
    except ValueError as e:
        return bad(handler, str(e))
    wss = load_workspaces()
    if any(w["path"] == str(p) for w in wss):
        return bad(handler, "Workspace already in list")
    wss.append({"path": str(p), "name": name or p.name})
    save_workspaces(wss)
    return j(handler, {"ok": True, "workspaces": wss})


def _handle_workspace_remove(handler, body, *, bad, j, load_workspaces, save_workspaces):
    path_str = body.get("path", "").strip()
    if not path_str:
        return bad(handler, "path is required")
    wss = load_workspaces()
    wss = [w for w in wss if w["path"] != path_str]
    save_workspaces(wss)
    return j(handler, {"ok": True, "workspaces": wss})


def _handle_workspace_rename(handler, body, *, bad, j, load_workspaces, save_workspaces):
    path_str = body.get("path", "").strip()
    name = body.get("name", "").strip()
    if not path_str or not name:
        return bad(handler, "path and name are required")
    wss = load_workspaces()
    for w in wss:
        if w["path"] == path_str:
            w["name"] = name
            break
    else:
        return bad(handler, "Workspace not found", 404)
    save_workspaces(wss)
    return j(handler, {"ok": True, "workspaces": wss})


def _handle_workspace_reorder(handler, body, *, bad, j, load_workspaces, save_workspaces):
    """Reorder workspaces by providing an ordered list of paths.

    Accepts {"paths": ["path1", "path2", ...]}. The workspaces list is
    rewritten so that entries appear in the given order. Any workspace
    not included in the request is appended at the end (preserves data).
    """
    paths = body.get("paths", [])
    if not paths or not isinstance(paths, list):
        return bad(handler, "paths is required and must be a list")
    wss = load_workspaces()
    by_path = {w["path"]: w for w in wss}
    # Build reordered list: given order first, then any omitted entries
    reordered = []
    seen = set()
    for p in paths:
        p = p.strip()
        if p in by_path and p not in seen:
            reordered.append(by_path[p])
            seen.add(p)
    # Append any workspaces not mentioned (safety net)
    for w in wss:
        if w["path"] not in seen:
            reordered.append(w)
    save_workspaces(reordered)
    return j(handler, {"ok": True, "workspaces": reordered})
