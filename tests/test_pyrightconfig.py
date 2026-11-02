"""Tests for the ``pyrightconfig.json`` generator wired into ``aisland setup``.

The generator (``editor_files.write_pyrightconfig``) replaces the previously
hand-maintained, hardcoded-path ``pyrightconfig.json`` at the workspace root
with a portable, regenerated-on-every-setup artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from aisland.editor_files import write_pyrightconfig
from aisland.projects import ProjectMetadata
from tests.records import project_metadata


def _fake_projects(workspace: Path, names: list[str]) -> list[ProjectMetadata]:
    """Build ``get_install_order``-shaped dicts (only ``path`` is consumed)."""
    projects: list[ProjectMetadata] = []
    for name in names:
        (workspace / name).mkdir(parents=True, exist_ok=True)
        projects.append(project_metadata(name, workspace / name))
    return projects


class TestWritePyrightConfig:
    def test_writes_valid_json_with_expected_shape(self, tmp_path: Path) -> None:
        projects = _fake_projects(tmp_path, ["confluid", "matrainer"])
        write_pyrightconfig(tmp_path, projects)

        out = tmp_path / "pyrightconfig.json"
        assert out.is_file()
        data = json.loads(out.read_text())
        assert data["venvPath"] == "."
        assert data["venv"] == ".venv"
        assert data["exclude"] == [
            "**/__pycache__",
            "**/.venv",
            "**/.*_cache",
            "**/node_modules",
            "confluid",
            "matrainer",
        ]

    def test_the_root_excludes_every_project_inside_it(self, tmp_path: Path) -> None:
        """An editor window lists each project as a folder of its own, whose instance analyses it; the root's
        instance analysing them too read every file twice and ran Pylance out of memory. Imports still resolve
        through `extraPaths`. A project outside the workspace is no file of the root's: it is not excluded."""
        projects = _fake_projects(tmp_path / "acme", ["matrainer", "confluid"])
        (tmp_path / "elsewhere").mkdir()
        projects.append(project_metadata("elsewhere", tmp_path / "elsewhere"))
        write_pyrightconfig(tmp_path / "acme", projects)

        data = json.loads((tmp_path / "acme" / "pyrightconfig.json").read_text())
        assert data["exclude"][-2:] == ["confluid", "matrainer"]
        assert "../elsewhere" not in data["exclude"]
        assert data["extraPaths"] == ["../elsewhere", "confluid", "matrainer"]

    def test_a_projects_own_config_excludes_no_project(self, tmp_path: Path) -> None:
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "matrainer"]))
        data = json.loads((tmp_path / "confluid" / "pyrightconfig.json").read_text())
        assert data["exclude"] == ["**/__pycache__", "**/.venv", "**/.*_cache", "**/node_modules"]

    def test_extra_paths_are_relative_and_sorted(self, tmp_path: Path) -> None:
        projects = _fake_projects(tmp_path, ["matrainer", "confluid", "raidar-ultralytics"])
        write_pyrightconfig(tmp_path, projects)

        data = json.loads((tmp_path / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == ["confluid", "matrainer", "raidar-ultralytics"]

    def test_no_absolute_paths_anywhere(self, tmp_path: Path) -> None:
        """The whole point of generating this file is portability — no machine-
        specific absolute path may leak into the output (No-Hardcoded-Paths)."""
        projects = _fake_projects(tmp_path, ["confluid"])
        write_pyrightconfig(tmp_path, projects)

        raw = (tmp_path / "pyrightconfig.json").read_text()
        assert str(tmp_path) not in raw
        data = json.loads(raw)
        for path in data["extraPaths"]:
            assert not Path(path).is_absolute()
        assert not Path(data["venvPath"]).is_absolute()

    def test_trailing_newline(self, tmp_path: Path) -> None:
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid"]))
        assert (tmp_path / "pyrightconfig.json").read_text().endswith("}\n")

    def test_regenerates_idempotently(self, tmp_path: Path) -> None:
        """Re-running setup must overwrite, not append/duplicate — the file
        always reflects the live project set."""
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "matrainer"]))
        first = (tmp_path / "pyrightconfig.json").read_text()

        # Project removed on the second run.
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid"]))
        second = json.loads((tmp_path / "pyrightconfig.json").read_text())

        assert second["extraPaths"] == ["confluid"]
        assert first != (tmp_path / "pyrightconfig.json").read_text()

    def test_empty_project_list(self, tmp_path: Path) -> None:
        write_pyrightconfig(tmp_path, [])
        data = json.loads((tmp_path / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == []


class TestWritePerProjectPyrightConfig:
    """Per-project pyrightconfig.json — for a project opened as its own editor root."""

    def test_writes_config_into_every_project(self, tmp_path: Path) -> None:
        projects = _fake_projects(tmp_path, ["confluid", "matrainer", "recordstream"])
        write_pyrightconfig(tmp_path, projects)

        for name in ["confluid", "matrainer", "recordstream"]:
            assert (tmp_path / name / "pyrightconfig.json").is_file()

    def test_venv_resolves_to_shared_workspace_venv(self, tmp_path: Path) -> None:
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "matrainer"]))

        data = json.loads((tmp_path / "confluid" / "pyrightconfig.json").read_text())
        assert data["venvPath"] == ".."
        assert data["venv"] == ".venv"
        assert (tmp_path / "confluid" / data["venvPath"]).resolve() == tmp_path

    def test_extra_paths_are_sorted_siblings_excluding_self(self, tmp_path: Path) -> None:
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["matrainer", "confluid", "recordstream"]))

        data = json.loads((tmp_path / "confluid" / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == ["../matrainer", "../recordstream"]

    def test_no_absolute_paths_anywhere(self, tmp_path: Path) -> None:
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "matrainer"]))

        raw = (tmp_path / "confluid" / "pyrightconfig.json").read_text()
        assert str(tmp_path) not in raw
        data = json.loads(raw)
        for path in data["extraPaths"]:
            assert not Path(path).is_absolute()
        assert not Path(data["venvPath"]).is_absolute()

    def test_regenerates_idempotently(self, tmp_path: Path) -> None:
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "matrainer"]))
        # Project removed on the second run — the surviving config sheds the sibling.
        write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid"]))

        data = json.loads((tmp_path / "confluid" / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == []
