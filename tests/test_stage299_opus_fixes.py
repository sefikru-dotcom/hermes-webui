"""Regression test for the Opus SHOULD-FIX bounds applied in stage-299.

PR #1664 introduced /api/wiki/status with `_llm_wiki_count_files` and
`_llm_wiki_page_files` that walk WIKI_PATH via `rglob`. Without bounds,
a misconfigured WIKI_PATH=/ or symlink loop would hang the endpoint.

These tests pin the defenses applied per Opus advisor on stage-299:
- A constant cap on iteration (_LLM_WIKI_MAX_FILES) for both functions
- A forbidden-roots blocklist (_LLM_WIKI_FORBIDDEN_ROOTS) that includes
  '/' / '/etc' / '/usr' / '/var' / '/opt' / '/sys' / '/proc' (resolved
  to absolute strings)
- Bounded behavior: if WIKI_PATH points at a forbidden root, both
  functions return 0/empty without iterating
"""
from pathlib import Path

def test_wiki_max_files_constant_present():
    from api import routes

    assert 1000 <= routes._LLM_WIKI_MAX_FILES <= 100000


def test_count_files_has_iteration_cap(tmp_path, monkeypatch):
    from api import routes

    root = tmp_path / "raw"
    root.mkdir()
    for i in range(5):
        (root / f"{i}.md").write_text("body")
    monkeypatch.setattr(routes, "_LLM_WIKI_MAX_FILES", 2)

    assert routes._llm_wiki_count_files(root) == 2


def test_page_files_has_iteration_cap(tmp_path, monkeypatch):
    from api import routes

    wiki = tmp_path / "wiki"
    section = wiki / "concepts"
    section.mkdir(parents=True)
    for i in range(5):
        (section / f"{i}.md").write_text("body")
    monkeypatch.setattr(routes, "_LLM_WIKI_MAX_FILES", 2)

    assert len(routes._llm_wiki_page_files_uncached(wiki)) == 2


def test_forbidden_roots_includes_system_paths():
    from api import routes

    for forbidden in ("/", "/etc", "/usr", "/var", "/opt", "/sys", "/proc"):
        assert str(Path(forbidden).resolve()) in routes._LLM_WIKI_FORBIDDEN_ROOTS


def test_count_files_returns_zero_for_forbidden_root(tmp_path, monkeypatch):
    from api import routes

    root = tmp_path / "forbidden"
    root.mkdir()
    monkeypatch.setattr(routes, "_LLM_WIKI_FORBIDDEN_ROOTS", frozenset({str(root.resolve())}))

    def no_walk(*args, **kwargs):
        raise AssertionError("forbidden roots must not be walked")

    monkeypatch.setattr(Path, "rglob", no_walk)
    assert routes._llm_wiki_count_files(root) == 0
    assert routes._llm_wiki_page_files_uncached(root) == []


def test_render_llm_wiki_status_uses_url_scheme_guard():
    """Opus SHOULD-FIX #1: docs_url interpolated into href must be scheme-guarded."""
    panels_js = (Path(__file__).parent.parent / "static" / "panels.js").read_text()
    # Find the _renderLlmWikiStatus function body
    start = panels_js.find("function _renderLlmWikiStatus")
    end = panels_js.find("\nfunction ", start + 1)
    body = panels_js[start:end]
    # Must use a scheme-guarded form, not raw esc()
    assert "/^https?:" in body or "test(rawDocsUrl)" in body or "test(docsUrl)" in body, (
        "Expected URL scheme guard (e.g. /^https?:\\/\\//.test(...)) before "
        "interpolating docsUrl into href to prevent javascript: scheme XSS "
        "if docs_url ever becomes config-driven."
    )
