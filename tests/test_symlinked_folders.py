"""A symbolic link to a folder of the workspace is a second name for that folder, never a project of its own.

A workspace may give its long project folders short names by links in its root (`ml -> machine_learning`). Every reader
of the workspace's projects skips such a link — the folder it names is found under its own name anyway — so nothing
is listed, installed or offered twice; a link to a folder OUTSIDE the workspace still brings that folder in. The
generated VS Code workspace file shows each project under its short name (`aisland.locations.alias_names`).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from aisland import harness, locations
from aisland.setup import editor_files
from aisland.workspace import file as workspace_file
from aisland.workspace import projects, selection, sources


def _project(folder: Path, name: str, extras: str = "dev") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n[project.optional-dependencies]\n{extras} = []\n'
    )
    return folder


def _clone(folder: Path, origin: str) -> None:
    """Make ``folder`` a git clone whose ``origin`` is ``origin``."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=folder, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=folder, check=True)


@pytest.fixture
def acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`acme/` (no `.gitmodules`) holding the projects `machine_learning/` and `object_detection/` (with its sub-project
    `object_detection/torchsig/`), the short names `ml -> machine_learning` and `od -> object_detection/`, `ext -> ../outside/extproj` (a
    project outside the workspace) and `logs -> ../outside/logs` (a folder that is no project)."""
    root = tmp_path / "acme"
    _clone(_project(root / "machine_learning", "machine-learning"), "https://example.com/machine_learning.git")
    _clone(_project(root / "object_detection", "object-detection"), "https://example.com/object_detection.git")
    _project(root / "object_detection" / "torchsig", "torchsig")
    _project(tmp_path / "outside" / "extproj", "extproj")
    (tmp_path / "outside" / "logs").mkdir()
    os.symlink("machine_learning", root / "ml")
    os.symlink("object_detection/", root / "od")
    os.symlink("../outside/extproj", root / "ext")
    os.symlink("../outside/logs", root / "logs")
    monkeypatch.setattr(editor_files, "_loggair_log_dir", lambda workspace_root: None)
    return root


def _relative(root: Path, paths: List[Path]) -> List[str]:
    return sorted(os.path.relpath(path, root) for path in paths)


# --- a link inside the workspace is no project; one outside is ------------------------------------------------------


def test_aisland_setup_discovers_each_project_once_and_the_one_a_link_brings_in(acme: Path) -> None:
    """`discover_projects` feeds setup's extras prompt, the install order, pyright and `mypy_path`."""
    assert _relative(acme, projects.discover_projects(acme)) == [
        "ext/pyproject.toml",
        "machine_learning/pyproject.toml",
        "object_detection/pyproject.toml",
        "object_detection/torchsig/pyproject.toml",
    ]


def test_the_extras_prompt_names_each_project_once(acme: Path) -> None:
    assert sorted(selection.declarations(acme, selection.EXTRAS)) == [
        "ext",
        "machine_learning",
        "object_detection",
        "torchsig",
    ]


def test_a_workspace_without_gitmodules_lists_each_folder_once(acme: Path) -> None:
    assert projects.workspace_folders(acme) == [
        ("ext", "ext", True),
        ("machine_learning", "machine_learning", True),
        ("object_detection", "object_detection", True),
    ]


def test_aisland_source_knows_each_project_once(acme: Path) -> None:
    assert sources.known_projects(acme) == ["ext", "machine_learning", "object_detection"]


def test_the_agent_commands_go_into_each_project_folder_once(acme: Path) -> None:
    assert harness._project_folders(acme) == [acme / "ext", acme / "machine_learning", acme / "object_detection"]


def test_the_workspace_file_lists_each_clone_once(acme: Path) -> None:
    """ext is a project but no clone, logs a link to a folder outside the workspace: neither is a project of the
    file, and each clone is listed once, never again under a link to it."""
    projects = workspace_file.workspace_projects(acme)
    assert [project.name for project in projects] == ["machine_learning", "object_detection"]


def test_a_link_into_a_project_from_inside_another_is_no_project_either(acme: Path) -> None:
    os.symlink("../object_detection/torchsig", acme / "machine_learning" / "torchsig")
    assert "machine_learning/torchsig/pyproject.toml" not in _relative(acme, projects.discover_projects(acme))


def test_a_link_that_points_nowhere_is_skipped(acme: Path) -> None:
    os.symlink("gone", acme / "broken")
    assert "broken" not in [name for name, _path, _active in projects.workspace_folders(acme)]


def test_a_workspace_reached_through_a_link_still_tells_its_aliases(acme: Path, tmp_path: Path) -> None:
    """`~/acme -> /build/acme`: the root's own link does not make every folder an outside one."""
    os.symlink(acme, tmp_path / "home_acme")
    entered = tmp_path / "home_acme"
    assert _relative(entered, projects.discover_projects(entered)) == [
        "ext/pyproject.toml",
        "machine_learning/pyproject.toml",
        "object_detection/pyproject.toml",
        "object_detection/torchsig/pyproject.toml",
    ]


# --- the short names ------------------------------------------------------------------------------------------------


def test_alias_names_map_each_folder_to_its_short_name(acme: Path) -> None:
    assert locations.alias_names(acme) == {
        (acme / "machine_learning").resolve(): "ml",
        (acme / "object_detection").resolve(): "od",
    }


def test_of_two_short_names_for_one_folder_the_first_by_name_wins(acme: Path) -> None:
    os.symlink("machine_learning", acme / "zz_ai")
    os.symlink("machine_learning", acme / "lab")
    assert locations.alias_names(acme)[(acme / "machine_learning").resolve()] == "lab"


def test_a_hidden_link_gives_no_short_name(acme: Path) -> None:
    os.symlink("machine_learning", acme / ".k")
    assert locations.alias_names(acme)[(acme / "machine_learning").resolve()] == "ml"


def _folders(root: Path) -> List[Dict[str, Any]]:
    text = editor_files.code_workspace_path(root).read_text()
    document: Dict[str, Any] = json.loads("\n".join(line for line in text.splitlines() if not line.startswith("//")))
    folders: List[Dict[str, Any]] = document["folders"]
    return folders


def test_the_vscode_workspace_shows_each_project_under_its_short_name(acme: Path) -> None:
    editor_files.write_code_workspace(acme)
    assert _folders(acme) == [
        {"name": "acme", "path": "."},
        {"name": "ext", "path": "ext"},
        {"name": "ml", "path": "machine_learning"},
        {"name": "od", "path": "object_detection"},
        {"name": "torchsig", "path": "object_detection/torchsig"},
    ]


def test_without_a_link_a_project_keeps_its_folders_name(acme: Path) -> None:
    (acme / "ml").unlink()
    editor_files.write_code_workspace(acme)
    assert {"name": "machine_learning", "path": "machine_learning"} in _folders(acme)
