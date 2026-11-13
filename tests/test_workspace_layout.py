"""The tests that check the REAL workspace run only where aisland sits inside one (`tests/workspace_layout.py`)."""

from pathlib import Path

from tests.workspace_layout import AISLAND_PROJECT, inside_a_workspace, needs_workspace


def test_a_checkout_whose_parent_holds_gitmodules_is_inside_a_workspace(tmp_path: Path) -> None:
    (tmp_path / ".gitmodules").write_text('[submodule "aisland"]\n\tpath = aisland\n')
    (tmp_path / "aisland").mkdir()
    assert inside_a_workspace(tmp_path / "aisland")


def test_a_checkout_on_its_own_is_not(tmp_path: Path) -> None:
    """What GitHub CI checks out: aisland alone, nothing above it."""
    (tmp_path / "aisland").mkdir()
    assert not inside_a_workspace(tmp_path / "aisland")


def test_the_marker_skips_exactly_when_this_checkout_is_not_inside_a_workspace() -> None:
    assert needs_workspace.args == (not inside_a_workspace(AISLAND_PROJECT),)
    assert needs_workspace.kwargs["reason"] == "aisland is not checked out inside a workspace (no .gitmodules above it)"
