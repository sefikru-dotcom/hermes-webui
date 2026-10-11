"""Profile-scoped skill discovery, views, mutations, and disabled state."""

from __future__ import annotations

import json
import os
from pathlib import Path

def _active_skills_dir() -> Path:
    """Return the skills directory for the request's active Hermes profile.

    WebUI profile switches are cookie/thread-local scoped, so the agent
    module-level ``tools.skills_tool.SKILLS_DIR`` can still point at the server
    startup profile. Skills UI endpoints must derive the directory from
    ``get_active_hermes_home()`` for every request instead of reading that
    process-global constant.
    """
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home()) / "skills"
    except Exception:
        try:
            from tools.skills_tool import SKILLS_DIR

            return Path(SKILLS_DIR)
        except Exception:
            return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser() / "skills"


def _skill_path_within(base_dir: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base_dir.resolve())
        return True
    except (OSError, ValueError):
        return False


def _skill_category_from_path(
    skill_md: Path,
    skills_dirs: list[Path],
    local_skills_dir: Path | None = None,
) -> str | None:
    """Return the UI category for a discovered skill path.

    Flat skills directly under the active *local* skills root stay uncategorized,
    while flat skills under an *external* root use that root's directory name as
    their category. ``local_skills_dir`` identifies the local root explicitly; if
    omitted it falls back to ``skills_dirs[0]`` for backward compatibility, but
    callers should pass it directly because the local root can be filtered out of
    ``skills_dirs`` (e.g. when it does not exist yet on a host with only external
    skills configured), which would otherwise misclassify the first external root
    as local.
    """
    if local_skills_dir is None:
        local_skills_dir = skills_dirs[0] if skills_dirs else None
    for skills_dir in skills_dirs:
        try:
            rel_path = skill_md.relative_to(skills_dir)
        except ValueError:
            continue
        parts = rel_path.parts
        if len(parts) >= 3:
            return parts[0]
        if len(parts) >= 2 and local_skills_dir is not None and skills_dir != local_skills_dir:
            return skills_dir.name
        return None
    return None


def _active_skill_search_dirs(skills_dir: Path) -> list[Path]:
    dirs = [skills_dir]
    try:
        from agent.skill_utils import get_external_skills_dirs

        dirs.extend(Path(p) for p in get_external_skills_dirs())
    except Exception:
        pass
    return [p for p in dirs if p.exists()]


def _active_profile_config_path(*, _get_config_path) -> Path:
    """Return config.yaml for the request's active WebUI profile.

    Skills endpoints are profile-scoped UI actions: both the visible disabled
    toggle state and toggle writes must follow the cookie/thread-local active
    Hermes home, not process-global HERMES_HOME or HERMES_CONFIG_PATH values
    captured at server startup.
    """
    test_override_module = getattr(_get_config_path, "__module__", "")
    if test_override_module != "api.config":
        return _get_config_path()
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home()) / "config.yaml"
    except Exception:
        return _get_config_path()


def _get_disabled_skill_names_for_profile(
    *,
    _active_profile_config_path,
    _load_yaml_config_file,
    _normalize_disabled_set,
) -> set:
    """Read disabled skill names from the active profile's config.yaml.

    Unlike ``tools.skills_tool._get_disabled_skill_names`` which reads from
    the process-global ``HERMES_HOME``, this uses ``_get_config_path()`` which
    resolves against the WebUI's active profile.  Checks
    ``skills.platform_disabled.webui`` first, falling back to
    ``skills.disabled``.
    """
    config_path = _active_profile_config_path()
    if not config_path.exists():
        return set()
    try:
        cfg = _load_yaml_config_file(config_path)
    except Exception:
        return set()
    if not isinstance(cfg, dict):
        return set()
    skills_cfg = cfg.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()
    # Check platform_disabled.webui first (mirrors agent platform resolution)
    platform_disabled = skills_cfg.get("platform_disabled")
    if isinstance(platform_disabled, dict) and "webui" in platform_disabled:
        return _normalize_disabled_set(platform_disabled["webui"])
    return _normalize_disabled_set(skills_cfg.get("disabled"))


def _parse_config_string_list(value) -> list:
    """Decode a config value that may hold a JSON-array string into a list.

    ``hermes config set`` (and JSON-mode editor saves) store lists as quoted
    JSON strings (``'[\"a\",\"b\"]'`` or the Python-literal ``\"['a']\"``), so a
    disabled list read from ``config.yaml`` can arrive as a single string
    instead of a YAML list. Treating it as one literal name makes the Skills
    panel show every skill as enabled and makes the toggle write a destructive
    single-entry list (hermes-webui#7120).

    Reuses ``agent.skill_utils.parse_config_string_list`` (hermes-agent #86661
    fix) when the bundled agent source is importable, and mirrors its logic
    otherwise so the two surfaces cannot drift. A scalar string still means one
    name.
    """
    try:
        from agent.skill_utils import parse_config_string_list

        return parse_config_string_list(value)
    except ImportError:
        pass
    import ast

    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return []


def _normalize_disabled_set(values, *, _parse_config_string_list) -> set:
    """Normalize a YAML disabled list into a set of stripped strings."""
    if values is None:
        return set()
    if isinstance(values, str):
        values = _parse_config_string_list(values)
    return {str(v).strip() for v in values if str(v).strip()}


def _skills_list_from_dir(
    skills_dir: Path,
    category: str | None=None,
    *,
    _active_skill_search_dirs,
    _get_disabled_skill_names_for_profile,
    _skill_category_from_path,
    logger,
) -> dict:
    """List skills using an explicit local skills directory.

    This mirrors ``tools.skills_tool.skills_list`` closely, but keeps the local
    scan root explicit so per-client WebUI profile switches do not race on or
    leak through the skills tool's module-global ``SKILLS_DIR``.
    """
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import (
        MAX_DESCRIPTION_LENGTH,
        _EXCLUDED_SKILL_DIRS,
        _parse_frontmatter,
        _sort_skills,
        skill_matches_platform,
    )

    if not skills_dir.exists():
        skills_dir.mkdir(parents=True, exist_ok=True)
        return {
            "success": True,
            "skills": [],
            "categories": [],
            "message": f"No skills found. Skills directory created at {skills_dir}/",
        }

    all_skills = []
    seen_names: set[str] = set()
    disabled = _get_disabled_skill_names_for_profile()
    search_dirs = _active_skill_search_dirs(skills_dir)

    for scan_dir in search_dirs:
        for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            skill_dir = skill_md.parent
            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
                frontmatter, body = _parse_frontmatter(content)
                if not skill_matches_platform(frontmatter):
                    continue
                name = frontmatter.get("name", skill_dir.name)[:64]
                if name in seen_names:
                    continue
                description = frontmatter.get("description", "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break
                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."
                seen_names.add(name)
                all_skills.append(
                    {
                        "name": name,
                        "description": description,
                        "category": _skill_category_from_path(
                            skill_md, search_dirs, local_skills_dir=skills_dir
                        ),
                        "disabled": name in disabled,
                    }
                )
            except (UnicodeDecodeError, PermissionError) as e:
                logger.debug("Failed to read skill file %s: %s", skill_md, e)
            except Exception as e:
                logger.debug(
                    "Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True
                )

    if category:
        all_skills = [s for s in all_skills if s.get("category") == category]
    all_skills = _sort_skills(all_skills)
    categories = sorted(set(s.get("category") for s in all_skills if s.get("category")))
    result = {
        "success": True,
        "skills": all_skills,
        "categories": categories,
        "count": len(all_skills),
    }
    if all_skills:
        result["hint"] = "Use skill_view(name) to see full content, tags, and linked files"
    else:
        result["message"] = "No skills found in skills/ directory."
    return result


def _find_skill_in_dirs(
    name: str,
    skills_dirs: list[Path],
    *,
    _skill_path_within,
) -> tuple[Path | None, Path | None]:
    """Resolve a WebUI skill name inside explicit skills directories."""
    from agent.skill_utils import iter_skill_index_files
    from tools.skills_tool import _EXCLUDED_SKILL_DIRS, _parse_frontmatter

    raw_name = str(name or "").strip().strip("/")
    if not raw_name:
        return None, None

    candidate_names = [raw_name]
    if ":" in raw_name:
        namespace, bare = raw_name.split(":", 1)
        if namespace and bare:
            candidate_names.append(f"{namespace}/{bare}")

    for skills_dir in skills_dirs:
        if not skills_dir.exists():
            continue
        for candidate_name in candidate_names:
            direct_path = skills_dir / candidate_name
            if not _skill_path_within(skills_dir, direct_path):
                continue
            if direct_path.is_dir() and (direct_path / "SKILL.md").exists():
                return direct_path, direct_path / "SKILL.md"
            legacy_md = direct_path.with_suffix(".md")
            if legacy_md.exists() and _skill_path_within(skills_dir, legacy_md):
                return legacy_md.parent, legacy_md

        for skill_md in iter_skill_index_files(skills_dir, "SKILL.md"):
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            skill_dir = skill_md.parent
            if skill_dir.name == raw_name:
                return skill_dir, skill_md
            try:
                frontmatter, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8")[:4000])
                if frontmatter.get("name") == raw_name:
                    return skill_dir, skill_md
            except Exception:
                continue

        for legacy_md in skills_dir.rglob("*.md"):
            if legacy_md.name == "SKILL.md":
                continue
            if legacy_md.stem == raw_name and _skill_path_within(skills_dir, legacy_md):
                return legacy_md.parent, legacy_md
    return None, None


def _find_skill_in_dir(name: str, skills_dir: Path, *, _find_skill_in_dirs) -> tuple[Path | None, Path | None]:
    """Resolve a WebUI skill name inside an explicit skills directory."""
    return _find_skill_in_dirs(name, [skills_dir])


def _skill_not_found_payload(
    name: str,
    skills_dir: Path,
    *,
    _SKILL_NOT_FOUND_LIST_LIMIT,
    _skills_list_from_dir,
) -> dict:
    all_names = [s["name"] for s in _skills_list_from_dir(skills_dir).get("skills", [])]
    total = len(all_names)
    available = all_names[:_SKILL_NOT_FOUND_LIST_LIMIT]
    truncated = total > len(available)
    hint = "Use skills_list to see all available skills"
    if truncated:
        hint = f"Showing {len(available)} of {total} skills. {hint}"
    return {
        "success": False,
        "error": f"Skill '{name}' not found.",
        "available_skills": available,
        "available_skills_truncated": truncated,
        "total_skills": total,
        "hint": hint,
    }


def _linked_files_for_skill(skill_dir: Path | None) -> dict:
    if not skill_dir or not (skill_dir / "SKILL.md").exists():
        return {}
    linked_files: dict[str, list[str]] = {}

    references_dir = skill_dir / "references"
    if references_dir.exists():
        refs = [str(f.relative_to(skill_dir)) for f in references_dir.glob("*.md")]
        if refs:
            linked_files["references"] = sorted(refs)

    templates_dir = skill_dir / "templates"
    if templates_dir.exists():
        templates = []
        for ext in ["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"]:
            templates.extend(str(f.relative_to(skill_dir)) for f in templates_dir.rglob(ext))
        if templates:
            linked_files["templates"] = sorted(set(templates))

    assets_dir = skill_dir / "assets"
    if assets_dir.exists():
        assets = [str(f.relative_to(skill_dir)) for f in assets_dir.rglob("*") if f.is_file()]
        if assets:
            linked_files["assets"] = sorted(assets)

    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        scripts = []
        for ext in ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"]:
            scripts.extend(str(f.relative_to(skill_dir)) for f in scripts_dir.glob(ext))
        if scripts:
            linked_files["scripts"] = sorted(set(scripts))

    return linked_files


def _skill_view_from_file(skill_dir: Path | None, skill_md: Path, *, _linked_files_for_skill) -> dict:
    from tools.skills_tool import _parse_frontmatter, _parse_tags, skill_matches_platform

    content = skill_md.read_text(encoding="utf-8")
    frontmatter, _body = _parse_frontmatter(content)
    if not skill_matches_platform(frontmatter):
        return {"success": False, "error": "Skill is not available on this platform."}

    metadata = frontmatter.get("metadata")
    hermes_meta = metadata.get("hermes", {}) if isinstance(metadata, dict) else {}
    tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
    related_skills = _parse_tags(
        hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
    )
    try:
        path = str(skill_md.relative_to((skill_dir or skill_md.parent).parent))
    except ValueError:
        path = str(skill_md)

    return {
        "success": True,
        "name": frontmatter.get("name", skill_md.stem if not skill_dir else skill_dir.name),
        "description": frontmatter.get("description", ""),
        "tags": tags,
        "related_skills": related_skills,
        "content": content,
        "path": path,
        "skill_dir": str(skill_dir) if skill_dir else None,
        "linked_files": _linked_files_for_skill(skill_dir),
    }


def _skill_view_from_active_dir(
    name: str,
    *,
    _active_skill_search_dirs,
    _active_skills_dir,
    _find_skill_in_dirs,
    _skill_not_found_payload,
    _skill_view_from_file,
) -> dict:
    from tools.skills_tool import skill_view as _skill_view

    skills_dir = _active_skills_dir()
    search_dirs = _active_skill_search_dirs(skills_dir)
    skill_dir, skill_md = _find_skill_in_dirs(name, search_dirs)
    if not skill_md:
        # Preserve plugin-qualified skill viewing without falling back to the
        # startup/root profile's local skills tree for ordinary missing skills.
        if ":" in str(name or ""):
            try:
                from agent.skill_utils import is_valid_namespace, parse_qualified_name
                from hermes_cli.plugins import discover_plugins, get_plugin_manager

                namespace, _bare = parse_qualified_name(name)
                if is_valid_namespace(namespace):
                    discover_plugins()
                    pm = get_plugin_manager()
                    if pm.find_plugin_skill(name) is not None or pm.list_plugin_skills(namespace):
                        raw = _skill_view(name)
                        return json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                pass
        return _skill_not_found_payload(name, skills_dir)
    return _skill_view_from_file(skill_dir, skill_md)


def _handle_skill_save(handler, body, *, _SKILLS_STATS_CACHE, _active_skills_dir, bad, j, require):
    try:
        require(body, "name", "content")
    except ValueError as e:
        return bad(handler, str(e))
    skill_name = body["name"].strip().lower().replace(" ", "-")
    if not skill_name or "/" in skill_name or ".." in skill_name:
        return bad(handler, "Invalid skill name")
    category = body.get("category", "").strip()
    if category and ("/" in category or ".." in category):
        return bad(handler, "Invalid category")
    skills_dir = _active_skills_dir()

    if category:
        skill_dir = skills_dir / category / skill_name
    else:
        skill_dir = skills_dir / skill_name
    # Validate resolved path stays within the active profile skills dir.
    try:
        skill_dir.resolve().relative_to(skills_dir.resolve())
    except ValueError:
        return bad(handler, "Invalid skill path")
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    if skill_file.is_symlink():
        return bad(handler, "Cannot save to a symlinked skill file")
    skill_file.write_text(body["content"], encoding="utf-8")
    _SKILLS_STATS_CACHE.clear()
    return j(handler, {"ok": True, "name": skill_name, "path": str(skill_file)})


def _handle_skill_delete(handler, body, *, _SKILLS_STATS_CACHE, _active_skills_dir, bad, j, require):
    try:
        require(body, "name")
    except ValueError as e:
        return bad(handler, str(e))
    import shutil

    skill_name = str(body["name"]).strip().lower().replace(" ", "-")
    if not skill_name or "/" in skill_name or ".." in skill_name:
        return bad(handler, "Invalid skill name")
    skills_dir = _active_skills_dir()
    matches = [p for p in skills_dir.rglob("SKILL.md") if p.parent.name == skill_name]
    if not matches:
        return bad(handler, "Skill not found", 404)
    skill_dir = matches[0].parent
    shutil.rmtree(str(skill_dir))
    _SKILLS_STATS_CACHE.clear()
    return j(handler, {"ok": True, "name": body["name"]})


def _normalize_names_list(names, *, _parse_config_string_list) -> list[str]:
    """Normalize a config value (None/str/list) into a deduplicated str list."""
    if names is None:
        return []
    if isinstance(names, str):
        names = _parse_config_string_list(names)
    elif not isinstance(names, list):
        names = list(names) if names else []
    return list(dict.fromkeys(str(d).strip() for d in names if str(d).strip()))


def _toggle_name_in_list(names, name: str, enabled: bool, *, _normalize_names_list) -> list[str]:
    """Add or remove *name* from *names*, returning a new list."""
    names = _normalize_names_list(names)
    if enabled:
        return [d for d in names if d != name]
    if name not in names:
        names.append(name)
    return names


def _handle_skill_toggle(
    handler,
    body,
    *,
    _SKILLS_STATS_CACHE,
    _active_profile_config_path,
    _active_skill_search_dirs,
    _active_skills_dir,
    _cfg_lock,
    _find_skill_in_dirs,
    _load_yaml_config_file,
    _save_yaml_config_file,
    _toggle_name_in_list,
    bad,
    j,
    reload_config,
    require,
):
    """Toggle a skill's enabled/disabled state in the active profile's config.yaml.

    Writes through to ``skills.platform_disabled.webui`` when that key exists
    so the toggle takes effect for WebUI sessions (the agent's
    ``get_disabled_skill_names`` checks platform-specific lists first when
    ``HERMES_SESSION_PLATFORM`` is set).
    """
    try:
        require(body, "name", "enabled")
    except ValueError as e:
        return bad(handler, str(e))

    name = body["name"].strip()
    enabled = bool(body["enabled"])

    # Validate the skill exists in the filesystem
    skills_dir = _active_skills_dir()
    search_dirs = _active_skill_search_dirs(skills_dir)
    skill_dir, skill_md = _find_skill_in_dirs(name, search_dirs)
    if not skill_md:
        return bad(handler, f"Skill '{name}' not found", 404)

    config_path = _active_profile_config_path()
    with _cfg_lock:
        cfg = _load_yaml_config_file(config_path)

        # Ensure skills section exists as a dict
        if "skills" not in cfg or not isinstance(cfg["skills"], dict):
            cfg["skills"] = {}
        skills_cfg = cfg["skills"]

        # Always update the global disabled list
        skills_cfg["disabled"] = _toggle_name_in_list(
            skills_cfg.get("disabled"), name, enabled
        )

        # Write-through to platform_disabled.webui if it exists so that the
        # toggle takes effect for WebUI sessions (the agent checks the
        # platform-specific list first when HERMES_SESSION_PLATFORM=webui).
        platform_disabled = skills_cfg.get("platform_disabled")
        if isinstance(platform_disabled, dict) and "webui" in platform_disabled:
            platform_disabled["webui"] = _toggle_name_in_list(
                platform_disabled["webui"], name, enabled
            )

        cfg["skills"] = skills_cfg
        _save_yaml_config_file(config_path, cfg)

    reload_config()  # outside with block — reload_config() acquires the lock itself
    _SKILLS_STATS_CACHE.clear()
    return j(handler, {"ok": True, "name": name, "enabled": enabled})
