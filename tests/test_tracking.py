"""Whether the root records a project's commits: `aisland workspace track|untrack [PROJECT…] [--machine-only]`.

An untracked project carries `ignore = all` for git: `git status` and `git diff` in the root never show its new
commits, and `git pull-all` never records them. The committed choice is the project's `track = false` in
`workspace.toml`, written to `.gitmodules` for git; this machine's choice (`--machine-only`) is
`aisland.track.<project>.track` in `.git/config`, written as `submodule.<name>.ignore` there (`all` or `none`), which
git reads over `.gitmodules`. aisland's own key survives a `git submodule deinit`, which drops git's section, and
setup writes git's key back. No project named means every project.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest
from click.testing import CliRunner, Result

from aisland import cli, local_workspace, tracking, workspace_file

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
    monkeypatch.setattr(cli, "get_workspace_root", lambda: ws)
    return CliRunner().invoke(cli.main, ["workspace", *args])


# --- untrack and track, committed --------------------------------------------------------------------------------


def test_untrack_hides_a_projects_new_commits_and_is_written_for_everyone(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(ws, monkeypatch, "untrack", "models")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> untracked: models (in workspace.toml and .gitmodules — commit them)",
        "  - models: untracked (the root records none of its commits)",
    ]
    assert workspace_file.load(ws).projects["models"].others == {"track": False}
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.models.ignore") == "all"
    _git(ws, "commit", "-q", "-am", "untrack models")
    _move(ws, "models")
    _move(ws, "tools")
    assert _status(ws) == [" M tools"]


def test_untrack_without_a_project_untracks_every_project(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(ws, monkeypatch, "untrack")
    assert result.exit_code == 0, result.output
    assert (
        result.output.splitlines()[0]
        == "==> untracked: models, tools (in workspace.toml and .gitmodules — commit them)"
    )
    for name in ("models", "tools"):
        assert _git(ws, "config", "-f", ".gitmodules", f"submodule.{name}.ignore") == "all"


def test_track_shows_the_commits_again_and_leaves_no_setting(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "untrack")
    _move(ws, "models")
    result = _run(ws, monkeypatch, "track", "models")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> tracked: models (in workspace.toml and .gitmodules — commit them)",
        "  - models: tracked",
    ]
    assert "track" not in workspace_file.load(ws).projects["models"].others
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.models.ignore") is None
    assert " M models" in _status(ws)


def test_doing_it_twice_changes_nothing_more(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "untrack", "models")
    again = _run(ws, monkeypatch, "untrack", "models")
    assert again.exit_code == 0, again.output
    assert again.output.splitlines() == ["==> untracked: models (in workspace.toml and .gitmodules — commit them)"]


def test_an_ignore_setting_of_the_users_own_is_left_alone(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(ws, "config", "-f", ".gitmodules", "submodule.tools.ignore", "dirty")
    _run(ws, monkeypatch, "track")
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.tools.ignore") == "dirty"


# --- this machine only ----------------------------------------------------------------------------------------------


def test_machine_only_untracks_here_and_commits_nothing(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(ws, monkeypatch, "untrack", "models", "--machine-only")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "==> untracked on this machine: models (nothing committed)"
    assert _git(ws, "config", "--local", "aisland.track.models.track") == "false"
    assert _git(ws, "config", "--local", "submodule.models.ignore") == "all"
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.models.ignore") is None
    assert _status(ws) == []
    _move(ws, "models")
    assert _status(ws) == []


def test_machine_only_tracks_here_what_everyone_else_untracks(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "untrack", "models")
    _git(ws, "commit", "-q", "-am", "untrack models")
    _run(ws, monkeypatch, "track", "models", "--machine-only")
    assert _git(ws, "config", "--local", "submodule.models.ignore") == "none"
    _move(ws, "models")
    assert _status(ws) == [" M models"]


def test_the_committed_choice_takes_over_this_machines_again(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(ws, monkeypatch, "untrack", "models", "--machine-only")
    _run(ws, monkeypatch, "track", "models")
    assert _get(ws, "config", "--local", "aisland.track.models.track") is None
    assert _get(ws, "config", "--local", "submodule.models.ignore") is None


def test_this_machines_choice_survives_a_deinit_and_setup_writes_it_back(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(ws, monkeypatch, "untrack", "models", "--machine-only")
    _git(ws, "submodule", "deinit", "-q", "models")
    _git(ws, "submodule", "update", "-q", "--init", "models")
    assert _get(ws, "config", "--local", "submodule.models.ignore") is None
    assert [change.line() for change in tracking.apply(ws)] == [
        "models: untracked on this machine (the root records none of its commits)"
    ]
    assert _git(ws, "config", "--local", "submodule.models.ignore") == "all"


def test_a_settled_workspace_needs_no_change(ws: Path) -> None:
    assert tracking.apply(ws) == []


# --- refusals --------------------------------------------------------------------------------------------------------


def test_an_unknown_project_is_refused_naming_the_projects(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = (ws / ".gitmodules").read_text()
    result = _run(ws, monkeypatch, "untrack", "models", "nothing")
    assert result.exit_code == 1
    assert result.output == "Error: 'nothing' is no submodule of the workspace (the submodules: models, tools)\n"
    assert (ws / ".gitmodules").read_text() == before
    assert "track" not in workspace_file.load(ws).projects["models"].others


def test_a_workspace_without_submodules_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q")
    result = _run(root, monkeypatch, "untrack")
    assert result.exit_code == 1
    assert result.output == (f"Error: {root} has no submodules: only a submodule's commits are recorded by the root\n")


# --- a local workspace commits its .gitmodules ----------------------------------------------------------------------


def test_in_a_local_workspace_the_change_is_committed(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_workspace.mark(ws)
    head = _git(ws, "rev-parse", "HEAD")
    result = _run(ws, monkeypatch, "untrack", "tools")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "==> untracked: tools (in workspace.toml and .gitmodules)",
        "  - tools: untracked (the root records none of its commits)",
        "  - committed .gitmodules",
    ]
    assert _git(ws, "rev-parse", "HEAD~1") == head
    assert _git(ws, "show", "--name-only", "--format=", "HEAD").splitlines() == [".gitmodules"]
