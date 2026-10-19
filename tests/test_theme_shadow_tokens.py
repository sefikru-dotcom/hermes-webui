"""Elevation tokens must be defined in both light and dark palettes.

`box-shadow: var(--shadow-sm)` silently drops the whole declaration when the
custom property is undefined, so the element renders flat with no warning.
"""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")


def _block(selector: str) -> str:
    start = CSS.index(selector)
    depth = 0
    for i in range(start, len(CSS)):
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
            if depth == 0:
                return CSS[start:i]
    raise AssertionError(f"unterminated block {selector}")


def test_shadow_tokens_defined_for_light_and_dark():
    for selector in ("  :root {\n", ":root.dark {\n"):
        block = _block(selector)
        for token in ("--shadow-sm", "--shadow-md", "--shadow-lg"):
            assert re.search(rf"{token}\s*:\s*\S", block), f"{token} missing in {selector.strip()}"


def test_every_used_shadow_token_exists():
    used = set(re.findall(r"var\((--shadow-[a-z0-9-]+)", CSS))
    defined = set(re.findall(r"(--shadow-[a-z0-9-]+)\s*:", CSS))
    assert used <= defined, f"undefined elevation tokens: {sorted(used - defined)}"
