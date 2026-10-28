"""Where aisland finds its workspace and keeps its state (`aisland.locations`).

Two ways to be installed, two rules. From a checkout (editable, `aisland/pyproject.toml` beside the package) every
path follows the package's own location, exactly as before wheels were a way to install aisland: the folder you run
it in and `AISLAND_WORKSPACE` change nothing. From a wheel the package sits in `site-packages`, so the workspace is
the folder you run it in — the nearest one holding a `.git` folder, or `AISLAND_WORKSPACE` — and the state goes to
its `.aisland/`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import click
import pytest

from aisland import locations, manager, pytest_stats, supervisor
from aisland.services import jenkins
from tests.workspace_layout import needs_workspace

AISLAND_PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = AISLAND_PROJECT.parent


# --- from a checkout: every path as before ----------------------------------------------------------------------


def test_this_checkout_is_recognised_as_one() -> None:
    assert locations.FROM_CHECKOUT is True


@needs_workspace
def test_from_a_checkout_the_folder_you_run_in_and_the_variable_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AISLAND_WORKSPACE", str(tmp_path))
    assert locations.workspace_root() == WORKSPACE
    assert manager.get_workspace_root() == WORKSPACE
    assert locations.state_dir() == AISLAND_PROJECT
    assert locations.workspace_found() is True


@needs_workspace
def test_from_a_checkout_the_state_stays_in_aislands_own_folder() -> None:
    assert manager.BASE_DIR == AISLAND_PROJECT
    assert manager.DATA_DIR == AISLAND_PROJECT / "data"
    assert manager.BIN_DIR == AISLAND_PROJECT / "bin"
    assert manager.TOOLS_DIR == AISLAND_PROJECT / "tools"
    assert supervisor.DATA_DIR == AISLAND_PROJECT / "data"
    assert supervisor.BIN_DIR == AISLAND_PROJECT / "bin"
    assert pytest_stats.WORKSPACE_ROOT == WORKSPACE
    assert pytest_stats.STATS_DIR == AISLAND_PROJECT / "data" / "test-stats"
    assert pytest_stats.JENKINS_JOBS_DIR == AISLAND_PROJECT / "data" / "jenkins" / "jobs"


def test_jenkins_finds_the_root_the_way_the_rest_of_aisland_does() -> None:
    """It counted three folders up from its own file, which in a wheel lands in the venv."""
    source = Path(jenkins.__file__).read_text()
    assert "parents[3]" not in source


# --- from a wheel: the folder you run it in ----------------------------------------------------------------------


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    """`acme/` (a git repository) holding the submodule `models/` (a `.git` FILE, as git writes it)."""
    root = tmp_path / "acme"
    (root / ".git").mkdir(parents=True)
    (root / "models" / "src").mkdir(parents=True)
    (root / "models" / ".git").write_text("gitdir: ../.git/modules/models\n")
    return root


def test_from_a_wheel_the_workspace_is_the_repository_you_run_in(acme: Path) -> None:
    assert locations.wheel_workspace_root(acme, {}) == acme


def test_from_a_wheel_a_submodule_s_folder_belongs_to_its_workspace(acme: Path) -> None:
    assert locations.wheel_workspace_root(acme / "models" / "src", {}) == acme


def test_from_a_wheel_the_variable_names_the_workspace(acme: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert locations.wheel_workspace_root(elsewhere, {"AISLAND_WORKSPACE": str(acme)}) == acme


def test_from_a_wheel_outside_any_repository_there_is_no_workspace(tmp_path: Path) -> None:
    """pytest's temporary folders sit in no git repository; an empty variable counts as unset."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert locations.find_workspace(plain) is None
    assert locations.wheel_workspace_root(plain, {"AISLAND_WORKSPACE": ""}) is None


def test_from_a_wheel_the_state_goes_to_the_workspace_s_aisland_folder(
    acme: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.chdir(acme / "models")
    monkeypatch.delenv("AISLAND_WORKSPACE", raising=False)
    assert locations.workspace_root() == acme
    assert locations.state_dir() == acme / ".aisland"
    assert locations.workspace_found() is True


def test_from_a_wheel_a_variable_naming_no_folder_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setenv("AISLAND_WORKSPACE", str(tmp_path / "missing"))
    assert locations.workspace_found() is False
    assert locations.missing_workspace_message() == (
        f"AISLAND_WORKSPACE={tmp_path / 'missing'} is not a folder. Set it to your workspace, or unset it to use "
        "the git repository you run aisland in."
    )


def test_from_a_wheel_outside_a_workspace_the_message_says_how_to_get_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setattr(locations, "find_workspace", lambda start: None)
    monkeypatch.delenv("AISLAND_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert locations.workspace_found() is False
    assert locations.workspace_root() == tmp_path.resolve()  # importing never fails; the CLI refuses
    assert locations.missing_workspace_message() == (
        f"No workspace here: {tmp_path.resolve()} is in no git repository and holds no project. Run aisland in "
        "your workspace or one of its projects, set AISLAND_WORKSPACE to it, or create one with "
        "`aisland workspace init <folder>`."
    )


# --- from a wheel: a folder you may not enter --------------------------------------------------------------------

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root enters a chmod 000 folder all the same")


@pytest.fixture
def locked(tmp_path: Path) -> Iterator[Path]:
    """`start/locked/`, a folder nobody may enter (`chmod 000`, as `/tmp/snap-private-tmp` is on Linux); its mode is
    put back afterwards so pytest can delete it."""
    folder = tmp_path / "start" / "locked"
    folder.mkdir(parents=True)
    folder.chmod(0o000)
    yield folder
    folder.chmod(0o755)


@needs_permissions
def test_from_a_wheel_a_subfolder_you_may_not_enter_counts_as_no_project(
    locked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing `aisland.manager` from `/tmp` raised `PermissionError` on `snap-private-tmp/pyproject.toml`."""
    start = locked.parent
    (start / "alpha").mkdir()
    (start / "alpha" / "pyproject.toml").write_text("")
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.chdir(start)
    monkeypatch.delenv("AISLAND_WORKSPACE", raising=False)
    assert locations.holds_projects(start) is True
    assert locations.workspace_root() == start
    assert locations.state_dir() == start / ".aisland"


@needs_permissions
def test_from_a_wheel_a_folder_holding_only_a_locked_subfolder_is_no_workspace(locked: Path) -> None:
    assert locations.holds_projects(locked.parent) is False
    assert locations.find_workspace(locked.parent) is None


@needs_permissions
def test_from_a_wheel_a_start_folder_you_may_not_read_is_looked_past(locked: Path) -> None:
    """Its own markers count as absent, so the lookup goes on to the folders above it."""
    assert locations.find_workspace(locked) is None
    assert locations.alias_names(locked) == {}
    assert locations.is_alias(locked / "ml", locked.parent) is False


# --- the CLI from a wheel, outside a workspace ------------------------------------------------------------------


@pytest.fixture
def nowhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """aisland from a wheel, run in a folder that is in no git repository."""
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setattr(locations, "find_workspace", lambda start: None)
    monkeypatch.delenv("AISLAND_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_workspace_command_is_refused_and_writes_nothing(nowhere: Path) -> None:
    from click.testing import CliRunner

    from aisland.cli import main

    result = CliRunner().invoke(main, ["setup", "-y"])
    assert result.exit_code == 1
    assert result.output.startswith("Error: No workspace here: ")
    assert list(nowhere.iterdir()) == []


@pytest.mark.parametrize("args", [["services", "--help"], ["workspace", "--help"], ["wheels", "--help"], ["--help"]])
def test_help_and_the_workspace_free_commands_run_anywhere(nowhere: Path, args: list) -> None:
    from click.testing import CliRunner

    from aisland.cli import main

    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Usage: ")


def test_setup_from_a_wheel_without_a_venv_says_how_to_make_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)
    with pytest.raises(click.ClickException) as refused:
        manager.setup_workspace(non_interactive=True)
    assert refused.value.message == (
        f"Virtual environment not found at {tmp_path / '.venv'}. Create it and install aisland into it: "
        "uv venv --seed .venv && .venv/bin/pip install <the aisland wheel>"
    )


# --- the git aliases' install -----------------------------------------------------------------------------------


def test_a_failing_alias_install_shows_the_script_s_output_when_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "setup_git.sh"
    script.write_text('echo "fatal: not a git repository"\nexit 3\n')
    monkeypatch.setattr(manager, "SETUP_GIT_SCRIPT", script)
    assert manager.install_git_aliases(tmp_path, quiet=True) is False
    captured = capsys.readouterr()
    assert captured.out == "fatal: not a git repository\n"
    assert captured.err == f"  - Error: installing the git aliases failed (bash {script} {tmp_path}).\n"
