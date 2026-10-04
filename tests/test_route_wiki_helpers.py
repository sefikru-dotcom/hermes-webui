"""Call-time compatibility contracts for the extracted Wiki helpers."""


def test_path_resolver_uses_current_profile_and_config(monkeypatch, tmp_path):
    from api import routes

    monkeypatch.delenv("WIKI_PATH", raising=False)
    homes = [tmp_path / "first", tmp_path / "second"]
    calls = []
    monkeypatch.setattr(routes, "_llm_wiki_active_hermes_home", lambda: homes[len(calls)])

    def env_path(home):
        calls.append(home)
        return str(home / "wiki")

    monkeypatch.setattr(routes, "_llm_wiki_env_file_path", env_path)
    assert routes._llm_wiki_resolve_path() == (homes[0] / "wiki", "WIKI_PATH", True)
    assert routes._llm_wiki_resolve_path() == (homes[1] / "wiki", "WIKI_PATH", True)
    assert calls == homes
    monkeypatch.setattr(routes, "_llm_wiki_active_hermes_home", lambda: homes[0])
    monkeypatch.setattr(routes, "_llm_wiki_env_file_path", lambda home: None)
    monkeypatch.setattr(routes, "_llm_wiki_config_path", lambda: str(tmp_path / "configured"))
    assert routes._llm_wiki_resolve_path() == (tmp_path / "configured", "skills.config.wiki.path", True)


def test_cache_keeps_route_owned_identity_and_current_scanner(monkeypatch, tmp_path):
    import threading
    from api import routes

    cache = {}
    lock = threading.Lock()
    calls = []
    monkeypatch.setattr(routes, "_wiki_allowlist_cache", cache)
    monkeypatch.setattr(routes, "_wiki_allowlist_cache_lock", lock)
    monkeypatch.setattr(routes, "_WIKI_ALLOWLIST_TTL", 60)
    monkeypatch.setattr(routes, "_llm_wiki_page_files_cache_signature", lambda root: ("unchanged",))

    def scan(root):
        calls.append(root)
        return [root / "first.md"]

    monkeypatch.setattr(routes, "_llm_wiki_page_files_uncached", scan)
    assert routes._llm_wiki_page_files(tmp_path) == [tmp_path / "first.md"]
    assert routes._llm_wiki_page_files(tmp_path) == [tmp_path / "first.md"]
    assert calls == [tmp_path]
    assert cache[str(tmp_path)]["files"] == (tmp_path / "first.md",)
    routes._llm_wiki_clear_page_files_cache()
    assert cache == {}
    monkeypatch.setattr(routes, "_llm_wiki_page_files_uncached", lambda root: [root / "second.md"])
    assert routes._llm_wiki_page_files(tmp_path) == [tmp_path / "second.md"]


def test_status_builder_uses_current_route_dependencies(monkeypatch, tmp_path):
    from api import routes

    monkeypatch.setattr(routes, "_llm_wiki_resolve_path", lambda: (tmp_path, "test", True))
    monkeypatch.setattr(routes, "_llm_wiki_allowlisted_entries", lambda root: {})
    monkeypatch.setattr(routes, "_llm_wiki_verified_status_file_stat", lambda root, page: None)
    monkeypatch.setattr(routes, "_llm_wiki_count_files", lambda root: 7)
    monkeypatch.setattr(routes, "_llm_wiki_last_writer", lambda root, pages: "test-writer")
    monkeypatch.setattr(routes, "_llm_wiki_safe_iso", lambda latest: "test-time")
    monkeypatch.setattr(routes, "_LLM_WIKI_DOCS_URL", "https://example.invalid/wiki")

    status = routes._build_llm_wiki_status()
    assert status["status"] == "empty"
    assert status["raw_source_count"] == 7
    assert status["last_writer"] == "test-writer"
    assert status["last_updated"] == "test-time"
    assert status["docs_url"] == "https://example.invalid/wiki"


def test_allowlist_uses_current_route_page_scanner(monkeypatch, tmp_path):
    from api import routes

    page = tmp_path / "concepts" / "real.md"
    page.parent.mkdir()
    page.write_text("body")
    monkeypatch.setattr(routes, "_llm_wiki_page_files", lambda root: [page])
    st = page.stat()
    assert routes._llm_wiki_allowlisted_entries(tmp_path) == {"concepts/real.md": (page, (st.st_dev, st.st_ino))}
    monkeypatch.setattr(routes, "_llm_wiki_page_files", lambda root: [])
    assert routes._llm_wiki_allowlisted_entries(tmp_path) == {}
