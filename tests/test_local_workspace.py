"""A local workspace (`aisland workspace init --local`): a root repository that tracks only the projects.

Its `.gitignore` ignores everything (`/*`), so only `.gitmodules` and the submodules are tracked. The workspace is
marked in this machine's `.git/config` (`aisland.workspace.local`). `local_workspace.settle` keeps it so after
`.gitmodules` changes or a project is added — by hand with `git submodule add -f`, by the `active` plugin, by
`aisland workspace track unset`: `.gitmodules` and the new projects are committed, nothing else. Whether the root records a
project's commits is `aisland.tracking`'s (`tests/test_tracking.py`).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aisland import activation
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


def _remote(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        _git(repo, *args)
    return repo


@pytest.fixture
def local(tmp_path: Path) -> Path:
    """A local workspace holding `models`, as `init --local --add <models>` leaves it."""
    root = tmp_path / "acme"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "submodule", "add", "-q", str(_remote(tmp_path, "models")), "models")
    (root / ".gitignore").write_text("/*\n")
    local_workspace.mark(root)
    assert local_workspace.settle(root).lines() == ["committed .gitmodules, models"]
    return root


def _status(root: Path) -> list:
    # Not stripped: the first line's leading space is part of its status.
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout.splitlines()


def test_a_marked_workspace_is_local_and_an_unmarked_one_is_not(local: Path, tmp_path: Path) -> None:
    assert local_workspace.is_local(local)
    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    assert not local_workspace.is_local(plain)
    assert not local_workspace.is_local(tmp_path / "no-repository")


@pytest.mark.slow
def test_a_project_added_by_hand_is_committed(local: Path, tmp_path: Path) -> None:
    _git(local, "submodule", "add", "-q", "-f", str(_remote(tmp_path, "tools")), "tools")
    assert local_workspace.settle(local).lines() == ["committed .gitmodules, tools"]
    assert _status(local) == []
    assert _git(local, "ls-files").splitlines() == [".gitmodules", "models", "tools"]


def test_a_settled_workspace_is_left_as_it_is(local: Path) -> None:
    head = _git(local, "rev-parse", "HEAD")
    assert local_workspace.settle(local).lines() == []
    assert _git(local, "rev-parse", "HEAD") == head


def test_settling_commits_only_gitmodules_and_the_projects(local: Path, tmp_path: Path) -> None:
    (local / "notes.txt").write_text("mine\n")
    _git(local, "add", "-f", "notes.txt")
    _git(local, "submodule", "add", "-q", "-f", str(_remote(tmp_path, "tools")), "tools")
    local_workspace.settle(local)
    assert _git(local, "show", "--name-only", "--format=", "--ignore-submodules=none", "HEAD").splitlines() == [
        ".gitmodules",
        "tools",
    ]
    assert _status(local) == ["A  notes.txt"]


def test_a_workspace_that_is_not_local_is_never_touched(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "submodule", "add", "-q", str(_remote(tmp_path, "models")), "models")
    assert local_workspace.settle(root).lines() == []
    assert _status(root) == ["A  .gitmodules", "A  models"]


def test_a_commit_git_refuses_is_said(local: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(local, "submodule", "add", "-q", "-f", str(_remote(tmp_path, "tools")), "tools")
    for key in _AUTHOR:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    lines = local_workspace.settle(local).lines()
    assert len(lines) == 1 and lines[0].startswith("not committed (.gitmodules, tools): ")


def test_the_active_plugin_adds_a_project_to_a_local_workspace_and_settles_it(local: Path, tmp_path: Path) -> None:
    (local / "workspace.toml").write_text(
        f'[project."models"]\nurl = "{tmp_path / "remote" / "models"}"\n\n'
        f'[project."extra"]\nurl = "{_remote(tmp_path, "extra")}"\nactive = true\n'
    )
    assert activation.reconcile(local) == []
    assert (local / "extra" / "pyproject.toml").is_file()
    assert _status(local) == []
