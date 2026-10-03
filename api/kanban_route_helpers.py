"""Small route-facing helpers for Kanban compatibility diagnostics."""

from __future__ import annotations


def kanban_unknown_endpoint_message(parsed, method: str) -> str:
    """Build the stale-client diagnostic for an unmatched Kanban route."""
    return (
        f"unknown Kanban endpoint: {method} {parsed.path}. "
        "If this appeared after a WebUI update, your browser may be running "
        "a stale cached bundle; use Hard refresh now, then reopen Kanban."
    )
