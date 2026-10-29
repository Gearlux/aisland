"""Cyclic workspace projects install in ONE `uv pip install`, and a failure is reported once.

Observed on a fresh machine: one project's `dev` extra requires a second project that requires the
first, so installing either alone makes uv look for the other on the index and fail. The
old loop then also tried (and failed) every dependent, burying the first error in a dozen more.
"""

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from aisland import manager as m
from aisland import selection
from aisland.projects import install_groups


def _project(name: str, *deps: str) -> dict:
    return {"name": name, "path": Path(f"/ws/{name}"), "dependencies": list(deps), "has_dev": False}


def _names(groups: List[List[dict]]) -> List[List[str]]:
    return [[p["name"] for p in g] for g in groups]


class TestInstallGroups:
    def test_acyclic_projects_are_singletons_dependencies_first(self) -> None:
        groups = install_groups([_project("app", "lib>=0.1"), _project("lib")])
        assert _names(groups) == [["lib"], ["app"]]

    def test_a_cycle_is_one_group(self) -> None:
        projects = [_project("app", "plugin>=0.1"), _project("plugin", "app>=0.1")]
        assert sorted(_names(install_groups(projects))[0]) == ["app", "plugin"]
        assert len(install_groups(projects)) == 1

    def test_the_cycle_still_follows_its_own_dependencies_and_precedes_its_dependents(self) -> None:
        projects = [
            _project("tool", "plugin"),
            _project("plugin", "app", "base"),
            _project("app", "plugin"),
            _project("base"),
        ]
        names = _names(install_groups(projects))
        assert names[0] == ["base"]
        assert sorted(names[1]) == ["app", "plugin"]
        assert names[2] == ["tool"]

    def test_external_dependencies_and_self_references_are_ignored(self) -> None:
        assert _names(install_groups([_project("a", "numpy>=1", "a[dev]")])) == [["a"]]


class TestRunInstalls:
    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
        seen: List[List[str]] = []

        def fake(cmd: List[str], cwd: Any = None) -> None:
            seen.append(cmd)
            if any("bad" in part for part in cmd):
                raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(subprocess, "check_call", fake)
        monkeypatch.setattr(selection, "editable_target", lambda root, path, name: f"{path}[dev]")
        return seen

    def test_a_cycle_is_installed_with_one_command(self, calls: List[List[str]], tmp_path: Path) -> None:
        projects = [_project("app", "plugin"), _project("plugin", "app")]
        failed, skipped = m._run_installs(tmp_path, "uv", projects)
        assert (failed, skipped) == ([], [])
        assert len(calls) == 1
        assert calls[0].count("-e") == 2

    def test_dependents_of_a_failure_are_skipped_not_attempted(self, calls: List[List[str]], tmp_path: Path) -> None:
        projects = [_project("bad"), _project("child", "bad"), _project("grandchild", "child"), _project("other")]
        failed, skipped = m._run_installs(tmp_path, "uv", projects)
        assert failed == ["bad"]
        assert skipped == ["child", "grandchild"]
        attempted = [part for cmd in calls for part in cmd if part.startswith("/ws/")]
        assert attempted == ["/ws/bad[dev]", "/ws/other[dev]"]  # unrelated project still installs

    def test_the_report_names_the_failure_and_the_skips(
        self, calls: List[List[str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        m._run_installs(tmp_path, "uv", [_project("bad"), _project("child", "bad")])
        captured = capsys.readouterr()
        assert "Failed to install bad" in captured.err  # a failure setup goes on past: stderr, and the run exits 1
        assert "Skipping child" in captured.out and "bad" in captured.out
