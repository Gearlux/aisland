"""``aisland swiftbar init``: the menu scripts watch every project's Jenkins jobs and GitHub repository, dependencies
first, and SwiftBar is pointed at them only when the user says yes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from aisland import locations
from aisland.services import base, swiftbar

_REAL_RUN = subprocess.run


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``acme`` with ``lib`` and ``app`` (which needs ``lib``), each with a GitHub origin; Jenkins has ``lib`` and
    ``app.local``."""
    root = tmp_path / "acme"
    for name, deps in (("app", ["lib"]), ("lib", [])):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = {deps!r}\n'
        )
        _REAL_RUN(["git", "init", "-q", str(folder)], check=True)
        _REAL_RUN(["git", "remote", "add", "origin", f"git@github.com:acme/{name}.git"], cwd=folder, check=True)
    _REAL_RUN(["git", "init", "-q", str(root)], check=True)
    data = tmp_path / "data"
    (data / "jenkins" / "jobs" / "lib").mkdir(parents=True)
    (data / "jenkins" / "jobs" / "app.local").mkdir(parents=True)
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    monkeypatch.setattr(base, "DATA_DIR", tmp_path / "state")
    monkeypatch.setattr(swiftbar, "DATA_DIR", data)
    monkeypatch.setattr(swiftbar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(swiftbar.JenkinsService, "url", lambda self: "http://localhost:9080")
    return root


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Every command but git's the service runs, recorded instead of run; its stop and launch are recorded too."""
    ran: List[List[str]] = []

    def run(cmd: Any, *args: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        if cmd[0] == "git":
            return _REAL_RUN(cmd, *args, **kwargs)
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swiftbar.subprocess, "run", run)
    monkeypatch.setattr(swiftbar.SwiftBarService, "stop", lambda self: ran.append(["<stop>"]))
    monkeypatch.setattr(swiftbar.SwiftBarService, "launch", lambda self: ran.append(["<launch>"]))
    return ran


def test_the_scripts_watch_each_projects_jobs_and_repository_dependencies_first(
    workspace: Path, commands: List[List[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(swiftbar.click, "confirm", lambda *a, **k: False)
    bar = swiftbar.SwiftBarService()

    bar.init_scripts()

    jenkins = (bar.plugins_dir / "jenkins_status.5m.sh").read_text()
    github = (bar.plugins_dir / "gh_status.10m.sh").read_text()
    assert 'PROJECTS=("lib" "app.local")' in jenkins
    assert 'REPOS=$(echo -e "acme/lib\\nacme/app")' in github
    for script in (jenkins, github):
        assert script.startswith("#!/bin/bash\n# ===")
        assert "<[" not in script
        assert _REAL_RUN(["bash", "-n"], input=script, text=True).returncode == 0
    out = capsys.readouterr().out
    assert "Monitored Jenkins jobs: lib, app.local" in out
    assert "Monitored GitHub repos: acme/lib, acme/app" in out
    assert f"Please manually point SwiftBar to: {bar.plugins_dir}" in out
    assert commands == []


def test_a_yes_points_swiftbar_at_the_scripts_and_starts_it_again(
    workspace: Path, commands: List[List[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swiftbar.click, "confirm", lambda *a, **k: True)
    bar = swiftbar.SwiftBarService()

    bar.init_scripts()

    defaults = ["defaults", "write", "com.ameba.SwiftBar"]
    assert commands == [
        ["<stop>"],
        [*defaults, "PluginDirectory", str(bar.plugins_dir)],
        [*defaults, "PluginsDirectory", str(bar.plugins_dir)],
        [*defaults, "MakePluginExecutable", "-bool", "YES"],
        [*defaults, "DisabledPlugins", "-array", "CLAUDE.md", "AGENTS.md", "GEMINI.md"],
        ["open", "-g", f"swiftbar://changePluginsDirectory?path={bar.plugins_dir}"],
        ["<launch>"],
    ]
