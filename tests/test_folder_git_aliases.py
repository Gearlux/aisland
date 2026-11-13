"""`git pull-all` / `git push-all` in a workspace without `.gitmodules`.

There, each subfolder that is its own git repository (a clone) is pulled and pushed as a submodule would be — on
its OWN branch, since independent clones share none — and the root too when it is a repository, last. No pointers
are recorded: nothing records them. `switch-all`, `merge-all`, `tag-all`, `status-all` and `safe` loop over the
clones the same way (`git_folder_all.sh`). The aliases go into every clone's local config, naming the workspace root,
because from inside a clone git cannot find it. A workspace with `.gitmodules` keeps its aliases
(`tests/test_git_pull_push_all.py`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

import pytest

GIT_DIR = Path(__file__).resolve().parents[1] / "aisland" / "git"
SETUP_GIT = GIT_DIR / "setup_git.sh"


class Folder:
    """`work/` (`root_is_repo`: a repository of its own) holding the clones `alpha` (on `main`) and `beta` (on
    `dev`), each of a bare origin under `origin/`; `other/` is a second clone of each, to publish from."""

    def __init__(self, tmp: Path, root_is_repo: bool) -> None:
        self.tmp = tmp
        self.env: Dict[str, str] = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.env.pop("GIT_DIR", None)
        if Path("/bin/bash").exists():
            # `bash` (the git aliases' too) is /bin/bash: on macOS that is bash 3.2, which the scripts are
            # written for (it read `$noun…` as one variable name), as tests/test_git_pull_push_all.py does.
            self.env["PATH"] = os.pathsep.join(["/bin", self.env.get("PATH", "")])
        self.root = tmp / "work"
        self.root.mkdir()
        for name, branch in (("alpha", "main"), ("beta", "dev")):
            seed = tmp / "seed" / name
            seed.mkdir(parents=True)
            self.git(seed, "init", "-q", "-b", branch)
            (seed / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
            (seed / "f.txt").write_text("one\n")
            self.git(seed, "add", ".")
            self.git(seed, "commit", "-q", "-m", "first")
            self.git(tmp, "clone", "-q", "--bare", str(seed), str(tmp / "origin" / f"{name}.git"))
            self.git(tmp, "clone", "-q", str(tmp / "origin" / f"{name}.git"), str(self.root / name))
            self.git(tmp, "clone", "-q", str(tmp / "origin" / f"{name}.git"), str(tmp / "other" / name))
        if root_is_repo:
            seed = tmp / "seed" / "root"
            seed.mkdir()
            self.git(seed, "init", "-q", "-b", "main")
            (seed / "README.md").write_text("root\n")
            self.git(seed, "add", ".")
            self.git(seed, "commit", "-q", "-m", "first")
            self.git(tmp, "clone", "-q", "--bare", str(seed), str(tmp / "origin" / "root.git"))
            self.git(self.root, "init", "-q", "-b", "main")
            self.git(self.root, "remote", "add", "origin", str(tmp / "origin" / "root.git"))
            self.git(self.root, "fetch", "-q", "origin")
            self.git(self.root, "reset", "-q", "--hard", "origin/main")
            self.git(self.root, "branch", "-q", "--set-upstream-to", "origin/main")
            (self.root / ".git" / "info" / "exclude").write_text("/alpha/\n/beta/\n/.gitattributes\n")

    def git(self, repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=repo, env=self.env, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed in {repo}:\n{result.stdout}{result.stderr}")
        return result.stdout.strip()

    def publish(self, name: str, text: str) -> str:
        """A commit pushed to `name`'s origin from the other clone; its sha."""
        other = self.tmp / "other" / name
        (other / "f.txt").write_text(text)
        self.git(other, "commit", "-q", "-am", text.strip())
        self.git(other, "push", "-q", "origin", "HEAD")
        return self.git(other, "rev-parse", "HEAD")

    def install(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SETUP_GIT), str(self.root)], cwd=self.tmp, env=self.env, capture_output=True, text=True
        )

    def run(self, alias: str, cwd: Optional[Path] = None, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", alias, *args], cwd=cwd or self.root / "beta", env=self.env, capture_output=True, text=True
        )


@pytest.fixture
def plain(tmp_path: Path) -> Folder:
    folder = Folder(tmp_path, root_is_repo=False)
    assert folder.install().returncode == 0
    return folder


@pytest.fixture
def repo_root(tmp_path: Path) -> Folder:
    folder = Folder(tmp_path, root_is_repo=True)
    assert folder.install().returncode == 0
    return folder


# --- the aliases ------------------------------------------------------------------------------------------------


def test_every_clone_gets_pull_all_and_push_all_naming_the_workspace(plain: Folder) -> None:
    for name in ("alpha", "beta"):
        repo = plain.root / name
        assert plain.git(repo, "config", "--local", "alias.pull-all") == (
            f'!GIT_PULL_ALL_ROOT="{plain.root}" bash "{GIT_DIR / "git_pull_all.sh"}"'
        )
        assert plain.git(repo, "config", "--local", "alias.push-all") == (
            f'!GIT_PULL_ALL_ROOT="{plain.root}" bash "{GIT_DIR / "git_push_all.sh"}"'
        )


@pytest.mark.parametrize(
    "alias, verb",
    [("switch-all", "switch"), ("merge-all", "merge"), ("tag-all", "tag"), ("status-all", "status"), ("safe", "safe")],
)
def test_the_other_workspace_aliases_loop_over_the_clones(plain: Folder, alias: str, verb: str) -> None:
    """Where submodules fan out with `git submodule foreach`, a folder workspace runs `git_folder_all.sh <verb>`."""
    assert plain.git(plain.root / "alpha", "config", "--local", f"alias.{alias}") == (
        f'!GIT_PULL_ALL_ROOT="{plain.root}" bash "{GIT_DIR / "git_folder_all.sh"}" {verb}'
    )


# --- status-all, switch-all, merge-all, tag-all ---------------------------------------------------------------------


def test_status_all_shows_each_clone_s_branch_and_changes(plain: Folder) -> None:
    (plain.root / "alpha" / "f.txt").write_text("edited\n")
    plain.git(plain.root / "beta", "commit", "-q", "--allow-empty", "-m", "local")
    result = plain.run("status-all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "alpha                  (@main)",
        " M f.txt",
        "beta                   (@dev, 1 ahead)",
    ]


def test_status_all_puts_a_root_repository_first(repo_root: Folder) -> None:
    result = repo_root.run("status-all")
    assert result.stdout.splitlines()[0] == "root                   (@main)"


def test_switch_all_checks_out_the_branch_everywhere_and_lists_where_it_could_not(plain: Folder) -> None:
    plain.git(plain.root / "alpha", "branch", "feature")
    result = plain.run("switch-all", None, "feature")
    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == "alpha                  switched to feature"
    assert result.stdout.splitlines()[1].startswith("beta                   not switched: ")
    assert plain.git(plain.root / "alpha", "branch", "--show-current") == "feature"
    assert plain.git(plain.root / "beta", "branch", "--show-current") == "dev"


def test_merge_all_merges_the_ref_in_every_clone(plain: Folder) -> None:
    for name in ("alpha", "beta"):
        repo = plain.root / name
        plain.git(repo, "checkout", "-q", "-b", "feature")
        (repo / "g.txt").write_text("feature\n")
        plain.git(repo, "add", "g.txt")
        plain.git(repo, "commit", "-q", "-m", "feature")
        plain.git(repo, "checkout", "-q", "-")
    plain.git(plain.root / "beta", "merge", "-q", "feature")
    result = plain.run("merge-all", None, "feature")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "alpha                  merged feature",
        "beta                   up to date with feature",
    ]


def test_tag_all_tags_everywhere_then_pushes_each_tag(plain: Folder) -> None:
    result = plain.run("tag-all", None, "v1", "first release")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "alpha                  tagged v1",
        "beta                   tagged v1",
        "alpha                  pushed tag v1",
        "beta                   pushed tag v1",
    ]
    for name in ("alpha", "beta"):
        assert plain.git(plain.tmp / "origin" / f"{name}.git", "tag", "-n1", "v1").split(None, 1)[1] == "first release"


@pytest.mark.parametrize("alias", ["switch-all", "merge-all", "tag-all"])
def test_an_alias_without_its_argument_says_how_to_call_it(plain: Folder, alias: str) -> None:
    result = plain.run(alias)
    assert result.returncode == 2
    assert result.stderr.startswith(f"usage: git {alias} ")


# --- safe -----------------------------------------------------------------------------------------------------------


def test_safe_runs_the_gates_with_the_workspace_venv_then_commits_and_pushes(plain: Folder) -> None:
    """The gates run in the clone; mypy runs from the workspace root against the clone's folder (root
    AGENTS.md "Mypy MUST Run From Workspace Root") with the workspace's venv, which a clone cannot find."""
    log = plain.tmp / "gates.log"
    bin_dir = plain.root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    for tool in ("isort", "black", "flake8", "mypy", "pytest"):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $(pwd) $*" >> "{log}"\n')
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "activate").write_text(f'export PATH="{bin_dir}:$PATH"\n')
    (plain.root / "alpha" / "f.txt").write_text("safe\n")
    result = plain.run("safe", plain.root / "alpha", "feat: safe")
    assert result.returncode == 0, result.stdout + result.stderr
    alpha = plain.root / "alpha"
    assert log.read_text().splitlines() == [
        f"isort {alpha} .",
        f"black {alpha} .",
        f"flake8 {alpha} .",
        f"mypy {plain.root} alpha",
        f"pytest {alpha} ",
    ]
    assert plain.git(plain.tmp / "origin" / "alpha.git", "log", "-1", "--format=%s", "main") == "feat: safe"


def test_a_root_that_is_no_repository_is_left_alone(tmp_path: Path) -> None:
    folder = Folder(tmp_path, root_is_repo=False)
    result = folder.install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (folder.root / ".git").exists()
    assert not (folder.root / ".gitattributes").exists()
    assert "==> Configuring Git Aliases (no .gitmodules: the clones alpha, beta)..." in result.stdout


def test_a_folder_with_no_repository_at_all_gets_no_aliases(tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / "alpha").mkdir(parents=True)
    result = subprocess.run(["bash", str(SETUP_GIT), str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"==> No git repository in {root}: no git aliases installed.\n"


# --- pull-all -----------------------------------------------------------------------------------------------------


def test_pull_all_merges_each_clone_on_its_own_branch(plain: Folder) -> None:
    a_sha = plain.publish("alpha", "two\n")
    b_sha = plain.publish("beta", "two\n")
    result = plain.run("pull-all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "fetching 2 repositories…",
        "alpha                  merged origin/main (1 commit)",
        "beta                   merged origin/dev (1 commit)",
    ]
    assert plain.git(plain.root / "alpha", "rev-parse", "HEAD") == a_sha
    assert plain.git(plain.root / "beta", "rev-parse", "HEAD") == b_sha


def test_pull_all_with_nothing_new_says_so(plain: Folder) -> None:
    result = plain.run("pull-all", plain.root / "alpha")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["fetching 2 repositories…", "everything up to date"]


def test_pull_all_lists_edits_in_the_way_and_autostash_sets_them_aside(plain: Folder) -> None:
    plain.publish("alpha", "two\n")
    (plain.root / "alpha" / "f.txt").write_text("mine\n")
    stopped = plain.run("pull-all")
    assert stopped.returncode == 1
    assert "  alpha                not merged: uncommitted edits to f.txt would be overwritten" in stopped.stdout
    pulled = plain.run("pull-all", None, "--autostash")
    assert "alpha                  merged origin/main (1 commit)" in pulled.stdout


def test_pull_all_lists_a_detached_clone(plain: Folder) -> None:
    plain.git(plain.root / "beta", "checkout", "-q", "--detach")
    result = plain.run("pull-all", plain.root / "alpha")
    assert result.returncode == 1
    assert "  beta                 detached HEAD — not pulled" in result.stdout


def test_pull_all_pulls_a_root_that_is_a_repository_last(repo_root: Folder) -> None:
    other = repo_root.tmp / "other" / "root"
    repo_root.git(repo_root.tmp, "clone", "-q", str(repo_root.tmp / "origin" / "root.git"), str(other))
    (other / "README.md").write_text("two\n")
    repo_root.git(other, "commit", "-q", "-am", "two")
    repo_root.git(other, "push", "-q", "origin", "HEAD")
    repo_root.publish("alpha", "two\n")
    result = repo_root.run("pull-all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "fetching the root and 2 repositories…",
        "alpha                  merged origin/main (1 commit)",
        "root                   merged origin/main (1 commit)",
    ]


@pytest.mark.slow
def test_pull_all_skips_a_root_repository_without_origin(repo_root: Folder) -> None:
    repo_root.git(repo_root.root, "remote", "remove", "origin")
    repo_root.publish("alpha", "two\n")
    result = repo_root.run("pull-all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "fetching 2 repositories (the root has no origin remote)…",
        "alpha                  merged origin/main (1 commit)",
    ]


def test_pull_all_in_a_root_without_origin_and_no_clone_pulls_nothing(repo_root: Folder) -> None:
    repo_root.git(repo_root.root, "remote", "remove", "origin")
    for name in ("alpha", "beta"):
        shutil.rmtree(repo_root.root / name)
    result = repo_root.run("pull-all", repo_root.root)
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["the root has no origin remote and no subfolder is a clone — nothing pulled"]


# --- push-all -----------------------------------------------------------------------------------------------------


@pytest.mark.slow
def test_push_all_skips_a_root_repository_without_origin(repo_root: Folder) -> None:
    repo_root.git(repo_root.root, "remote", "remove", "origin")
    (repo_root.root / "alpha" / "f.txt").write_text("local\n")
    repo_root.git(repo_root.root / "alpha", "commit", "-q", "-am", "local")
    result = repo_root.run("push-all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "alpha                  pushed main (1 commit)",
        "beta                   dev up to date",
        "root                   no origin remote — not pushed",
    ]


def test_push_all_pushes_each_clone_s_branch(plain: Folder) -> None:
    (plain.root / "alpha" / "f.txt").write_text("local\n")
    plain.git(plain.root / "alpha", "commit", "-q", "-am", "local")
    result = plain.run("push-all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "alpha                  pushed main (1 commit)",
        "beta                   dev up to date",
    ]
    assert plain.git(plain.tmp / "origin" / "alpha.git", "rev-parse", "main") == plain.git(
        plain.root / "alpha", "rev-parse", "HEAD"
    )


def test_push_all_holds_the_root_back_when_a_clone_fails(repo_root: Folder) -> None:
    (repo_root.root / "README.md").write_text("local\n")
    repo_root.git(repo_root.root, "commit", "-q", "-am", "local")
    repo_root.git(repo_root.root / "beta", "remote", "set-url", "origin", str(repo_root.tmp / "missing.git"))
    (repo_root.root / "beta" / "f.txt").write_text("local\n")
    repo_root.git(repo_root.root / "beta", "commit", "-q", "-am", "local")
    result = repo_root.run("push-all")
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines[0] == "alpha                  main up to date"
    assert lines[1].startswith("beta                   not pushed: ")
    assert lines[2] == "root                   not pushed: a repository above did not push"
    assert repo_root.git(repo_root.tmp / "origin" / "root.git", "rev-parse", "main") != repo_root.git(
        repo_root.root, "rev-parse", "HEAD"
    )
