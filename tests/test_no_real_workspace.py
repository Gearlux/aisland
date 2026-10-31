"""No test reaches the workspace aisland is checked out in unless it says so (``tests/conftest.py::no_real_workspace``).

A test that forgot to point the workspace root at its own folder once ran ``aisland check`` on the real workspace,
whose ``isort .`` and ``black .`` rewrite files. The guard turns that into a failure naming the test.
"""

from pathlib import Path

import pytest

from aisland import locations, projects
from tests.conftest import RealWorkspaceReached
from tests.workspace_layout import needs_workspace


def test_a_test_that_asks_for_the_root_without_pointing_it_anywhere_fails_naming_itself(
    request: pytest.FixtureRequest,
) -> None:
    with pytest.raises(RealWorkspaceReached) as refused:
        projects.get_workspace_root()
    assert request.node.nodeid in str(refused.value)
    assert 'monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)' in str(refused.value)
    assert "@pytest.mark.real_workspace" in str(refused.value)


def test_a_test_that_points_the_root_at_its_folder_gets_its_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    assert projects.get_workspace_root() == tmp_path


@pytest.mark.real_workspace
def test_a_test_marked_real_workspace_gets_the_checkouts_workspace() -> None:
    assert locations.workspace_root() == locations.checkout_workspace_root()


@needs_workspace
def test_a_test_of_the_real_workspaces_facts_gets_it_too() -> None:
    assert (locations.workspace_root() / ".gitmodules").is_file()


def test_the_refusal_is_no_error_aisland_catches() -> None:
    """aisland catches ``OSError`` and its own ``AislandError`` around file and git work; the refusal is neither, so
    no ``except`` in aisland turns it into a quiet fallback."""
    assert issubclass(RealWorkspaceReached, AssertionError)
    assert not issubclass(RealWorkspaceReached, (OSError, ValueError))
