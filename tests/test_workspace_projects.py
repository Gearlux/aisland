"""`aisland workspace projects`: the projects in dependency order, the answer the harness commands read.

The harness's `/commit-all` and `/ship` walk the projects dependencies first. They ask this command, never an import of
aisland's modules, which `docs/stability.md` keeps out of the interface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aisland import locations
from aisland.cli import main


def _project(root: Path, folder: str, name: str, *dependencies: str) -> None:
    (root / folder).mkdir()
    requires = ", ".join(f'"{dependency}"' for dependency in dependencies)
    (root / folder / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = [{requires}]\n'
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """app needs acme-models (folder `models`), which needs lib: alphabetical order would be wrong twice."""
    _project(tmp_path, "app", "app", "acme-models")
    _project(tmp_path, "models", "acme-models", "lib>=0.1")
    _project(tmp_path, "lib", "lib")
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    return tmp_path


@pytest.mark.usefixtures("workspace")
def test_the_folders_come_one_a_line_dependencies_first() -> None:
    result = CliRunner().invoke(main, ["workspace", "projects"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["lib", "models", "app"]


@pytest.mark.usefixtures("workspace")
def test_json_adds_each_projects_distribution_name() -> None:
    result = CliRunner().invoke(main, ["workspace", "projects", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "projects": [
            {"folder": "lib", "name": "lib"},
            {"folder": "models", "name": "acme-models"},
            {"folder": "app", "name": "app"},
        ]
    }


def test_outside_a_workspace_it_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_found", lambda: False)
    result = CliRunner().invoke(main, ["workspace", "projects"])
    assert result.exit_code == 1
    assert "Error:" in result.output
