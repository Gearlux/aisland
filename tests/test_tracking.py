"""Whether the root records a project's commits: `aisland workspace track set|unset|status [PROJECT…]`.

An untracked project carries `ignore = all` for git: `git status` and `git diff` in the root never show its new
commits, and `git pull-all` never records them. The committed choice is the project's `track = false` in
`workspace.toml`, written to `.gitmodules` for git; this machine's choice (`--machine-only`) is
`aisland.track.<project>.track` in `.git/config`, written as `submodule.<name>.ignore` there (`all` or `none`), which
git reads over `.gitmodules`. aisland's own key survives a `git submodule deinit`, which drops git's section, and
setup writes git's key back. No project named means every project. `unset` stops recording a project's commits, `set`
records them again, `status` says which and why without changing anything.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import List, Optional

import click
import pytest
from click.testing import CliRunner, Result

from aisland import activation, cli, locations, tracking
from aisland.cli import workspace as cli_workspace
from aisland.workspace import file as workspace_file
from aisland.workspace import local as local_workspace

_AUTHOR = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's git configuration is neither read nor touched; local paths may be submodules."""
    for key in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in _AUTHOR.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "gitconfig").write_text('[protocol "file"]\n\tallow = always\n')


def _git(cwd: Path, *args: str) -> str:
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _get(cwd: Path, *args: str) -> Optional[str]:
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else None


def _remote(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        _git(repo, *args)
    return repo


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A shared root (committed, its own files tracked) holding `models` and `tools` as submodules."""
    root = tmp_path / "acme"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for name in ("models", "tools"):
        _git(root, "submodule", "add", "-q", str(_remote(tmp_path, name)), name)
    (root / "workspace.toml").write_text(
        textwrap.dedent(
            f"""\
            [project."models"]
            url = "{tmp_path / 'remote' / 'models'}"

            [project."tools"]
            url = "{tmp_path / 'remote' / 'tools'}"
            """
        )
    )
    _git(root, "add", "workspace.toml")
    _git(root, "commit", "-q", "-m", "workspace")
    return root


def _move(root: Path, project: str) -> None:
    """A new commit in ``project``, as a pull or one's own work leaves it."""
    (root / project / "new.txt").write_text("new\n")
    _git(root / project, "add", "new.txt")
    _git(root / project, "commit", "-q", "-m", "new")


def _status(root: Path) -> List[str]:
    # Not stripped: the first line's leading space is part of its status.
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout.splitlines()


def _run(ws: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> Result:
    monkeypatch.setattr(locations, "workspace_root", lambda: ws)
    return CliRunner().invoke(cli.main, ["workspace", *args])


# --- track unset and track set, committed ------------------------------------------------------------------------


def test_unset_hides_a_projects_new_commits_and_is_written_for_everyone(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(ws, monkeypatch, "track", "unset", "models")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> untracked: models (in workspace.toml and .gitmodules — commit them)",
        "  - models: untracked (the root records none of its commits)",
    ]
    assert workspace_file.load(ws).projects["models"].others == {"track": False}
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.models.ignore") == "all"
    _git(ws, "commit", "-q", "-am", "track unset models")
    _move(ws, "models")
    _move(ws, "tools")
    assert _status(ws) == [" M tools"]


def test_unset_without_a_project_untracks_every_project(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(ws, monkeypatch, "track", "unset")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == (
        "==> untracked: every project, and every one added later (in workspace.toml and .gitmodules — commit them)"
    )
    for name in ("models", "tools"):
        assert _git(ws, "config", "-f", ".gitmodules", f"submodule.{name}.ignore") == "all"
    # The workspace's choice, which a project added later follows too; no project carries one of its own.
    assert workspace_file.load(ws).settings["track"] is False
    assert all("track" not in project.others for project in workspace_file.load(ws).projects.values())


def test_set_shows_the_commits_again_and_leaves_no_setting(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset", "models")
    _move(ws, "models")
    result = _run(ws, monkeypatch, "track", "set", "models")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> tracked: models (in workspace.toml and .gitmodules — commit them)",
        "  - models: tracked",
    ]
    assert "track" not in workspace_file.load(ws).projects["models"].others
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.models.ignore") is None
    assert " M models" in _status(ws)


@pytest.mark.slow
def test_a_project_tracked_in_an_untracked_workspace_keeps_its_own_choice(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(ws, monkeypatch, "track", "unset")
    _run(ws, monkeypatch, "track", "set", "models")
    assert workspace_file.load(ws).projects["models"].others == {"track": True}
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.models.ignore") is None
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.tools.ignore") == "all"
    _run(ws, monkeypatch, "track", "set")  # the whole workspace again: no choice of anyone's is left
    assert "track" not in workspace_file.load(ws).settings
    assert all("track" not in project.others for project in workspace_file.load(ws).projects.values())


@pytest.mark.slow
def test_doing_it_twice_changes_nothing_more(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset", "models")
    again = _run(ws, monkeypatch, "track", "unset", "models")
    assert again.exit_code == 0, again.output
    assert again.output.splitlines() == ["==> untracked: models (in workspace.toml and .gitmodules — commit them)"]


@pytest.mark.slow
def test_an_ignore_setting_of_the_users_own_is_left_alone(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(ws, "config", "-f", ".gitmodules", "submodule.tools.ignore", "dirty")
    _run(ws, monkeypatch, "track", "set")
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.tools.ignore") == "dirty"


# --- this machine only ----------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_machine_only_untracks_here_and_commits_nothing(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(ws, monkeypatch, "track", "unset", "models", "--machine-only")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "==> untracked on this machine: models (nothing committed)"
    assert _git(ws, "config", "--local", "aisland.track.models.track") == "false"
    assert _git(ws, "config", "--local", "submodule.models.ignore") == "all"
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.models.ignore") is None
    assert _status(ws) == []
    _move(ws, "models")
    assert _status(ws) == []


def test_machine_only_tracks_here_what_everyone_else_untracks(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset", "models")
    _git(ws, "commit", "-q", "-am", "track unset models")
    _run(ws, monkeypatch, "track", "set", "models", "--machine-only")
    assert _git(ws, "config", "--local", "submodule.models.ignore") == "none"
    _move(ws, "models")
    assert _status(ws) == [" M models"]


@pytest.mark.slow
def test_the_committed_choice_takes_over_this_machines_again(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset", "models", "--machine-only")
    _run(ws, monkeypatch, "track", "set", "models")
    assert _get(ws, "config", "--local", "aisland.track.models.track") is None
    assert _get(ws, "config", "--local", "submodule.models.ignore") is None


@pytest.mark.slow
def test_this_machines_choice_survives_a_deinit_and_setup_writes_it_back(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(ws, monkeypatch, "track", "unset", "models", "--machine-only")
    _git(ws, "submodule", "deinit", "-q", "models")
    _git(ws, "submodule", "update", "-q", "--init", "models")
    assert _get(ws, "config", "--local", "submodule.models.ignore") is None
    assert [change.line() for change in tracking.apply(ws)] == [
        "models: untracked on this machine (the root records none of its commits)"
    ]
    assert _git(ws, "config", "--local", "submodule.models.ignore") == "all"


@pytest.mark.slow
def test_a_settled_workspace_needs_no_change(ws: Path) -> None:
    assert tracking.apply(ws) == []


# --- refusals --------------------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_an_unknown_project_is_refused_naming_the_projects(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = (ws / ".gitmodules").read_text()
    result = _run(ws, monkeypatch, "track", "unset", "models", "nothing")
    assert result.exit_code == 1
    assert result.output == "Error: 'nothing' is no submodule of the workspace (the submodules: models, tools)\n"
    assert (ws / ".gitmodules").read_text() == before
    assert "track" not in workspace_file.load(ws).projects["models"].others


def test_without_submodules_unset_is_the_choice_for_the_projects_to_come(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q")
    result = _run(root, monkeypatch, "track", "unset")
    assert result.exit_code == 0, result.output
    assert workspace_file.load(root).settings["track"] is False
    result = _run(root, monkeypatch, "track", "unset", "models")
    assert result.exit_code == 1
    assert result.output == "Error: 'models' is no submodule of the workspace (the submodules: none)\n"


@pytest.mark.slow
def test_machine_only_for_every_project_covers_those_added_later(ws: Path, tmp_path: Path) -> None:
    tracking.set_machine(ws, (), track=False)
    assert _git(ws, "config", "--local", "aisland.track.default") == "false"
    _git(ws, "submodule", "add", "-q", str(_remote(tmp_path, "extra")), "extra")
    workspace_file.refresh(ws)
    assert [change.line() for change in tracking.apply(ws)] == [
        "extra: untracked on this machine (the root records none of its commits)"
    ]


# --- a local workspace commits its .gitmodules ----------------------------------------------------------------------


@pytest.mark.slow
def test_in_a_local_workspace_the_change_is_committed(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_workspace.mark(ws)
    head = _git(ws, "rev-parse", "HEAD")
    result = _run(ws, monkeypatch, "track", "unset", "tools")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> untracked: tools (in workspace.toml and .gitmodules)",
        "  - tools: untracked (the root records none of its commits)",
        "  - committed .gitmodules",
    ]
    assert _git(ws, "rev-parse", "HEAD~1") == head
    assert _git(ws, "show", "--name-only", "--format=", "HEAD").splitlines() == [".gitmodules"]


# --- track status ---------------------------------------------------------------------------------------------------


def _add_project(ws: Path, tmp_path: Path, name: str) -> None:
    """Another submodule, listed in workspace.toml as setup lists one."""
    _git(ws, "submodule", "add", "-q", str(_remote(tmp_path, name)), name)
    workspace_file.refresh(ws)


def _says(ws: Path, monkeypatch: pytest.MonkeyPatch, *projects: str) -> List[str]:
    """The lines `aisland workspace track status [PROJECT]…` prints."""
    result = _run(ws, monkeypatch, "track", "status", *projects)
    assert result.exit_code == 0, result.output
    return result.output.splitlines()


def test_status_of_a_workspace_that_tracks_everything_says_so_once(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _says(ws, monkeypatch) == [
        "==> tracked: all 2 projects (the root records their commits)",
        "  - from the default: no track in workspace.toml",
        "  - this machine: no choice of its own",
        "  - git: no ignore, applied for all 2",
    ]


def test_status_of_a_workspace_that_untracks_everything_says_so_once(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset")
    assert _says(ws, monkeypatch) == [
        "==> untracked: all 2 projects (the root records none of their commits)",
        "  - from [workspace] track = false in workspace.toml",
        "  - this machine: no choice of its own",
        "  - git: ignore = all, applied for all 2",
    ]


def test_status_lists_only_the_projects_that_differ_from_the_workspace_default(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_project(ws, tmp_path, "extra")
    _add_project(ws, tmp_path, "spare")
    _run(ws, monkeypatch, "track", "unset")
    _run(ws, monkeypatch, "track", "set", "models")
    _run(ws, monkeypatch, "track", "unset", "tools", "--machine-only")
    assert _says(ws, monkeypatch) == [
        "==> the workspace default: untracked ([workspace] track = false in workspace.toml)",
        "  - models: tracked (its own track = true in workspace.toml; git: applied)",
        "  - tools: untracked (this machine's aisland.track.tools.track = false; git: applied)",
        "  - 2 more: untracked, as the workspace default says",
    ]


def test_status_lists_a_single_plain_project_instead_of_counting_it(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset", "models")
    assert _says(ws, monkeypatch) == [
        "==> the workspace default: tracked (the default: no track in workspace.toml)",
        "  - models: untracked (its own track = false in workspace.toml; git: applied)",
        "  - tools: tracked (the default: no track in workspace.toml; git: applied)",
    ]


def test_status_of_named_projects_is_one_line_each_in_the_order_named(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(ws, monkeypatch, "track", "unset", "models")
    assert _says(ws, monkeypatch, "models") == [
        "models: untracked (its own track = false in workspace.toml; git: applied)"
    ]
    assert _says(ws, monkeypatch, "tools", "models") == [
        "tools: tracked (the default: no track in workspace.toml; git: applied)",
        "models: untracked (its own track = false in workspace.toml; git: applied)",
    ]


def test_status_changes_nothing(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset", "models", "--machine-only")
    files = (ws / "workspace.toml", ws / ".gitmodules", ws / ".git" / "config")
    before = [file.read_bytes() for file in files]
    _says(ws, monkeypatch)
    _says(ws, monkeypatch, "models")
    assert [file.read_bytes() for file in files] == before


def test_track_alone_changes_nothing_and_names_its_commands(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _move(ws, "models")
    before = [(ws / name).read_bytes() for name in ("workspace.toml", ".gitmodules")]
    result = _run(ws, monkeypatch, "track")
    assert result.exit_code == 2
    for command in ("set", "unset", "status"):
        assert re.search(rf"^\s+{command}\s", result.output, re.M), result.output
    assert [(ws / name).read_bytes() for name in ("workspace.toml", ".gitmodules")] == before
    assert _status(ws) == [" M models"]


@pytest.mark.slow
def test_status_says_when_git_does_not_yet_agree_with_the_choice(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_file.set_settings(ws, ("track",), {"models": {"track": False}})
    assert _says(ws, monkeypatch) == [
        "==> the workspace default: tracked (the default: no track in workspace.toml)",
        "  - models: untracked (its own track = false in workspace.toml; git: NOT applied — run `aisland setup`)",
        "  - tools: tracked (the default: no track in workspace.toml; git: applied)",
    ]
    tracking.apply(ws)  # what `aisland setup` runs
    assert _says(ws, monkeypatch, "models") == [
        "models: untracked (its own track = false in workspace.toml; git: applied)"
    ]
    _git(ws, "config", "-f", ".gitmodules", "submodule.tools.ignore", "all")  # hidden by hand against the choice
    assert _says(ws, monkeypatch, "tools") == [
        "tools: tracked (the default: no track in workspace.toml; git: NOT applied — run `aisland setup`)"
    ]


@pytest.mark.slow
def test_status_names_this_machines_choice_for_every_project(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracking.set_machine(ws, (), track=False)
    assert _says(ws, monkeypatch) == [
        "==> the workspace default: tracked (the default: no track in workspace.toml)",
        "  - models: untracked (this machine's aisland.track.default = false; git: applied)",
        "  - tools: untracked (this machine's aisland.track.default = false; git: applied)",
    ]


@pytest.mark.slow
def test_status_leaves_an_ignore_setting_of_the_users_own_alone(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(ws, "config", "-f", ".gitmodules", "submodule.tools.ignore", "dirty")
    assert _says(ws, monkeypatch) == [
        "==> the workspace default: tracked (the default: no track in workspace.toml)",
        "  - models: tracked (the default: no track in workspace.toml; git: applied)",
        "  - tools: tracked (the default: no track in workspace.toml; git: ignore = dirty, yours, left alone)",
    ]


@pytest.mark.slow
def test_status_as_json_gives_every_project_one_row(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "track", "unset", "models")
    result = _run(ws, monkeypatch, "track", "status", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "default_tracked": True,
        "projects": [
            {
                "project": "models",
                "tracked": False,
                "source": "project",
                "reason": "its own track = false in workspace.toml",
                "git": "applied",
                "ignore": "all",
            },
            {
                "project": "tools",
                "tracked": True,
                "source": "default",
                "reason": "the default: no track in workspace.toml",
                "git": "applied",
                "ignore": None,
            },
        ],
    }


@pytest.mark.slow
def test_status_of_an_unknown_project_is_refused_naming_the_projects(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(ws, monkeypatch, "track", "status", "models", "nothing")
    assert result.exit_code == 1
    assert result.output == "Error: 'nothing' is no submodule of the workspace (the submodules: models, tools)\n"


@pytest.mark.slow
def test_status_without_submodules_gives_the_choice_for_the_projects_to_come(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q")
    assert _says(root, monkeypatch) == [
        "==> the workspace default: tracked (the default: no track in workspace.toml)",
        "  - no submodules yet",
    ]
    _run(root, monkeypatch, "track", "unset")
    assert _says(root, monkeypatch)[0] == (
        "==> the workspace default: untracked ([workspace] track = false in workspace.toml)"
    )


@pytest.mark.slow
def test_every_track_command_completes_the_submodules(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: ws)
    track = cli_workspace.workspace_group.commands["track"]
    assert isinstance(track, click.Group)
    for name in ("set", "unset", "status"):
        command = track.commands[name]
        projects = next(param for param in command.params if param.name == "projects")
        with click.Context(command) as ctx:
            assert [item.value for item in projects.shell_complete(ctx, "mo")] == ["models"]


# --- an untracked project follows its branch ------------------------------------------------------------------------


def _publish(tmp_path: Path, name: str) -> str:
    """A new commit on the remote's `main`, as another machine pushes it."""
    remote = tmp_path / "remote" / name
    (remote / "pushed.txt").write_text("pushed\n")
    _git(remote, "add", "pushed.txt")
    _git(remote, "commit", "-q", "-m", "pushed")
    return _git(remote, "rev-parse", "HEAD")


def _detach(ws: Path, project: str) -> str:
    """``project`` on the commit the root records and on no branch, as a clone's `git submodule update` leaves it."""
    _git(ws / project, "checkout", "-q", "--detach")
    return _git(ws / project, "rev-parse", "HEAD")


def test_unset_puts_a_project_on_no_branch_on_its_branch_at_origins_newest(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _detach(ws, "models")
    newest = _publish(tmp_path, "models")
    result = _run(ws, monkeypatch, "track", "unset", "models")
    assert result.exit_code == 0, result.output
    assert "  - models: on main (it was on no branch)" in result.output.splitlines()
    assert _git(ws / "models", "branch", "--show-current") == "main"
    assert _git(ws / "models", "rev-parse", "HEAD") == newest


@pytest.mark.slow
def test_a_project_without_a_local_branch_gets_one_tracking_origin(ws: Path, tmp_path: Path) -> None:
    tracking.set_committed(ws, ("models",), track=False)
    _detach(ws, "models")
    _git(ws / "models", "branch", "-q", "-D", "main")
    newest = _publish(tmp_path, "models")
    assert tracking.follow_branches(ws) == ["models: on main (it was on no branch)"]
    assert _git(ws / "models", "rev-parse", "HEAD") == newest
    assert _git(ws / "models", "rev-parse", "--abbrev-ref", "main@{u}") == "origin/main"


@pytest.mark.slow
def test_the_branch_gitmodules_names_is_followed_and_dot_is_the_roots(ws: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote" / "tools"
    _git(remote, "branch", "release")
    tracking.set_committed(ws, (), track=False)
    _git(ws, "config", "-f", ".gitmodules", "submodule.tools.branch", "release")
    _git(ws, "config", "-f", ".gitmodules", "submodule.models.branch", ".")
    _detach(ws, "models")
    _detach(ws, "tools")
    assert tracking.follow_branches(ws) == [
        "models: on main (it was on no branch)",
        "tools: on release (it was on no branch)",
    ]


@pytest.mark.slow
def test_a_tracked_project_on_no_branch_is_left_on_its_commit(ws: Path, tmp_path: Path) -> None:
    before = _detach(ws, "models")
    _publish(tmp_path, "models")
    assert tracking.follow_branches(ws) == []
    assert _git(ws / "models", "rev-parse", "HEAD") == before


@pytest.mark.slow
def test_a_switch_git_refuses_is_said_and_the_project_left(ws: Path, tmp_path: Path) -> None:
    tracking.set_committed(ws, ("models",), track=False)
    before = _detach(ws, "models")
    remote = tmp_path / "remote" / "models"
    (remote / "pyproject.toml").write_text('[project]\nname = "models"\nversion = "0.2.0"\n')
    _git(remote, "commit", "-q", "-am", "0.2.0")
    _git(ws / "models", "fetch", "-q", "origin")
    _git(ws / "models", "branch", "-q", "-f", "main", "origin/main")
    (ws / "models" / "pyproject.toml").write_text("mine\n")
    lines = tracking.follow_branches(ws)
    assert len(lines) == 1 and lines[0].startswith("models: stays on no branch — git switch main refused: ")
    assert _git(ws / "models", "rev-parse", "HEAD") == before


@pytest.mark.slow
def test_an_unreachable_origin_still_switches_to_the_branch_it_knows(ws: Path, tmp_path: Path) -> None:
    tracking.set_committed(ws, ("models",), track=False)
    _detach(ws, "models")
    _git(ws / "models", "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    assert tracking.follow_branches(ws) == ["models: on main (it was on no branch)"]


def test_setup_puts_untracked_projects_on_their_branch(ws: Path, tmp_path: Path) -> None:
    tracking.set_committed(ws, ("models",), track=False)
    _detach(ws, "models")
    newest = _publish(tmp_path, "models")
    activation.reconcile(ws)
    assert _git(ws / "models", "rev-parse", "HEAD") == newest


@pytest.mark.slow
def test_a_local_branch_with_commits_of_its_own_is_switched_to_and_not_merged(ws: Path, tmp_path: Path) -> None:
    tracking.set_committed(ws, ("models",), track=False)
    (ws / "models" / "mine.txt").write_text("mine\n")
    _git(ws / "models", "add", "mine.txt")
    _git(ws / "models", "commit", "-q", "-m", "mine")
    mine = _git(ws / "models", "rev-parse", "HEAD")
    _detach(ws, "models")
    _git(ws / "models", "checkout", "-q", "HEAD~1")
    _publish(tmp_path, "models")
    assert tracking.follow_branches(ws) == ["models: on main (it was on no branch)"]
    assert _git(ws / "models", "rev-parse", "HEAD") == mine
