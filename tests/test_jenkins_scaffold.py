"""Regression tests for `aisland jenkins scaffold` over the full workspace.

Specifically: scaffolding all projects MUST include aisland itself (regression
for the historical `and child.name != "aisland"` filter that silently skipped
aisland's own CI surface).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from aisland.services.jenkins import JenkinsService


def _make_pyproject(dir_: Path, name: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    # The scaffolder probes for an inner package dir with __init__.py.
    pkg = dir_ / name.replace("-", "")
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").touch()


def test_scaffold_all_projects_includes_aisland(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When scaffolding all projects, aisland MUST be in the list of recursed
    targets — previously it was filtered out by `child.name != "aisland"`.
    """
    # Build a fake workspace with three sibling projects, one of which is aisland.
    for name in ("aisland", "marainer", "traidwind"):
        _make_pyproject(tmp_path / name, name)

    from aisland import manager

    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)

    # Capture the project names the scaffolder recurses into instead of
    # actually generating files (the file-writing path is exercised
    # end-to-end by ad-hoc CLI runs).
    visited: List[str] = []
    original = JenkinsService.scaffold_pipelines

    def _spy(self: JenkinsService, project_name: Any = None, force: bool = False) -> None:
        if project_name is None:
            # Fall through to the real "iterate all projects" branch.
            return original(self, project_name=None, force=force)
        visited.append(project_name)

    monkeypatch.setattr(JenkinsService, "scaffold_pipelines", _spy)

    svc = JenkinsService()
    svc.scaffold_pipelines(project_name=None)

    assert "aisland" in visited, "aisland must not be filtered out of the all-projects loop"
    assert {"aisland", "marainer", "traidwind"} <= set(visited)
