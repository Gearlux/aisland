"""The workspace as one record (`aisland.workspace.model`): each project with its folder, submodule, file row and metadata.

A caller asks one `Project` instead of zipping the folder list, the submodules, `workspace.toml` and the
`pyproject.toml` files by name. The orders made from the metadata never follow the order a folder listing gives.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from aisland.pyproject import PyprojectError
from aisland.workspace import model
from aisland.workspace.model import Workspace


def _project(folder: Path, name: str, requires: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    dependencies = f'"{requires}"' if requires else ""
    (folder / "pyproject.toml").write_text(f'[project]\nname = "{name}"\ndependencies = [{dependencies}]\n')


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    """A workspace of plain folders: `app` needs `lib`, `tools` is listed in workspace.toml, `docs` has no pyproject."""
    root = tmp_path / "acme"
    _project(root / "app", "acme-app", "acme-lib>=0.1")
    _project(root / "lib", "acme-lib")
    _project(root / "tools", "acme-tools")
    (root / "docs").mkdir()
    (root / "workspace.toml").write_text(
        '[workspace]\nformat = 1\nname = "acme"\n\n'
        '[project."tools"]\nurl = "git@example.com:acme/tools.git"\nbranch = "main"\n'
    )
    return root


def test_each_project_joins_its_folder_its_row_and_its_metadata(acme: Path) -> None:
    workspace = Workspace(acme)
    assert [project.name for project in workspace.projects] == ["app", "lib", "tools"]
    tools = workspace.project("tools")
    assert tools is not None
    assert (tools.folder, tools.path, tools.active, tools.submodule) == ("tools", acme / "tools", True, None)
    assert tools.listed is not None and tools.listed.url == "git@example.com:acme/tools.git"
    assert tools.metadata is not None and tools.metadata["name"] == "acme-tools"
    app = workspace.project("app")
    assert app is not None and app.listed is None and not app.checked_out


def test_a_project_nobody_has_is_none(acme: Path) -> None:
    assert Workspace(acme).project("docs") is None


def test_the_install_order_puts_a_dependency_first(acme: Path) -> None:
    assert [entry["name"] for entry in Workspace(acme).install_order] == ["acme-lib", "acme-app", "acme-tools"]


def test_the_metadata_never_follows_the_order_the_disk_lists_folders_in(
    acme: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: List[Path] = model.discover_projects(acme)
    expected = [entry["name"] for entry in Workspace(acme).metadata]
    monkeypatch.setattr(model, "discover_projects", lambda root: list(reversed(listed)))
    assert [entry["name"] for entry in Workspace(acme).metadata] == expected == ["acme-app", "acme-lib", "acme-tools"]


def test_the_known_names_are_the_folders_holding_a_pyproject(acme: Path) -> None:
    (acme / ".hidden").mkdir()
    (acme / ".hidden" / "pyproject.toml").write_text('[project]\nname = "hidden"\n')
    assert Workspace(acme).known_names == ["app", "lib", "tools"]
    assert Workspace(acme).top_level_folders == [acme / "app", acme / "lib", acme / "tools"]


def test_a_workspace_is_a_snapshot_read_once(acme: Path) -> None:
    """Asked twice, a part is read once: a caller that changes the workspace builds a new one."""
    workspace = Workspace(acme)
    assert workspace.name == "acme"
    (acme / "workspace.toml").write_text('[workspace]\nformat = 1\nname = "other"\n')
    assert workspace.name == "acme" and Workspace(acme).name == "other"


def test_a_workspace_under_a_hidden_folder_still_finds_its_projects(tmp_path: Path) -> None:
    """Only the folders' own names are read: a workspace whose path above it holds `.venv` is a workspace like any."""
    from aisland.workspace.projects import discover_projects

    root = tmp_path / ".venvs-work" / "acme"
    for folder in ("app", ".venv/lib/stray", ".cache/tool"):
        (root / folder).mkdir(parents=True)
        (root / folder / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert discover_projects(root) == [root / "app" / "pyproject.toml"]


def test_a_sub_projects_broken_file_is_refused_naming_it(tmp_path: Path) -> None:
    """A template's `pyproject.toml` with placeholders stopped every reader with tomllib's error, naming no file."""
    _project(tmp_path / "app", "app")
    broken = tmp_path / "app" / "template" / "pyproject.toml"
    broken.parent.mkdir()
    broken.write_text('[project]\nname = "{{ name }}\nversion = {{ version }}\n')
    with pytest.raises(PyprojectError, match=rf"^{broken}: "):
        Workspace(tmp_path).metadata
