"""Profile memory and read-only project context handlers."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from urllib.parse import parse_qs

_PROJECT_CONTEXT_HERMES_NAMES = (".hermes.md", "HERMES.md")
# Mirror the agent's lowercase filename variants (agents.md / claude.md) so the
# tab does not under-report on case-sensitive filesystems.
_PROJECT_CONTEXT_CWD_NAMES = (
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "claude.md",
    ".cursorrules",
)
# Modern Cursor rules live in a directory of .mdc files, which the agent also loads.
_PROJECT_CONTEXT_CURSOR_RULES_GLOB = ".cursor/rules/*.mdc"
_PROJECT_CONTEXT_MAX_BYTES = 20_000


def _strip_project_context_frontmatter(content: str) -> str:
    """Strip a leading YAML frontmatter block, mirroring the agent's loader.

    The agent removes a leading ``---\\n ... \\n---`` block before injecting a
    context file. Without this the tab would display frontmatter the agent never
    sends to the model.
    """
    if not content.startswith("---"):
        return content
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return "".join(lines[idx + 1:]).lstrip("\n")
    return content



def _project_context_git_root(start: Path) -> Path | None:
    """Return the nearest git root for project context discovery."""
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None



def _project_context_candidates(
    workspace: Path,
    *,
    _PROJECT_CONTEXT_CURSOR_RULES_GLOB,
    _PROJECT_CONTEXT_CWD_NAMES,
    _PROJECT_CONTEXT_HERMES_NAMES,
    _project_context_git_root,
) -> list[Path]:
    """Mirror the agent's first-match project context file priority.

    #4164: in a non-git workspace the agent's ``_find_hermes_md`` walks all
    the way up to filesystem root because its ``stop_at = git_root`` is
    ``None`` — so a workspace at ``/tmp/x/project/subdir`` could surface
    ``/tmp/x/HERMES.md`` (a file *outside* the user's workspace) in the
    Project Context tab.

    The WebUI tab is a read-only mirror of what the agent injects, so the
    safest bound that does not over-promise is: when there is no git root,
    treat the workspace itself as the stop boundary. The cwd is still
    scanned (preserving the in-workspace AGENTS.md / HERMES.md behavior),
    but we no longer walk into the user's home directory or ``/tmp``.

    The agent-side walk in ``agent/prompt_builder._find_hermes_md`` should
    be bounded the same way for full parity; until that ships the WebUI
    will under-report context files that live *above* a non-git workspace,
    which is strictly less surprising than over-reporting them.
    """
    cwd = workspace.resolve()
    candidates: list[Path] = []
    git_root = _project_context_git_root(cwd)
    # When inside a git tree, walk up to the git root as before. When not,
    # bound the walk at the workspace itself so we never surface files
    # above the user's workspace.
    stop_at = git_root if git_root is not None else cwd

    for directory in [cwd, *cwd.parents]:
        for name in _PROJECT_CONTEXT_HERMES_NAMES:
            candidates.append(directory / name)
        if directory == stop_at:
            break

    for name in _PROJECT_CONTEXT_CWD_NAMES:
        candidates.append(cwd / name)

    # Modern Cursor rules: the agent reads .cursor/rules/*.mdc in addition to
    # the legacy .cursorrules file. Sort for deterministic ordering.
    try:
        candidates.extend(sorted(cwd.glob(_PROJECT_CONTEXT_CURSOR_RULES_GLOB)))
    except OSError:
        pass

    return candidates



def _memory_project_context_workspace(
    parsed,
    *,
    get_last_workspace,
    get_session,
    logger,
    resolve_trusted_workspace,
) -> Path | None:
    qs = parse_qs(parsed.query or "") if parsed is not None else {}
    sid = qs.get("session_id", [""])[0]
    if sid:
        try:
            # A blank session workspace (freshly-created/draft sessions) must not
            # fall through to Path("").resolve(), which returns the server's own
            # CWD and would surface the install's AGENTS.md/HERMES.md as if it
            # were the user's project context.
            ws = (get_session(sid).workspace or "").strip()
            if not ws:
                return None
            return Path(ws).expanduser().resolve()
        except Exception:
            return None

    raw_workspace = qs.get("workspace", [""])[0] or os.environ.get("TERMINAL_CWD", "") or get_last_workspace()
    if not raw_workspace:
        return None
    try:
        return Path(resolve_trusted_workspace(raw_workspace)).expanduser().resolve()
    except Exception:
        logger.debug("Skipping project context for untrusted workspace %s", raw_workspace, exc_info=True)
        return None



def _read_active_project_context(
    workspace: Path | None,
    *,
    _PROJECT_CONTEXT_MAX_BYTES,
    _project_context_candidates,
    _strip_project_context_frontmatter,
    logger,
) -> dict:
    payload = {
        "content": "",
        "path": "",
        "mtime": None,
        "workspace": str(workspace) if workspace else "",
        "shadowed": [],
    }
    if not workspace:
        return payload
    try:
        if not workspace.exists() or not workspace.is_dir():
            return payload
    except OSError:
        return payload

    seen: set[str] = set()
    readable: list[dict] = []
    for candidate in _project_context_candidates(workspace):
        try:
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            key = os.path.normcase(str(resolved)).casefold()
            if key in seen:
                continue
            seen.add(key)
            content = resolved.read_text(encoding="utf-8", errors="replace")
            # Mirror what the agent actually injects: strip YAML frontmatter and
            # cap each source, so the tab reports the effective context rather
            # than raw file bytes the agent never sends to the model.
            content = _strip_project_context_frontmatter(content)
            if len(content) > _PROJECT_CONTEXT_MAX_BYTES:
                content = content[:_PROJECT_CONTEXT_MAX_BYTES]
            if not content.strip():
                continue
            readable.append(
                {
                    "name": resolved.name,
                    "path": str(resolved),
                    "content": content,
                    "mtime": resolved.stat().st_mtime,
                }
            )
        except Exception:
            logger.debug("Could not read project context candidate %s", candidate, exc_info=True)

    if not readable:
        return payload

    active = readable[0]
    payload.update(
        {
            "content": active["content"],
            "path": active["path"],
            "mtime": active["mtime"],
            "name": active["name"],
            "shadowed": [
                {
                    "name": item["name"],
                    "path": item["path"],
                    "mtime": item["mtime"],
                    "shadowed_by": active["name"],
                    "shadowed_by_path": active["path"],
                }
                for item in readable[1:]
            ],
        }
    )
    return payload



def _handle_memory_read(
    handler,
    parsed=None,
    *,
    _external_notes_sources_enabled,
    _memory_project_context_workspace,
    _read_active_project_context,
    _redact_text,
    _webui_truthy,
    get_config_snapshot,
    j,
):
    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        mem_dir = home / "memories"
    except ImportError:
        home = Path.home() / ".hermes"
        mem_dir = home / "memories"

    # Respect memory_enabled and user_profile_enabled config flags (#6406)
    # Use get_config_snapshot() for per-profile isolation — get_config() returns
    # the process-global mutable _cfg_cache which races across profiles.
    # The flags are nested under cfg["memory"] in Hermes Agent's schema.
    cfg = get_config_snapshot()
    mem = cfg.get("memory") if isinstance(cfg, dict) else None
    mem_cfg = mem if isinstance(mem, dict) else {}
    memory_enabled = _webui_truthy(mem_cfg.get("memory_enabled", True))
    user_profile_enabled = _webui_truthy(mem_cfg.get("user_profile_enabled", True))

    mem_file = mem_dir / "MEMORY.md" if memory_enabled else None
    user_file = mem_dir / "USER.md" if user_profile_enabled else None
    soul_file = home / "SOUL.md"
    memory = (
        mem_file.read_text(encoding="utf-8", errors="replace")
        if mem_file and mem_file.exists()
        else ""
    )
    user = (
        user_file.read_text(encoding="utf-8", errors="replace")
        if user_file and user_file.exists()
        else ""
    )
    soul = (
        soul_file.read_text(encoding="utf-8", errors="replace")
        if soul_file.exists()
        else ""
    )
    project_context = _read_active_project_context(_memory_project_context_workspace(parsed))
    return j(
        handler,
        {
            "memory": _redact_text(memory),
            "user": _redact_text(user),
            "soul": _redact_text(soul),
            "project_context": _redact_text(project_context["content"]),
            "memory_path": str(mem_file) if mem_file else "",
            "user_path": str(user_file) if user_file else "",
            "soul_path": str(soul_file),
            "project_context_path": project_context["path"],
            "project_context_name": project_context.get("name", ""),
            "project_context_workspace": project_context["workspace"],
            "memory_mtime": mem_file.stat().st_mtime if mem_file and mem_file.exists() else None,
            "user_mtime": user_file.stat().st_mtime if user_file and user_file.exists() else None,
            "soul_mtime": soul_file.stat().st_mtime if soul_file.exists() else None,
            "project_context_mtime": project_context["mtime"],
            "project_context_shadowed": project_context["shadowed"],
            "external_notes_enabled": _external_notes_sources_enabled(cfg),
        },
    )



def _handle_memory_write(handler, body, *, _webui_truthy, bad, get_config_snapshot, j, require):
    try:
        require(body, "section", "content")
    except ValueError as e:
        return bad(handler, str(e))
    section = body["section"]

    # Respect memory_enabled and user_profile_enabled config flags (#6406)
    # Use get_config_snapshot() for per-profile isolation — get_config() returns
    # the process-global mutable _cfg_cache which races across profiles.
    # The flags are nested under cfg["memory"] in Hermes Agent's schema.
    cfg = get_config_snapshot()
    mem = cfg.get("memory") if isinstance(cfg, dict) else None
    mem_cfg = mem if isinstance(mem, dict) else {}
    if section == "memory":
        if not _webui_truthy(mem_cfg.get("memory_enabled", True)):
            return bad(handler, "Memory is disabled by configuration (memory_enabled: false)", 403)
    elif section == "user":
        if not _webui_truthy(mem_cfg.get("user_profile_enabled", True)):
            return bad(handler, "User profile is disabled by configuration (user_profile_enabled: false)", 403)

    try:
        from api.profiles import get_active_hermes_home

        home = get_active_hermes_home()
        mem_dir = home / "memories"
    except ImportError:
        home = Path.home() / ".hermes"
        mem_dir = home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if section == "memory":
        target = mem_dir / "MEMORY.md"
    elif section == "user":
        target = mem_dir / "USER.md"
    elif section == "soul":
        target = home / "SOUL.md"
    else:
        return bad(handler, 'section must be "memory", "user", or "soul"')
    # Refuse to write through a symlinked target file: a symlink planted at the
    # memory path (e.g. via a restored/imported workspace) would otherwise let a
    # memory write clobber an arbitrary file outside the memories directory. This
    # mirrors the symlink-rejection hardening already shipped for skills/plugins
    # (#4217/#4234/#4240).
    if target.is_symlink():
        return bad(handler, "Cannot write to a symlinked memory file")
    try:
        target.write_text(body["content"], encoding="utf-8")
    except OSError as exc:
        if not isinstance(exc, PermissionError) and getattr(exc, "errno", None) != errno.EROFS:
            raise
        mode_hint = ""
        try:
            mode_hint = f" (mode {target.stat().st_mode & 0o777:o})"
        except OSError:
            pass
        return bad(
            handler,
            (
                f"{target.name} is not writable{mode_hint}: {target}. "
                "Run chmod 644 on the file or fix ownership on the shared volume."
            ),
            403,
        )
    return j(handler, {"ok": True, "section": section, "path": str(target)})
