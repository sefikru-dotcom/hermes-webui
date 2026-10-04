"""Call-time compatibility seams for the skill domain."""


def test_disabled_skill_reader_follows_current_profile_and_loader(monkeypatch, tmp_path):
    from api import routes

    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.touch()
    second.touch()
    current = [first]
    monkeypatch.setattr(routes, "_active_profile_config_path", lambda: current[0])
    monkeypatch.setattr(routes, "_load_yaml_config_file", lambda path: {"skills": {"disabled": [path.stem]}})
    assert routes._get_disabled_skill_names_for_profile() == {"first"}
    current[0] = second
    assert routes._get_disabled_skill_names_for_profile() == {"second"}


def test_skill_not_found_uses_current_list_and_limit(monkeypatch, tmp_path):
    from api import routes

    monkeypatch.setattr(routes, "_skills_list_from_dir", lambda root: {"skills": [{"name": str(i)} for i in range(4)]})
    monkeypatch.setattr(routes, "_SKILL_NOT_FOUND_LIST_LIMIT", 2)
    result = routes._skill_not_found_payload("missing", tmp_path)
    assert result["available_skills"] == ["0", "1"]
    assert result["available_skills_truncated"] is True
    assert result["total_skills"] == 4


def test_skill_toggle_keeps_route_owned_config_lock_and_cache(monkeypatch, tmp_path):
    import threading
    from api import routes

    skill = tmp_path / "skill"
    skill.mkdir()
    index = skill / "SKILL.md"
    index.touch()
    cfg = {"skills": {"disabled": ["skill"], "platform_disabled": {"webui": ["skill"]}}}
    writes = []
    lock = threading.Lock()
    cache = {"old": object()}
    monkeypatch.setattr(routes, "_cfg_lock", lock)
    monkeypatch.setattr(routes, "_SKILLS_STATS_CACHE", cache)
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda root: [root])
    monkeypatch.setattr(routes, "_find_skill_in_dirs", lambda name, roots: (skill, index))
    monkeypatch.setattr(routes, "_active_profile_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(routes, "_load_yaml_config_file", lambda path: cfg)

    def save(path, config):
        assert lock.locked()
        writes.append((path, config))

    def reload():
        assert not lock.locked()

    monkeypatch.setattr(routes, "_save_yaml_config_file", save)
    monkeypatch.setattr(routes, "reload_config", reload)
    monkeypatch.setattr(routes, "j", lambda handler, payload: payload)
    assert routes._handle_skill_toggle(object(), {"name": "skill", "enabled": True})["ok"] is True
    assert cfg["skills"]["disabled"] == []
    assert cfg["skills"]["platform_disabled"]["webui"] == []
    assert writes[0][0] == tmp_path / "config.yaml"
    assert cache == {}


def test_skill_path_containment_export_keeps_shared_identity(tmp_path):
    from api import route_skills, routes

    assert routes._skill_path_within is route_skills._skill_path_within
    assert routes._skill_path_within(tmp_path, tmp_path / "skill") is True
    assert routes._skill_path_within(tmp_path, tmp_path.parent / "outside") is False
