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


def test_scaffold_emits_directory_example_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Verify-Examples step MUST also execute directory examples
    (``examples/*/run.py``) in ALL THREE generated files — the opt-in glob that
    runs e.g. confluid's ``examples/ml_experiments/run.py`` while skipping
    installable apps that ship no run.py (network-dependent examples).
    """
    _make_pyproject(tmp_path / "proj", "proj")

    from aisland import manager

    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)

    svc = JenkinsService()
    # The Jenkins linter is unreachable in tests -> warn-and-skip, files still written.
    svc.scaffold_pipelines(project_name="proj", force=True)

    generated = {
        "Jenkinsfile": tmp_path / "proj" / "Jenkinsfile",
        "Jenkinsfile.local": tmp_path / "proj" / "Jenkinsfile.local",
        "ci.yml": tmp_path / "proj" / ".github" / "workflows" / "ci.yml",
    }
    for desc, path in generated.items():
        assert path.exists(), f"{desc} was not generated"
        content = path.read_text()
        assert "examples/*.py" in content, f"{desc} lost the flat-example loop"
        assert "examples/*/run.py" in content, f"{desc} must run directory examples via examples/*/run.py"


def test_the_regenerate_header_is_a_command_the_cli_accepts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The header is the ONLY regeneration instruction an operator or agent has, so it must run.

    It used to say `aisland jenkins scaffold --project <name> --force`, which fails with
    `Error: No such option: --project` — the project is a positional argument. This parses the
    emitted line with the REAL click command rather than string-matching, so the pin tracks the
    CLI's signature instead of restating it.
    """
    _make_pyproject(tmp_path / "proj", "proj")

    from aisland import manager
    from aisland.cli import jenkins_scaffold

    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)
    JenkinsService().scaffold_pipelines(project_name="proj", force=True)

    generated = [
        tmp_path / "proj" / "Jenkinsfile",
        tmp_path / "proj" / "Jenkinsfile.local",
        tmp_path / "proj" / ".github" / "workflows" / "ci.yml",
    ]
    for path in generated:
        line = next(ln for ln in path.read_text().splitlines() if "To regenerate:" in ln)
        argv = line.split("To regenerate:")[1].split()

        assert argv[:3] == ["aisland", "jenkins", "scaffold"], f"{path.name}: unexpected command: {argv}"
        # The command's own parser is the authority on what the rest means.
        with jenkins_scaffold.make_context("scaffold", argv[3:], resilient_parsing=False) as ctx:
            assert ctx.params == {"project": "proj", "force": True}, f"{path.name}: {ctx.params}"
