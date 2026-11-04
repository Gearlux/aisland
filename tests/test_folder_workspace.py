"""A workspace without `.gitmodules`: every subfolder holding a `pyproject.toml` is one of its projects.

aisland treats such a folder as if each project were a submodule — the folder you run aisland in (or, from inside a
project, the project's parent) is the workspace, the project list comes from the folders, and what only a submodule
can do (a `github`/`pypi` source empties the folder and checks it out again) is refused with the reason. A workspace
WITH `.gitmodules` is read from it, as always.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from aisland import locations, projects, pytest_stats, sources
from aisland.errors import AislandError
from aisland.wheels import build as wheels


def _project(folder: Path, name: str, deps: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = [{deps}]\n')
    return folder


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    """`acme/` (no git) holding the projects `alpha/` and `beta/` (beta needs alpha), and folders that are not ones."""
    root = tmp_path / "acme"
    _project(root / "alpha", "alpha")
    _project(root / "beta", "beta", '"alpha"')
    (root / "beta" / "src" / "beta").mkdir(parents=True)
    (root / "docs").mkdir()  # no pyproject.toml: not a project
    _project(root / ".venv", "not-a-project")  # hidden: never a project
    return root


# --- finding the workspace (aisland from a wheel) -----------------------------------------------------------------


def test_the_folder_you_run_in_is_the_workspace_when_it_holds_projects(acme: Path) -> None:
    assert locations.find_workspace(acme) == acme


def test_from_inside_a_project_the_workspace_is_the_project_s_parent(acme: Path) -> None:
    assert locations.find_workspace(acme / "beta" / "src" / "beta") == acme


def test_a_project_that_is_its_own_git_clone_is_never_the_workspace(acme: Path) -> None:
    (acme / "alpha" / ".git").mkdir()
    assert locations.find_workspace(acme / "alpha") == acme


def test_a_superproject_s_gitmodules_wins(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    _project(root / "confluid", "confluid")
    (root / ".gitmodules").write_text('[submodule "confluid"]\n\tpath = confluid\n')
    (root / ".git").mkdir()
    (root / "confluid" / ".git").write_text("gitdir: ../.git/modules/confluid\n")
    assert locations.find_workspace(root / "confluid") == root


def test_a_git_repository_holding_no_project_yet_is_a_workspace(tmp_path: Path) -> None:
    """What `aisland workspace init` makes before any `--add`."""
    root = tmp_path / "fresh"
    (root / ".git").mkdir(parents=True)
    assert locations.find_workspace(root) == root


def test_a_folder_beside_a_project_is_no_workspace(acme: Path) -> None:
    """Only the folder you run in counts its subfolders: from `docs/`, `acme/` is not taken for a workspace — or
    a home folder holding one project would become one wherever you ran aisland under it."""
    assert locations.find_workspace(acme / "docs") is None


# --- the projects ---------------------------------------------------------------------------------------------------


def test_without_gitmodules_every_subfolder_with_a_pyproject_is_an_active_project(acme: Path) -> None:
    assert projects.workspace_folders(acme) == [("alpha", "alpha", True), ("beta", "beta", True)]


def test_with_gitmodules_the_submodules_are_the_projects(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    _project(root / "confluid", "confluid")
    _project(root / "stray", "stray")  # a folder .gitmodules does not declare
    (root / ".gitmodules").write_text('[submodule "confluid"]\n\tpath = confluid\n\tactive = false\n')
    assert projects.workspace_folders(root) == [("confluid", "confluid", False)]


def test_wheels_sees_the_folder_projects(acme: Path) -> None:
    assert sorted(wheels.workspace_projects(acme)) == ["alpha", "beta"]


def test_a_wheel_of_a_project_outside_git_is_refused(acme: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wheels, "pypi_state", lambda distribution, owner: ("absent", None))
    with pytest.raises(click.ClickException) as raised:
        wheels.vendor_wheels(acme, "beta")
    assert raised.value.message == (
        "alpha is in no git repository; a wheel is built from a project's last commit. "
        "Put it under git (git init, then commit) and run this again."
    )


def test_a_test_run_of_a_folder_project_is_recorded(acme: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pytest_stats, "workspace_root", lambda: acme)
    assert pytest_stats._is_workspace_project(acme / "beta") is True
    assert pytest_stats._is_workspace_project(acme / "docs") is False
    assert pytest_stats._is_workspace_project(acme / "beta" / "src") is False


@pytest.mark.parametrize("kind", ["pypi", "github"])
def test_a_source_that_would_empty_a_folder_project_is_refused(acme: Path, kind: str) -> None:
    with pytest.raises(AislandError) as refusal:
        sources.set_project_source("alpha", kind, workspace_root=acme)
    assert refusal.value.format_message() == (
        f"'alpha' is a folder of the workspace, not a submodule: a {kind} source empties its folder, "
        "and nothing could check it out again. Keep it local."
    )
    assert (acme / "alpha" / "pyproject.toml").is_file()
