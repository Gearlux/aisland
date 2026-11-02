"""`aisland workspace test-stats on|off [PROJECT…] [--machine-only]`: whether the pytest plugin records a project's runs.

The plugin records every run of a workspace project (`docs/tests.md` → "The pytest plugin"); this switch turns that
off — for the whole workspace (no project named, those added later included) or for named projects, for everyone
(`workspace.toml`) or for this machine (`.git/config`). This machine's choice wins over the committed one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from datetime import date
from pathlib import Path
from typing import List, Optional

import pytest
from click.testing import CliRunner, Result

from aisland import cli, locations, pytest_stats, workspace_file

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's git configuration is neither read nor touched."""
    for key in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("JENKINS_URL", raising=False)


def _git(cwd: Path, *args: str) -> Optional[str]:
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else None


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A workspace holding `app` and `lib`; `lib`'s folder is `library`."""
    root = tmp_path / "acme"
    for folder in ("app", "library"):
        (root / folder / "tests").mkdir(parents=True)
        (root / folder / "pyproject.toml").write_text(f'[project]\nname = "{folder}"\nversion = "0.1.0"\n')
        (root / folder / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n")
    _git(root, "init", "-q", "-b", "main")
    (root / "workspace.toml").write_text(
        textwrap.dedent(
            """\
            [project."app"]
            url = "https://example.com/acme/app.git"

            [project."lib"]
            path = "library"
            url = "https://example.com/acme/lib.git"
            """
        )
    )
    return root


def _run(ws: Path, monkeypatch: pytest.MonkeyPatch, *args: str, cwd: Optional[Path] = None) -> Result:
    """The command run from the workspace (or ``cwd``), as a person runs it: from a wheel, the folder decides."""
    monkeypatch.setattr(cli, "get_workspace_root", lambda: ws)
    monkeypatch.chdir(cwd or ws)
    return CliRunner().invoke(cli.main, ["workspace", "test-stats", *args])


def _recorded(ws: Path) -> List[str]:
    """The folders whose runs the plugin records."""
    return [folder for folder in ("app", "library") if pytest_stats.records(ws, folder)]


# --- the default ------------------------------------------------------------------------------------------------------


def test_without_a_switch_every_project_is_recorded(ws: Path) -> None:
    assert _recorded(ws) == ["app", "library"]


# --- for everyone (workspace.toml) ------------------------------------------------------------------------------------


def test_off_without_a_project_turns_the_whole_workspace_off(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(ws, monkeypatch, "off")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> test runs not recorded: every project, and every one added later (in workspace.toml — commit it)",
        "  - app: not recorded",
        "  - lib: not recorded",
    ]
    assert workspace_file.load(ws).settings["test-stats"] is False
    assert _recorded(ws) == []


def test_a_project_added_later_follows_the_workspace(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "off")
    (ws / "tools").mkdir()
    with (ws / "workspace.toml").open("a") as handle:
        handle.write('\n[project."tools"]\nurl = "https://example.com/acme/tools.git"\n')
    assert not pytest_stats.records(ws, "tools")


def test_off_for_one_project_leaves_the_others_recorded(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(ws, monkeypatch, "off", "lib")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "==> test runs not recorded: lib (in workspace.toml — commit it)"
    assert workspace_file.load(ws).projects["lib"].others == {"test-stats": False}
    assert _recorded(ws) == ["app"]


def test_on_for_one_project_in_a_workspace_that_is_off(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "off")
    result = _run(ws, monkeypatch, "on", "app")
    assert result.output.splitlines() == [
        "==> test runs recorded: app (in workspace.toml — commit it)",
        "  - app: recorded",
        "  - lib: not recorded",
    ]
    assert workspace_file.load(ws).projects["app"].others == {"test-stats": True}
    assert _recorded(ws) == ["app"]


def test_on_without_a_project_leaves_no_switch_of_anyones(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "off")
    _run(ws, monkeypatch, "on", "app")
    _run(ws, monkeypatch, "off", "lib", "--machine-only")
    _run(ws, monkeypatch, "on")
    assert "test-stats" not in workspace_file.load(ws).settings
    assert all("test-stats" not in project.others for project in workspace_file.load(ws).projects.values())
    assert _git(ws, "config", "--local", "--get-regexp", r"^aisland\.test-stats\.") is None
    assert _recorded(ws) == ["app", "library"]


def test_a_committed_switch_drops_this_machines_choice_for_the_project(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(ws, monkeypatch, "off", "app", "--machine-only")
    _run(ws, monkeypatch, "on", "app")
    assert _git(ws, "config", "--local", "aisland.test-stats.app.record") is None
    assert _recorded(ws) == ["app", "library"]


# --- this machine only (.git/config) ----------------------------------------------------------------------------------


def test_machine_only_changes_this_machine_and_commits_nothing(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = (ws / "workspace.toml").read_bytes()
    result = _run(ws, monkeypatch, "off", "app", "--machine-only")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> test runs not recorded on this machine: app (nothing committed)",
        "  - app: not recorded (this machine)",
        "  - lib: recorded",
    ]
    assert (ws / "workspace.toml").read_bytes() == before
    assert _git(ws, "config", "--local", "aisland.test-stats.app.record") == "false"
    assert _recorded(ws) == ["library"]


def test_machine_only_without_a_project_turns_every_project_off_here(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "off", "--machine-only")
    assert _git(ws, "config", "--local", "aisland.test-stats.default") == "false"
    assert _recorded(ws) == []


def test_this_machines_choice_wins_over_the_committed_one(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "off")
    _run(ws, monkeypatch, "on", "lib", "--machine-only")
    assert _recorded(ws) == ["library"]


# --- refusals and what the plugin never does ---------------------------------------------------------------------------


def test_a_name_no_project_has_is_refused_before_anything_changes(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = (ws / "workspace.toml").read_bytes()
    result = _run(ws, monkeypatch, "off", "nosuch")
    assert result.exit_code == 1
    assert "Error: 'nosuch' is no project of the workspace (its projects: app, lib)" in result.output
    assert (ws / "workspace.toml").read_bytes() == before


def test_a_folder_name_is_not_a_project_name(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(ws, monkeypatch, "off", "library").exit_code == 1


def test_an_unreadable_workspace_file_never_costs_a_run(ws: Path) -> None:
    (ws / "workspace.toml").write_text("not = valid = toml")
    assert _recorded(ws) == ["app", "library"]


def test_a_workspace_without_a_file_or_a_repository_records(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    assert pytest_stats.records(tmp_path, "app")


# --- outside an aisland workspace --------------------------------------------------------------------------------------


def test_outside_an_aisland_workspace_the_switch_is_refused_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aisland from a wheel in `~/code/app`, a repository of its own: the plugin records nothing there, so `off` has
    nothing to switch — it says so instead of writing a `workspace.toml` into `~/code`."""
    code = tmp_path / "code"
    (code / "app").mkdir(parents=True)
    _git(code / "app", "init", "-q")
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    for args in (["off"], ["off", "--machine-only"]):
        result = _run(code, monkeypatch, *args, cwd=code / "app")
        assert result.exit_code == 1
        assert f"Error: {code} is no aisland workspace (it holds no workspace.toml or .gitmodules)" in result.output
    assert sorted(path.name for path in code.iterdir()) == ["app"]


def test_machine_only_in_a_workspace_that_is_no_repository_is_refused(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This machine's switch lives in `.git/config`; without one it would be written nowhere and still be reported."""
    shutil.rmtree(ws / ".git")
    result = _run(ws, monkeypatch, "off", "--machine-only")
    assert result.exit_code == 1
    assert f"Error: {ws} is no git repository: this machine's switch lives in its .git/config" in result.output
    assert _recorded(ws) == ["app", "library"]


# --- end to end: a pytest run, and the report -----------------------------------------------------------------------------


def test_a_run_of_a_project_that_is_off_writes_nothing(
    pytester: pytest.Pytester, ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pytest_stats, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(pytest_stats, "STATS_DIR", ws / "stats")
    _run(ws, monkeypatch, "off", "lib")
    for folder in ("app", "library"):
        monkeypatch.chdir(ws / folder)
        pytester.runpytest_inprocess("-p", "no:aisland_test_stats", "-p", "no:cacheprovider", plugins=[pytest_stats])
    assert (ws / "stats" / "app" / f"{date.today()}.jsonl").is_file()
    assert not (ws / "stats" / "library").exists()


def test_the_report_says_the_project_is_not_recorded_and_how_to_turn_it_on(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pytest_stats, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(pytest_stats, "STATS_DIR", ws / "stats")
    monkeypatch.setattr(pytest_stats, "JENKINS_JOBS_DIR", ws / "jobs")
    _run(ws, monkeypatch, "off", "app")
    text = CliRunner().invoke(cli.main, ["tests", "report", "app"]).output
    assert "Test runs of app are not recorded: `aisland workspace test-stats on app` records them again." in text
    data = json.loads(CliRunner().invoke(cli.main, ["tests", "report", "app", "--json"]).output)
    assert data["recording"] is False


def test_the_report_names_the_machine_only_switch_when_this_machine_turned_it_off(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pytest_stats, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(pytest_stats, "STATS_DIR", ws / "stats")
    monkeypatch.setattr(pytest_stats, "JENKINS_JOBS_DIR", ws / "jobs")
    _run(ws, monkeypatch, "off", "--machine-only")
    text = CliRunner().invoke(cli.main, ["tests", "report", "app"]).output
    assert "`aisland workspace test-stats on app --machine-only` records them again." in text
