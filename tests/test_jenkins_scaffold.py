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
    for name in ("aisland", "matrainer", "traidwind"):
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
    assert {"aisland", "matrainer", "traidwind"} <= set(visited)


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


def _make_pyproject_with_slow_marker(dir_: Path, name: str) -> None:
    _make_pyproject(dir_, name)
    (dir_ / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\n\n'
        "[tool.pytest.ini_options]\n"
        "markers = [\"slow: heavy end-to-end tests; deselect with -m 'not slow'.\"]\n",
        encoding="utf-8",
    )


def _scaffolded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make: Any) -> dict[str, str]:
    from aisland import manager

    make(tmp_path / "proj", "proj")
    (tmp_path / ".gitmodules").write_text("", encoding="utf-8")
    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)
    JenkinsService().scaffold_pipelines(project_name="proj", force=True)
    return {
        "Jenkinsfile": (tmp_path / "proj" / "Jenkinsfile").read_text(encoding="utf-8"),
        "Jenkinsfile.local": (tmp_path / "proj" / "Jenkinsfile.local").read_text(encoding="utf-8"),
        "ci.yml": (tmp_path / "proj" / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
    }


class TestTheSlowMarkerSplitsTheTestInvocations:
    """A registered `slow` pytest marker IS the declaration: the routine run deselects the slow
    tests and a separate stage/job runs them, so a long end-to-end smoke test leaves every
    push's critical path while its dep-bump-drift coverage is preserved. A project WITHOUT the
    marker gets byte-identical output to before — opting in never churns the fleet's files."""

    def test_a_registered_slow_marker_splits_all_three_artifacts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)
        for name, content in files.items():
            assert "-m 'not slow'" in content, f"{name}: the routine run must deselect the slow tests"
            assert "-m slow" in content, f"{name}: the slow leg is missing"
        assert "stage('Slow Tests')" in files["Jenkinsfile"]
        assert "stage('Slow Tests')" in files["Jenkinsfile.local"]
        assert "slow-tests:" in files["ci.yml"]
        # The GitHub slow leg runs PARALLEL to unit-tests (same dependency), not after it.
        assert files["ci.yml"].count("needs: [quality-gates]") >= 2

    def test_exit_code_5_is_tolerated_in_the_slow_leg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A registered marker with nothing currently marked is a valid state, not a failure."""
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)
        assert "rc != 0 && rc != 5" in files["Jenkinsfile"]
        assert "rc != 0 && rc != 5" in files["Jenkinsfile.local"]
        assert "-ne 5" in files["ci.yml"]

    def test_a_project_without_the_marker_is_untouched(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject)
        for name, content in files.items():
            assert "not slow" not in content, f"{name}: an unmarked project must not deselect anything"
            assert "Slow Tests" not in content and "slow-tests:" not in content, name
