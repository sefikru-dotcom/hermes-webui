"""File operations endpoint with route-owned anchor seams."""
import json
import os
from pathlib import Path
from urllib.parse import parse_qs


def handle_file_operations(handler, parsed, *, deps):
    """Handle file operations with injected route seams."""
    
    # Import dependencies - these will be injected by the caller
    _handle_list_dir = deps.get("_handle_list_dir")
    _handle_escape_authorize = deps.get("_handle_escape_authorize")
    _handle_escape_list_dir = deps.get("_handle_escape_list_dir")
    _handle_escape_file_read = deps.get("_handle_escape_file_read")
    _handle_escape_file_raw = deps.get("_handle_escape_file_raw")
    _handle_file_raw = deps.get("_handle_file_raw")
    _handle_file_read = deps.get("_handle_file_read")
    bad = deps.get("bad")
    j = deps.get("j")
    
    # Route to appropriate handler based on path
    path = parsed.path or ""
    
    if path == "/api/list-dir" and _handle_list_dir:
        return _handle_list_dir(handler, parsed)
    
    elif path == "/api/escape/authorize" and _handle_escape_authorize:
        return _handle_escape_authorize(handler, parsed)
    
    elif path == "/api/escape/list-dir" and _handle_escape_list_dir:
        return _handle_escape_list_dir(handler, parsed)
    
    elif path == "/api/escape/file-read" and _handle_escape_file_read:
        return _handle_escape_file_read(handler, parsed)
    
    elif path == "/api/escape/file-raw" and _handle_escape_file_raw:
        return _handle_escape_file_raw(handler, parsed)
    
    elif path == "/api/file/raw" and _handle_file_raw:
        return _handle_file_raw(handler, parsed)
    
    elif path == "/api/file/read" and _handle_file_read:
        return _handle_file_read(handler, parsed)
    
    else:
        return bad("Unknown file operation endpoint", 404) if bad else None


def handle_file_mutations(handler, parsed, *, deps):
    """Handle file mutations with injected route seams."""
    
    # Import dependencies - these will be injected by the caller
    _handle_file_delete = deps.get("_handle_file_delete")
    _handle_file_save = deps.get("_handle_file_save")
    _handle_office_file_save = deps.get("_handle_office_file_save")
    _handle_file_create = deps.get("_handle_file_create")
    _handle_file_rename = deps.get("_handle_file_rename")
    _handle_file_move = deps.get("_handle_file_move")
    _handle_file_reveal = deps.get("_handle_file_reveal")
    _handle_file_path = deps.get("_handle_file_path")
    _handle_file_open_vscode = deps.get("_handle_file_open_vscode")
    _handle_workspace_add = deps.get("_handle_workspace_add")
    _handle_workspace_remove = deps.get("_handle_workspace_remove")
    _handle_workspace_rename = deps.get("_handle_workspace_rename")
    _handle_workspace_reorder = deps.get("_handle_workspace_reorder")
    bad = deps.get("bad")
    j = deps.get("j")
    
    # Read body for POST operations
    if handler.command == "POST":
        try:
            length = int(handler.headers.get("Content-Length", "0"))
            raw = handler.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except Exception:
                body = {}
        except (ValueError, OverflowError):
            body = {}
    else:
        body = {}
    
    # Route to appropriate handler based on path
    path = parsed.path or ""
    
    if path == "/api/file/delete" and _handle_file_delete:
        return _handle_file_delete(handler, body)
    
    elif path == "/api/file/save" and _handle_file_save:
        return _handle_file_save(handler, body)
    
    elif path == "/api/file/office-save" and _handle_office_file_save:
        return _handle_office_file_save(handler, body)
    
    elif path == "/api/file/create" and _handle_file_create:
        return _handle_file_create(handler, body)
    
    elif path == "/api/file/rename" and _handle_file_rename:
        return _handle_file_rename(handler, body)
    
    elif path == "/api/file/move" and _handle_file_move:
        return _handle_file_move(handler, body)
    
    elif path == "/api/file/reveal" and _handle_file_reveal:
        return _handle_file_reveal(handler, body)
    
    elif path == "/api/file/path" and _handle_file_path:
        return _handle_file_path(handler, body)
    
    elif path == "/api/file/open-vscode" and _handle_file_open_vscode:
        return _handle_file_open_vscode(handler, body)
    
    elif path == "/api/workspace/add" and _handle_workspace_add:
        return _handle_workspace_add(handler, body)
    
    elif path == "/api/workspace/remove" and _handle_workspace_remove:
        return _handle_workspace_remove(handler, body)
    
    elif path == "/api/workspace/rename" and _handle_workspace_rename:
        return _handle_workspace_rename(handler, body)
    
    elif path == "/api/workspace/reorder" and _handle_workspace_reorder:
        return _handle_workspace_reorder(handler, body)
    
    else:
        return bad("Unknown endpoint", 404) if bad else None
