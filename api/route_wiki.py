"""Read-only LLM Wiki routes."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs


def handle_wiki_get(handler, parsed, *, deps):
    """Handle wiki status, browse, and page reads with injected route seams."""
    _LLM_WIKI_MAX_PAGE_BYTES = deps["_LLM_WIKI_MAX_PAGE_BYTES"]
    _handle_llm_wiki_status = deps["_handle_llm_wiki_status"]
    _llm_wiki_allowlisted_entries = deps["_llm_wiki_allowlisted_entries"]
    _llm_wiki_resolve_path = deps["_llm_wiki_resolve_path"]
    _skill_path_within = deps["_skill_path_within"]
    bad = deps["bad"]
    j = deps["j"]

    if parsed.path == "/api/wiki/status":
        return _handle_llm_wiki_status(handler, parsed)
    if parsed.path == "/api/wiki/browse":
        wiki_root, _, _ = _llm_wiki_resolve_path()
        if not wiki_root or not os.path.isdir(wiki_root):
            return bad(handler, "Wiki not configured or directory not found", status=404)
        allowlisted_entries = _llm_wiki_allowlisted_entries(Path(wiki_root))
        pages = []
        for rel_path, (fp, identity) in sorted(allowlisted_entries.items(), key=lambda item: item[0].lower()):
            try:
                st = fp.stat()
            except OSError:
                continue
            if (st.st_dev, st.st_ino) != identity:
                continue
            pages.append({"name": Path(rel_path).name, "path": rel_path, "size": st.st_size, "mtime": int(st.st_mtime)})
        return j(handler, {"pages": pages})
    if parsed.path == "/api/wiki/page":
        wiki_root, _, _ = _llm_wiki_resolve_path()
        page_path = parse_qs(parsed.query or "").get("path", [""])[0]
        if not wiki_root or not page_path:
            return bad(handler, "Wiki not configured or path not provided", status=400)
        if "\\" in page_path:
            return bad(handler, "Invalid path", status=400)
        # Reject a real `..` path SEGMENT (or absolute path), not the bare
        # substring — a legitimate listed filename like `v1..v2.md` contains
        # ".." without being traversal. Containment + the resolved-allowlist
        # membership check below are the actual security boundary.
        requested_key = page_path.replace("\\", "/")
        _page_parts = requested_key.split("/")
        if os.path.isabs(page_path) or any(part == ".." for part in _page_parts):
            return bad(handler, "Invalid path", status=400)
        if any(part in ("", ".") for part in _page_parts):
            return bad(handler, "Invalid path", status=400)
        full_path = Path(os.path.join(wiki_root, page_path))
        if not _skill_path_within(Path(wiki_root), full_path):
            return bad(handler, "Invalid path", status=400)
        try:
            wiki_real = Path(wiki_root).resolve()
        except OSError:
            return bad(handler, "Page not found", status=404)
        # Only serve files the browse/list path would surface (same allowlist:
        # *.md under the wiki page-dirs, no dotfiles, forbidden-roots guard).
        # Without this the read endpoint could return ANY file inside the wiki
        # root (e.g. .env / .git/config / non-.md), since containment alone
        # doesn't constrain which files are readable (Opus review finding).
        # Capture each allowlisted page's STABLE IDENTITY (st_dev, st_ino) so the
        # post-open fstat below can detect a file/parent-dir swapped in after the
        # allowlist check (TOCTOU write-race, Codex finding) — a pathname re-open
        # alone can't, since O_NOFOLLOW only guards the final component, not a
        # swapped parent directory.
        allowed_identity = _llm_wiki_allowlisted_entries(wiki_real)
        try:
            resolved_target = full_path.resolve()
        except OSError:
            return bad(handler, "Page not found", status=404)
        requested_entry = allowed_identity.get(requested_key)
        if requested_entry is None:
            return bad(handler, "Page not found", status=404)
        allowlisted_target, allowlisted_identity = requested_entry
        if resolved_target != allowlisted_target:
            return bad(handler, "Page not found", status=404)
        # Read the ALREADY-RESOLVED, allowlisted real path with O_NOFOLLOW so a
        # symlink swapped in for the final component between the allowlist check
        # and the read is refused rather than followed. Then fstat the open fd
        # and require its (st_dev, st_ino) to match the identity captured during
        # allowlisting — this closes a parent-directory swap that O_NOFOLLOW
        # would otherwise follow. Any mismatch / vanished / swapped page returns
        # a clean 404, never a 500.
        try:
            fd = os.open(str(resolved_target), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                st_open = os.fstat(fd)
                if (st_open.st_dev, st_open.st_ino) != allowlisted_identity:
                    return bad(handler, "Page not found", status=404)
                raw = os.read(fd, _LLM_WIKI_MAX_PAGE_BYTES + 1)
            finally:
                os.close(fd)
            if len(raw) > _LLM_WIKI_MAX_PAGE_BYTES:
                raw = raw[:_LLM_WIKI_MAX_PAGE_BYTES]
            content = raw.decode("utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            return bad(handler, "Page not found", status=404)
        except OSError:
            # ELOOP (symlink swapped in under O_NOFOLLOW) or any other read
            # failure → clean 404, never a 500.
            return bad(handler, "Could not read page", status=404)
        return j(handler, {"content": content, "path": page_path})
    return None
