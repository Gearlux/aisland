"""`aisland setup` adds a clone the workspace does not list as a submodule, after asking.

A clone is a direct subfolder that is its own repository (a `.git` folder; a submodule's is a file), with an `origin`
and a `pyproject.toml`, which `.gitmodules` does not list and which is no link to another folder. Setup asks about each
(`[Y/n]`; `-y` adds without asking) and adds it with `git submodule add -b <the branch it is on> <its origin>
<folder>` — git keeps the clone as it is, `-f` only in a local root. The new project is tracked like the others: untracked
when every other project is. A workspace without `.gitmodules` (a folder of clones, unless it is a local root) has no
clone to add — each clone already is a project.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

from aisland import clones, local_workspace, tracking, workspace_file

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


def _remote(tmp_path: Path, name: str, pyproject: bool = True) -> Path:
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    if pyproject:
        (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    else:
        (repo / "data.txt").write_text("data\n")
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        _git(repo, *args)
    _git(repo, "branch", "dev/main")
    return repo


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A shared root holding `models` as a submodule, and `foo` cloned beside it on `dev/main`."""
    root = tmp_path / "acme"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "submodule", "add", "-q", str(_remote(tmp_path, "models")), "models")
    _git(root, "commit", "-q", "-m", "workspace")
    workspace_file.refresh(root)
    _git(root, "clone", "-q", "-b", "dev/main", str(_remote(tmp_path, "foo")), "foo")
    return root


def _status(root: Path) -> List[str]:
    run = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    return run.stdout.splitlines()


def test_a_clone_is_found_with_its_origin_and_branch(ws: Path, tmp_path: Path) -> None:
    assert clones.unlisted(ws) == [clones.Clone("foo", str(tmp_path / "remote" / "foo"), "dev/main")]


def test_setup_y_adds_the_clone_as_it_is_and_lists_it(
    ws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _git(ws / "foo", "rev-parse", "HEAD")
    (ws / "foo" / "work.txt").write_text("uncommitted\n")
    lines = clones.add_unlisted(ws, non_interactive=True)
    assert lines == [f"added foo as a submodule ({tmp_path / 'remote' / 'foo'}, dev/main)"]
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.foo.branch") == "dev/main"
    assert (ws / "foo" / ".git").is_dir()  # the clone kept as it is: its own .git folder, its HEAD, its edits
    assert _git(ws / "foo", "rev-parse", "HEAD") == head
    assert (ws / "foo" / "work.txt").read_text() == "uncommitted\n"
    assert "foo" in workspace_file.load(ws).projects
    assert capsys.readouterr().out == ""  # -y asks nothing


def test_setup_asks_and_a_no_leaves_the_clone_out(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: List[str] = []

    def answer(text: str, default: bool) -> bool:
        asked.append(text)
        assert default is True
        return False

    monkeypatch.setattr(clones.click, "confirm", answer)
    assert clones.add_unlisted(ws, non_interactive=False) == []
    assert asked == [
        f"  - foo is a clone the workspace does not list ({ws.parent / 'remote' / 'foo'}, on dev/main). "
        "Add it as a submodule?"
    ]
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.foo.path") is None


def test_a_yes_adds_it(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clones.click, "confirm", lambda text, default: True)
    assert len(clones.add_unlisted(ws, non_interactive=False)) == 1
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.foo.path") == "foo"


def test_a_clone_on_no_branch_is_added_without_one(ws: Path, tmp_path: Path) -> None:
    _git(ws / "foo", "checkout", "-q", "--detach")
    assert clones.add_unlisted(ws, non_interactive=True) == [
        f"added foo as a submodule ({tmp_path / 'remote' / 'foo'}, on no branch)"
    ]
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.foo.branch") is None


def test_in_a_workspace_whose_projects_are_all_untracked_it_is_untracked_too(ws: Path) -> None:
    tracking.set_committed(ws, (), track=False)
    lines = clones.add_unlisted(ws, non_interactive=True)
    assert lines[1:] == ["foo: untracked (the root records none of its commits)"]
    assert workspace_file.load(ws).projects["foo"].others == {"track": False}


def test_untracked_on_this_machine_only_makes_it_so_on_this_machine_only(ws: Path) -> None:
    tracking.set_machine(ws, (), track=False)
    lines = clones.add_unlisted(ws, non_interactive=True)
    assert lines[1:] == ["foo: untracked on this machine (the root records none of its commits)"]
    assert "track" not in workspace_file.load(ws).projects["foo"].others


def test_beside_a_tracked_project_it_stays_tracked(ws: Path, tmp_path: Path) -> None:
    _git(ws, "submodule", "add", "-q", str(_remote(tmp_path, "tools")), "tools")
    workspace_file.refresh(ws)
    tracking.set_committed(ws, ("models",), track=False)
    assert clones.add_unlisted(ws, non_interactive=True)[1:] == []
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.foo.ignore") is None


def test_a_local_root_adds_it_with_f_and_commits_it(ws: Path) -> None:
    (ws / ".gitignore").write_text("/*\n")
    local_workspace.mark(ws)
    local_workspace.settle(ws)
    assert len(clones.add_unlisted(ws, non_interactive=True)) == 1
    assert local_workspace.settle(ws).lines() == ["committed .gitmodules, foo"]
    assert _status(ws) == []


@pytest.mark.parametrize("kind", ["no pyproject.toml", "no origin", "a link", "a hidden folder", "a submodule"])
def test_a_folder_that_is_no_clone_to_add_is_left_alone(ws: Path, tmp_path: Path, kind: str) -> None:
    if kind == "no pyproject.toml":
        _git(ws, "clone", "-q", str(_remote(tmp_path, "data", pyproject=False)), "data")
    elif kind == "no origin":
        (ws / "scratch").mkdir()
        (ws / "scratch" / "pyproject.toml").write_text('[project]\nname = "scratch"\n')
        _git(ws / "scratch", "init", "-q")
    elif kind == "a link":
        (ws / "short").symlink_to(ws / "models")
    elif kind == "a hidden folder":
        _git(ws, "clone", "-q", str(tmp_path / "remote" / "models"), ".hidden")
    assert [clone.path for clone in clones.unlisted(ws)] == ["foo"]


def test_a_folder_of_clones_has_none_to_add(tmp_path: Path) -> None:
    root = tmp_path / "folder"
    root.mkdir()
    _git(root, "clone", "-q", str(_remote(tmp_path, "foo")), "foo")
    assert clones.unlisted(root) == []
    _git(root, "init", "-q")  # a root that is a repository, without .gitmodules: still a folder of clones
    assert clones.unlisted(root) == []


def test_a_clone_git_refuses_to_add_is_said_and_setup_goes_on(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `models` is already in the index: git refuses, and the next clone is still added.
    taken = clones.Clone("models", str(tmp_path / "remote" / "models"), "main")
    found = [taken, *clones.unlisted(ws)]
    monkeypatch.setattr(clones, "unlisted", lambda root: found)
    lines = clones.add_unlisted(ws, non_interactive=True)
    assert lines[0].startswith("could not add models: ")
    assert lines[1].startswith("added foo as a submodule")
