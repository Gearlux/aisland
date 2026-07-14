"""Tests for the ``pyrightconfig.json`` generator wired into ``aisland setup``.

The generator (``manager._write_pyrightconfig``) replaces the previously
hand-maintained, hardcoded-path ``pyrightconfig.json`` at the workspace root
with a portable, regenerated-on-every-setup artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from aisland.manager import _ensure_gitignore_line, _write_pyrightconfig


def _fake_projects(workspace: Path, names: list[str]) -> list[dict]:
    """Build ``get_install_order``-shaped dicts (only ``path`` is consumed)."""
    projects = []
    for name in names:
        (workspace / name).mkdir(parents=True, exist_ok=True)
        projects.append({"name": name, "path": workspace / name})
    return projects


class TestWritePyrightConfig:
    def test_writes_valid_json_with_expected_shape(self, tmp_path: Path) -> None:
        projects = _fake_projects(tmp_path, ["confluid", "marainer"])
        _write_pyrightconfig(tmp_path, projects)

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
        ]

    def test_extra_paths_are_relative_and_sorted(self, tmp_path: Path) -> None:
        projects = _fake_projects(tmp_path, ["marainer", "confluid", "raidar-ultralytics"])
        _write_pyrightconfig(tmp_path, projects)

        data = json.loads((tmp_path / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == ["confluid", "marainer", "raidar-ultralytics"]

    def test_no_absolute_paths_anywhere(self, tmp_path: Path) -> None:
        """The whole point of generating this file is portability — no machine-
        specific absolute path may leak into the output (No-Hardcoded-Paths)."""
        projects = _fake_projects(tmp_path, ["confluid"])
        _write_pyrightconfig(tmp_path, projects)

        raw = (tmp_path / "pyrightconfig.json").read_text()
        assert str(tmp_path) not in raw
        data = json.loads(raw)
        for path in data["extraPaths"]:
            assert not Path(path).is_absolute()
        assert not Path(data["venvPath"]).is_absolute()

    def test_trailing_newline(self, tmp_path: Path) -> None:
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid"]))
        assert (tmp_path / "pyrightconfig.json").read_text().endswith("}\n")

    def test_regenerates_idempotently(self, tmp_path: Path) -> None:
        """Re-running setup must overwrite, not append/duplicate — the file
        always reflects the live project set."""
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "marainer"]))
        first = (tmp_path / "pyrightconfig.json").read_text()

        # Project removed on the second run.
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid"]))
        second = json.loads((tmp_path / "pyrightconfig.json").read_text())

        assert second["extraPaths"] == ["confluid"]
        assert first != (tmp_path / "pyrightconfig.json").read_text()

    def test_empty_project_list(self, tmp_path: Path) -> None:
        _write_pyrightconfig(tmp_path, [])
        data = json.loads((tmp_path / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == []


class TestWritePerProjectPyrightConfig:
    """Per-project pyrightconfig.json — for a project opened as its own editor root."""

    def test_writes_config_into_every_project(self, tmp_path: Path) -> None:
        projects = _fake_projects(tmp_path, ["confluid", "marainer", "dataflux"])
        _write_pyrightconfig(tmp_path, projects)

        for name in ["confluid", "marainer", "dataflux"]:
            assert (tmp_path / name / "pyrightconfig.json").is_file()

    def test_venv_resolves_to_shared_workspace_venv(self, tmp_path: Path) -> None:
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "marainer"]))

        data = json.loads((tmp_path / "confluid" / "pyrightconfig.json").read_text())
        assert data["venvPath"] == ".."
        assert data["venv"] == ".venv"
        assert (tmp_path / "confluid" / data["venvPath"]).resolve() == tmp_path

    def test_extra_paths_are_sorted_siblings_excluding_self(self, tmp_path: Path) -> None:
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["marainer", "confluid", "dataflux"]))

        data = json.loads((tmp_path / "confluid" / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == ["../dataflux", "../marainer"]

    def test_no_absolute_paths_anywhere(self, tmp_path: Path) -> None:
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "marainer"]))

        raw = (tmp_path / "confluid" / "pyrightconfig.json").read_text()
        assert str(tmp_path) not in raw
        data = json.loads(raw)
        for path in data["extraPaths"]:
            assert not Path(path).is_absolute()
        assert not Path(data["venvPath"]).is_absolute()

    def test_regenerates_idempotently(self, tmp_path: Path) -> None:
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid", "marainer"]))
        # Project removed on the second run — the surviving config sheds the sibling.
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid"]))

        data = json.loads((tmp_path / "confluid" / "pyrightconfig.json").read_text())
        assert data["extraPaths"] == []

    def test_gitignore_gains_artifact_entry(self, tmp_path: Path) -> None:
        _write_pyrightconfig(tmp_path, _fake_projects(tmp_path, ["confluid"]))
        assert "/pyrightconfig.json" in (tmp_path / "confluid" / ".gitignore").read_text().splitlines()


class TestEnsureGitignoreLine:
    def test_creates_missing_gitignore(self, tmp_path: Path) -> None:
        _ensure_gitignore_line(tmp_path, "/pyrightconfig.json")
        assert (tmp_path / ".gitignore").read_text() == "/pyrightconfig.json\n"

    def test_appends_preserving_existing_content(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.pyc\n")
        _ensure_gitignore_line(tmp_path, "/pyrightconfig.json")
        assert (tmp_path / ".gitignore").read_text() == "*.pyc\n/pyrightconfig.json\n"

    def test_appends_newline_separator_when_missing(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("*.pyc")
        _ensure_gitignore_line(tmp_path, "/pyrightconfig.json")
        assert (tmp_path / ".gitignore").read_text() == "*.pyc\n/pyrightconfig.json\n"

    def test_idempotent_exact_match(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("/pyrightconfig.json\n")
        _ensure_gitignore_line(tmp_path, "/pyrightconfig.json")
        assert (tmp_path / ".gitignore").read_text() == "/pyrightconfig.json\n"

    def test_idempotent_unanchored_variant_counts_as_covered(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("pyrightconfig.json\n")
        _ensure_gitignore_line(tmp_path, "/pyrightconfig.json")
        assert (tmp_path / ".gitignore").read_text() == "pyrightconfig.json\n"
