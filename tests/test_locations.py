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
from typing import Callable, Dict, Iterator

import click
import pytest

from aisland import locations, paths, pytest_stats, supervisor
from aisland.ci import stats_report
from aisland.services import jenkins
from aisland.setup import git_settings, manager, mcp_config
from aisland.workspace import projects, sources
from tests.workspace_layout import needs_workspace

AISLAND_PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = AISLAND_PROJECT.parent


# --- from a checkout: every path as before ----------------------------------------------------------------------


@needs_workspace
def test_this_checkout_is_recognised_as_one() -> None:
    """aisland checked out in the workspace that lists it; a clone of its own (CI's) is no checkout."""
    assert locations.FROM_CHECKOUT is True


def _clone(tmp_path: Path, where: str = "aisland") -> Path:
    """A checkout of aisland at ``tmp_path/where``: its pyproject.toml beside the package, its own TASKS.md."""
    checkout = tmp_path / where
    (checkout / "aisland").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text('[project]\nname = "aisland"\n')
    (checkout / "TASKS.md").write_text("aisland's backlog\n")
    return checkout


@pytest.mark.parametrize(
    "files, where, listed",
    [
        ({}, "aisland", False),
        ({"TASKS.md": "", ".git/HEAD": ""}, "aisland", False),
        ({".gitmodules": '[submodule "lib"]\n\tpath = lib\n'}, "aisland", False),
        ({".gitmodules": '[submodule "aisland"]\n\tpath = aisland\n\turl = u\n'}, "aisland", True),
        ({".gitmodules": '[submodule "x"]\n\tpath = tools/aisland\n'}, "tools/aisland", True),
        ({"workspace.toml": '[project."aisland"]\nurl = "u"\n'}, "aisland", True),
        ({"workspace.toml": '[project."aisland"]\npath = "ais"\n'}, "ais", True),
        ({"workspace.toml": "not toml ["}, "aisland", False),
    ],
    ids=[
        "a clone of its own",
        "a repository above that lists nothing",
        "a superproject listing other projects",
        "a submodule of the workspace",
        "a submodule at a nested path",
        "a project of workspace.toml",
        "a project of workspace.toml in a folder of its own name",
        "a workspace.toml aisland cannot read",
    ],
)
def test_a_checkout_runs_as_one_only_where_a_workspace_lists_it(
    tmp_path: Path, files: Dict[str, str], where: str, listed: bool
) -> None:
    """A contributor's clone of aisland, kept anywhere, works like a wheel: on the workspace it is run in."""
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    checkout = _clone(tmp_path, where)
    assert locations.listing_workspace(checkout) == (tmp_path if listed else None)
    assert locations.runs_as_checkout(checkout) is listed


def test_a_folder_without_aislands_pyproject_is_no_checkout(tmp_path: Path) -> None:
    (tmp_path / ".gitmodules").write_text('[submodule "aisland"]\n\tpath = aisland\n')
    (tmp_path / "aisland" / "aisland").mkdir(parents=True)
    assert not locations.runs_as_checkout(tmp_path / "aisland")


def test_a_checkout_s_workspace_is_the_one_that_lists_it_not_its_own_backlog(tmp_path: Path) -> None:
    """A workspace listing aisland in workspace.toml alone holds no .gitmodules or TASKS.md: the clone's own TASKS.md
    must not make the clone the workspace."""
    (tmp_path / "workspace.toml").write_text('[project."aisland"]\nurl = "u"\n')
    checkout = _clone(tmp_path)
    assert locations.checkout_workspace_root(checkout / "aisland" / "manager.py") == tmp_path


@needs_workspace
def test_from_a_checkout_the_folder_you_run_in_and_the_variable_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AISLAND_WORKSPACE", str(tmp_path))
    assert locations.workspace_root() == WORKSPACE
    assert projects.get_workspace_root() == WORKSPACE
    assert locations.state_dir() == AISLAND_PROJECT
    assert locations.workspace_found() is True


@needs_workspace
@pytest.mark.real_workspace
def test_from_a_checkout_the_state_stays_in_aislands_own_folder() -> None:
    assert paths.state_dir() == AISLAND_PROJECT
    assert paths.data_dir() == AISLAND_PROJECT / "data"
    assert paths.bin_dir() == AISLAND_PROJECT / "bin"
    assert paths.tools_dir() == AISLAND_PROJECT / "tools"
    assert supervisor.config_file() == AISLAND_PROJECT / "data" / "process-compose.yaml"
    assert pytest_stats.workspace_root() == WORKSPACE
    assert pytest_stats.stats_folder() == AISLAND_PROJECT / "data" / "test-stats"
    assert stats_report.jenkins_jobs_folder() == AISLAND_PROJECT / "data" / "jenkins" / "jobs"


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


def test_from_a_wheel_a_folder_is_an_aisland_workspace_by_its_file_or_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository of its own (a project's parent, a fresh `git init`) is where `init` and `setup` may start, but no
    aisland workspace yet: `workspace.toml` or `.gitmodules` makes one, or the variable naming it."""
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    code = tmp_path / "code"
    (code / "app").mkdir(parents=True)
    (code / "app" / "pyproject.toml").write_text("")
    assert locations.find_workspace(code / "app") == code
    assert not locations.is_workspace(code, {})
    assert locations.is_workspace(code, {"AISLAND_WORKSPACE": str(code)})
    for marker in locations.WORKSPACE_MARKERS:
        (code / marker).write_text("")
        assert locations.is_workspace(code, {})
        (code / marker).unlink()


def test_from_a_checkout_its_workspace_is_always_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "FROM_CHECKOUT", True)
    assert locations.is_workspace(tmp_path, {})


def test_from_a_wheel_outside_any_repository_there_is_no_workspace(tmp_path: Path) -> None:
    """pytest's temporary folders sit in no git repository; an empty variable counts as unset."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert locations.find_workspace(plain) is None
    assert locations.wheel_workspace_root(plain, {"AISLAND_WORKSPACE": ""}) is None


@pytest.mark.real_workspace
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


@pytest.mark.real_workspace
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
@pytest.mark.real_workspace
def test_from_a_wheel_a_subfolder_you_may_not_enter_counts_as_no_project(
    locked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing `aisland.setup.manager` from `/tmp` raised `PermissionError` on `snap-private-tmp/pyproject.toml`."""
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
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    with pytest.raises(click.ClickException) as refused:
        manager.setup_workspace(non_interactive=True)
    assert refused.value.message == (
        f"Virtual environment not found at {tmp_path / '.venv'}. Create it and install aisland into it: "
        "uv venv --seed .venv && .venv/bin/pip install aisland"
    )


@pytest.mark.parametrize(
    "command",
    [
        lambda root: sources.set_project_source("lib", "local", workspace_root=root),
        lambda root: sources.apply_configured(workspace_root=root),
        lambda root: mcp_config.generate_mcp_json(),
        lambda root: mcp_config.generate_gemini_settings(),
    ],
    ids=["source set", "source apply", "mcp claude", "mcp gemini"],
)
def test_every_command_needing_the_venv_says_how_to_make_one_from_a_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: Callable[[Path], object]
) -> None:
    """A wheel install has no `aisland/setup.sh`: the refusal names the commands that make the venv."""
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "pyproject.toml").write_text('[project]\nname = "lib"\nversion = "0.1.0"\n')
    with pytest.raises(click.ClickException) as refused:
        command(tmp_path)
    assert refused.value.message == (
        f"Virtual environment not found at {tmp_path / '.venv'}. Create it and install aisland into it: "
        "uv venv --seed .venv && .venv/bin/pip install aisland"
    )


# --- the git aliases' install -----------------------------------------------------------------------------------


def test_a_failing_alias_install_shows_the_script_s_output_when_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "setup_git.sh"
    script.write_text('echo "fatal: not a git repository"\nexit 3\n')
    monkeypatch.setattr(git_settings, "SETUP_GIT_SCRIPT", script)
    assert git_settings.install_git_aliases(tmp_path, quiet=True) is False
    captured = capsys.readouterr()
    assert captured.out == "fatal: not a git repository\n"
    assert captured.err == f"  - Error: installing the git aliases failed (bash {script} {tmp_path}).\n"
