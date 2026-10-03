"""Project OS dashboard helpers extracted from api.routes.

Pure workspace-reading, board-resolution and summary helpers. The HTTP
handler (_handle_project_os_dashboard) stays in api.routes so URL/JSON
contracts and monkeypatch tests remain on the route module; routes
re-exports these names with identity preserved.
"""

from __future__ import annotations

import json
from pathlib import Path

from api.workspace import read_file_content

def _project_os_workspace_read(repo_root: Path, rel: str) -> dict | None:
    try:
        return read_file_content(repo_root, rel)
    except Exception:
        return None


def _project_os_workspace_json(repo_root: Path, rel: str) -> dict | None:
    payload = _project_os_workspace_read(repo_root, rel)
    if not payload or not isinstance(payload.get("content"), str):
        return None
    try:
        parsed = json.loads(payload.get("content") or "")
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _project_os_truth_board_slugs(repo_root: Path) -> set[str]:
    slugs: set[str] = set()

    def add(value) -> None:
        text = str(value or "").strip()
        if text:
            slugs.add(text)

    for rel in (
        ".ax/handoff/current.json",
        ".ax/status/active.json",
        ".ax/status/heartbeat.json",
    ):
        truth = _project_os_workspace_json(repo_root, rel)
        if not isinstance(truth, dict):
            continue
        raw_board = truth.get("board")
        if isinstance(raw_board, dict):
            add(raw_board.get("slug"))
            add(raw_board.get("id"))
            add(raw_board.get("name"))
            add(raw_board.get("display_name"))
        else:
            add(raw_board)
        for key in (
            "selected_board_slug",
            "canonical_backlog_board_id",
            "current_browser_board_id",
            "active_proof_board_id",
            "recover_board_id",
        ):
            add(truth.get(key))
    return slugs


def _project_os_repo_matches_board(repo_root: Path, board_slug: str | None) -> bool:
    slug = str(board_slug or "").strip()
    return bool(slug and slug in _project_os_truth_board_slugs(repo_root))


def _project_os_candidate_repo_roots(workspace_root: Path | None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser().resolve()
        except Exception:
            return
        if not resolved.is_dir():
            return
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            candidates.append(resolved)

    add(workspace_root)

    scan_root = None
    if workspace_root is not None:
        try:
            scan_root = workspace_root.expanduser().resolve()
        except Exception:
            scan_root = None
    if not scan_root or not scan_root.is_dir():
        return candidates

    skip_names = {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "node_modules",
        "vendor",
        "dist",
        "build",
    }
    queue_dirs: list[tuple[Path, int]] = [(scan_root, 0)]
    inspected = 0
    while queue_dirs and inspected < 300:
        current, depth = queue_dirs.pop(0)
        inspected += 1
        if (current / ".ax").is_dir() or (current / "docs" / "project-os").is_dir():
            add(current)
        if depth >= 3:
            continue
        try:
            children = sorted(
                (child for child in current.iterdir() if child.is_dir()),
                key=lambda child: child.name,
            )
        except Exception:
            continue
        for child in children:
            name = child.name
            if name in skip_names or (name.startswith(".") and name != ".ax"):
                continue
            queue_dirs.append((child, depth + 1))
    add(Path.cwd())
    return candidates


def _project_os_resolve_repo_root_for_board(repo_root: Path | None, board_slug: str | None) -> Path | None:
    slug = str(board_slug or "").strip()
    if not slug:
        return repo_root if repo_root and repo_root.exists() else None
    if repo_root and repo_root.exists() and _project_os_repo_matches_board(repo_root, slug):
        return repo_root
    for candidate in _project_os_candidate_repo_roots(repo_root):
        if _project_os_repo_matches_board(candidate, slug):
            return candidate
    return repo_root if repo_root and repo_root.exists() else None


def _project_os_goal_summary(project_md: dict | None, handoff: dict | None, status_md: dict | None, board_name: str | None = None, board_desc: str | None = None) -> str:
    project_text = str((project_md or {}).get("content") or "")
    for line in project_text.splitlines():
        text = line.strip().lstrip("- ").strip()
        if not text or text.startswith("#"):
            continue
        return text[:220]
    handoff_summary = str((handoff or {}).get("goal_summary") or "").strip()
    if handoff_summary:
        return handoff_summary[:220]
    desc = str(board_desc or "").strip()
    if desc:
        return desc[:220]
    status_text = str((status_md or {}).get("content") or "")
    for line in status_text.splitlines():
        text = line.strip().lstrip("- ").strip()
        if not text or text.startswith("#"):
            continue
        return text[:220]
    return str(board_name or "Project OS").strip()[:220]


def _project_os_onboarding_context(repo_root: Path, project_md: dict | None, plan_md: dict | None, status_md: dict | None) -> dict:
    project_text = str((project_md or {}).get("content") or "")
    plan_text = str((plan_md or {}).get("content") or "")
    status_text = str((status_md or {}).get("content") or "")
    merged = "\n".join([project_text, plan_text, status_text])
    is_non_git_workspace = not (repo_root / ".git").exists()
    has_boundary_hold = "TO_BE_VALIDATED_BY_HERMES" in merged
    child_repo_blocked = (
        "auto-promoted" in merged
        or "auto-adopted" in merged
        or "auto-adoption | `금지`" in merged
        or "자동 승격 금지" in merged
        or "canonical repo continuity로 승격하지 않습니다" in merged
    )
    workspace_root_confirmed = str(repo_root) in merged
    if not (is_non_git_workspace and (project_text or plan_text or status_text)):
        return {
            "active": False,
            "doc_source": "project-os",
        }
    status_label = "보류(안전)" if has_boundary_hold else "확인됨"
    summary = "workspace root onboarding 진행 중 · 저장소 경계는 아직 미확정이며 자동 승격은 금지됩니다."
    next_safe_action = "workspace-root 기준으로 경계만 좁게 검증"
    if has_boundary_hold:
        summary = "workspace root onboarding 진행 중 · 저장소 경계는 아직 미확정이며 TO_BE_VALIDATED_BY_HERMES 상태를 유지합니다."
    return {
        "active": True,
        "doc_source": "root",
        "status_label": status_label,
        "summary": summary,
        "next_safe_action": next_safe_action,
        "workspace_root_confirmed": workspace_root_confirmed,
        "repo_boundary_status": "TO_BE_VALIDATED_BY_HERMES" if has_boundary_hold else "confirmed",
        "child_repo_auto_promotion_blocked": bool(child_repo_blocked),
        "guardrails": [
            "workspace root 확인됨" if workspace_root_confirmed else "workspace root 확인 필요",
            "child repo 자동 승격 금지 유지" if child_repo_blocked else "child repo guardrail 확인 필요",
            "repo boundary 미확정 유지" if has_boundary_hold else "repo boundary confirmed",
        ],
    }
