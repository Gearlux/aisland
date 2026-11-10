"""``aisland check``: every gate runs once, in the folder it belongs to, and a failed gate shows what it printed."""

from __future__ import annotations

from pathlib import Path
from typing import List

import click
import pytest
from click.testing import CliRunner

from aisland import locations
from aisland.cli import main

TOOLS = ("isort", "black", "flake8", "mypy", "pytest", "act")


def _invoke(*args: str) -> click.testing.Result:
    return CliRunner().invoke(main, list(args))


def _project(root: Path, name: str) -> Path:
    (root / name).mkdir(parents=True)
    (root / name / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    return root / name


def _install_tools(root: Path, fail_in: str = "", fail_tool: str = "") -> Path:
    """A ``.venv/bin`` of tools that log ``<tool> <folder> <arguments>`` and, run in ``fail_in``, fail loudly.

    Returns the log. Only ``fail_tool`` fails: ``fail_in`` is the folder it fails in (every folder when empty)."""
    bin_dir = root / ".venv" / "bin"
    log = root / "calls.log"
    for tool in TOOLS:
        lines = ["#!/bin/sh", f'echo "{tool} $(pwd -P) $*" >> "{log}"']
        if tool == fail_tool:
            fails = (
                f'*/{fail_in}) echo "{tool} found a problem"; exit 1;;'
                if fail_in
                else f'*) echo "{tool} found a problem"; exit 1;;'
            )
            lines.append(f'case "$(pwd -P)" in {fails} esac')
        script = bin_dir / tool
        script.write_text("\n".join(lines) + "\n")
        script.chmod(0o755)
    return log


def _calls(log: Path) -> List[List[str]]:
    return [line.split(" ", 2) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    return tmp_path


def test_every_gate_passes_once_and_runs_in_the_folder_it_belongs_to(workspace: Path) -> None:
    app = _project(workspace, "app")
    log = _install_tools(workspace)

    result = _invoke("check")

    assert result.exit_code == 0, result.output
    for tool in ("isort", "black", "flake8", "mypy", "pytest"):
        assert f"[PASS] {tool}" in result.output
    calls = _calls(log)
    assert [tool for tool, _folder, _args in calls] == ["isort", "black", "flake8", "mypy", "pytest"]
    folders = {tool: folder for tool, folder, _args in calls}
    assert {folders[tool] for tool in ("isort", "black", "flake8", "pytest")} == {str(app.resolve())}
    assert folders["mypy"] == str(workspace.resolve())
    assert [args for tool, _folder, args in calls if tool == "mypy"] == ["app"]


def test_the_formatters_only_check_and_show_the_change_they_would_make(workspace: Path) -> None:
    _project(workspace, "app")
    log = _install_tools(workspace)

    result = _invoke("check", "app")

    assert result.exit_code == 0, result.output
    args = {tool: args for tool, _folder, args in _calls(log)}
    assert args["isort"] == "--check-only --diff ."
    assert args["black"] == "--check --diff ."


def test_write_lets_the_formatters_rewrite_the_files(workspace: Path) -> None:
    _project(workspace, "app")
    log = _install_tools(workspace)

    result = _invoke("check", "--write", "app")

    assert result.exit_code == 0, result.output
    args = {tool: args for tool, _folder, args in _calls(log)}
    assert args["isort"] == "."
    assert args["black"] == "."
    assert args["flake8"] == "."


def test_a_failed_gate_runs_once_and_shows_the_output_it_printed_once(workspace: Path) -> None:
    _project(workspace, "app")
    log = _install_tools(workspace, fail_tool="pytest")

    result = _invoke("check", "pytest", "app")

    assert result.exit_code == 1
    assert "[FAIL] pytest" in result.output
    assert result.output.count("pytest found a problem") == 1
    assert [tool for tool, _folder, _args in _calls(log)] == ["pytest"]
    assert "Error: the quality gates failed: app" in result.stderr


def test_a_gate_that_fails_does_not_stop_the_gates_after_it(workspace: Path) -> None:
    _project(workspace, "app")
    log = _install_tools(workspace, fail_tool="black")

    result = _invoke("check", "app")

    assert result.exit_code == 1
    assert "[FAIL] black" in result.output
    assert "[PASS] pytest" in result.output
    assert [tool for tool, _folder, _args in _calls(log)] == ["isort", "black", "flake8", "mypy", "pytest"]


def test_a_tool_named_alone_runs_only_that_tool(workspace: Path) -> None:
    _project(workspace, "app")
    log = _install_tools(workspace)

    result = _invoke("check", "flake8")

    assert result.exit_code == 0
    assert [tool for tool, _folder, _args in _calls(log)] == ["flake8"]


def test_with_several_projects_a_summary_says_which_failed(workspace: Path) -> None:
    _project(workspace, "app")
    _project(workspace, "lib")
    _install_tools(workspace, fail_in="lib", fail_tool="pytest")

    result = _invoke("check", "pytest")

    assert result.exit_code == 1
    assert "Quality Gate Summary:" in result.output
    assert "app                       : SUCCESS" in result.output
    assert "lib                       : FAILURE" in result.output
    assert "Error: the quality gates failed: lib" in result.stderr


def test_act_is_a_gate_only_for_a_project_with_workflows(workspace: Path) -> None:
    _project(workspace, "app")
    (_project(workspace, "lib") / ".github" / "workflows").mkdir(parents=True)
    log = _install_tools(workspace)

    result = _invoke("check", "act")

    assert result.exit_code == 0, result.output
    assert [(tool, Path(folder).name, args) for tool, folder, args in _calls(log)] == [("act", "lib", "--list")]


def test_the_help_names_what_each_word_takes() -> None:
    usage = CliRunner().invoke(main, ["check", "--help"]).output.splitlines()[0]
    assert usage.endswith("check [OPTIONS] [TOOL] [PROJECT]")
