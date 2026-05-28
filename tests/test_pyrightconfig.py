"""Tests for the ``pyrightconfig.json`` generator wired into ``aisland setup``.

The generator (``manager._write_pyrightconfig``) replaces the previously
hand-maintained, hardcoded-path ``pyrightconfig.json`` at the workspace root
with a portable, regenerated-on-every-setup artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from aisland.manager import _write_pyrightconfig


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
