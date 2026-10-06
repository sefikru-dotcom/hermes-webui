"""Regression tests for confirmation dialogs on the mobile chat drawer."""
import re
from pathlib import Path


CSS = Path("static/style.css").read_text(encoding="utf-8")


def _z_index_of(selector_regex: str) -> int | None:
    match = re.search(selector_regex + r"\s*\{[^}]*z-index:\s*(\d+)", CSS)
    return int(match.group(1)) if match else None


def test_confirmation_dialog_outranks_mobile_sidebar_drawer():
    """Delete confirmation must not render underneath the open chat drawer."""
    dialog_z = _z_index_of(r"\.app-dialog-overlay")
    sidebar_z = _z_index_of(r"\.sidebar\.mobile-open")
    assert dialog_z is not None, "Confirmation dialog overlay must declare a z-index"
    assert sidebar_z is not None, "Mobile sidebar drawer must declare a z-index"
    assert dialog_z > sidebar_z, (
        f".app-dialog-overlay z-index ({dialog_z}) must be greater than "
        f".sidebar.mobile-open z-index ({sidebar_z})"
    )


def test_confirmation_dialog_stays_below_image_lightbox():
    """The dialog must not cover the higher-priority image lightbox."""
    dialog_z = _z_index_of(r"\.app-dialog-overlay")
    lightbox_z = _z_index_of(r"\.img-lightbox")
    assert dialog_z is not None
    assert lightbox_z is not None
    assert lightbox_z > dialog_z
