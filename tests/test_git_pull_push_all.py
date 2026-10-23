"""Tests for ``git_pull_all.sh`` (the ``git pull-all`` alias) and the ``git push-all`` alias.

Every test builds a throwaway workspace — bare "origin" repositories for a root and two
submodules, ``alpha`` and ``beta`` — and clones it as two machines, ``A`` and ``B``. A
publishes; B runs ``git pull-all``. The clones carry ``submodule.recurse true`` like the
real workspace and NO ``pull.rebase`` setting: the script merges explicitly, it never runs
``git pull``. ``GIT_CONFIG_GLOBAL`` points at a sandbox file, so the developer's own git
configuration is neither read nor touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

AISLAND = Path(__file__).resolve().parents[1]
PULL_ALL = AISLAND / "aisland" / "git" / "git_pull_all.sh"
SETUP_GIT = AISLAND / "aisland" / "git" / "setup_git.sh"
# Where the scripts sit in a workspace; they find the root three levels above themselves.
SCRIPTS = Path("aisland", "aisland", "git")
BRANCH = "dev/main"
LINES = ["line1", "line2", "line3", "line4", "line5"]


class Workspace:
    """Origins for a root and two submodules, cloned as machines ``A`` and ``B``.

    Building one takes a few seconds of git calls, so the tests share one built per session
    (:meth:`build`) and each works on a copy (:meth:`copy_of`).
    """

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=str(tmp / "gitconfig"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        if Path("/bin/bash").exists():
            # `bash` (the git aliases' too) is /bin/bash: on macOS that is bash 3.2, the
            # oldest a machine running the script has, even beside a newer Homebrew bash.
            env["PATH"] = os.pathsep.join(["/bin", env.get("PATH", "")])
        self.env: Dict[str, str] = env
        self.A = tmp / "A"
        self.B = tmp / "B"

    @classmethod
    def copy_of(cls, template: "Workspace", tmp: Path) -> "Workspace":
        """A copy of ``template`` whose clones fetch from and push to the copy's own origins."""
        shutil.copytree(template.tmp, tmp, symlinks=True)
        ws = cls(tmp)
        # The clones' remotes and the committed .gitmodules name the template's origins.
        ws.git(tmp, "config", "--global", f"url.{tmp / 'origin'}/.insteadOf", f"{template.tmp / 'origin'}/")
        return ws

    @classmethod
    def build(cls, tmp: Path) -> "Workspace":
        ws = cls(tmp)
        ws._build()
        return ws

    def _build(self) -> None:
        tmp = self.tmp
        for key, value in (("init.defaultBranch", BRANCH), ("protocol.file.allow", "always")):
            self.git(tmp, "config", "--global", key, value)
        origin = tmp / "origin"
        for name in ("root", "alpha", "beta"):
            self.git(tmp, "init", "-q", "--bare", str(origin / f"{name}.git"))
        for name in ("alpha", "beta"):
            seed = tmp / f"seed-{name}"
            self.git(tmp, "clone", "-q", str(origin / f"{name}.git"), str(seed))
            self.commit(seed, {"f.txt": LINES, "g.txt": LINES}, "init")
            self.git(seed, "push", "-q", "origin", BRANCH)
        seed = tmp / "seed-root"
        self.git(tmp, "clone", "-q", str(origin / "root.git"), str(seed))
        for name in ("alpha", "beta"):
            self.git(seed, "submodule", "add", "-q", str(origin / f"{name}.git"), name)
            self.git(seed, "config", "-f", ".gitmodules", f"submodule.{name}.branch", ".")
        self.commit(seed, {"README.md": ["r1", "r2", "r3", "r4", "r5"]}, "init workspace")
        self.git(seed, "push", "-q", "origin", BRANCH)
        self.clone("A")
        self.clone("B")

    # --- plumbing ---------------------------------------------------------------------------

    def git(self, repo: Path, *args: str, check: bool = True, strip: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=repo, env=self.env, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed in {repo}:\n{result.stdout}{result.stderr}")
        return result.stdout.strip() if strip else result.stdout

    def clone(self, name: str) -> Path:
        machine = self.tmp / name
        self.git(self.tmp, "clone", "-q", "--recurse-submodules", str(self.tmp / "origin" / "root.git"), str(machine))
        self.git(machine, "config", "submodule.recurse", "true")
        for sub in ("alpha", "beta"):
            self.git(machine / sub, "checkout", "-q", BRANCH)
        (machine / SCRIPTS).mkdir(parents=True)
        shutil.copy(PULL_ALL, machine / SCRIPTS / "git_pull_all.sh")
        with open(machine / ".git" / "info" / "exclude", "a") as exclude:
            exclude.write("/aisland/\n/.gitattributes\n")
        return machine

    def commit(self, repo: Path, files: Dict[str, List[str]], message: str) -> str:
        for name, lines in files.items():
            (repo / name).write_text("\n".join(lines) + "\n")
        self.git(repo, "add", *files)
        self.git(repo, "commit", "-q", "-m", message)
        return self.head(repo)

    def set_line(self, repo: Path, name: str, number: int, text: str) -> List[str]:
        """Line ``number`` (1-based) of ``repo/name`` replaced by ``text``; written, not committed."""
        lines = (repo / name).read_text().splitlines()
        lines[number - 1] = text
        (repo / name).write_text("\n".join(lines) + "\n")
        return lines

    def change(self, repo: Path, name: str, number: int, text: str) -> str:
        """Commit line ``number`` of ``repo/name`` as ``text``."""
        return self.commit(repo, {name: self.set_line(repo, name, number, text)}, f"{name}:{number}={text}")

    def push(self, repo: Path) -> None:
        self.git(repo, "push", "-q", "origin", BRANCH)

    def record(self, machine: Path, *subs: str) -> str:
        self.git(machine, "add", *subs)
        self.git(machine, "commit", "-q", "-m", f"chore: sync submodule pointers ({', '.join(subs)})")
        return self.head(machine)

    def head(self, repo: Path, rev: str = "HEAD") -> str:
        return self.git(repo, "rev-parse", rev)

    def recorded(self, machine: Path, sub: str, rev: str = "HEAD") -> str:
        return self.git(machine, "rev-parse", f"{rev}:{sub}")

    def contains(self, repo: Path, ancestor: str, rev: str = "HEAD") -> bool:
        return (
            subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, rev], cwd=repo, env=self.env).returncode
            == 0
        )

    def mid_merge(self, repo: Path) -> bool:
        return self.git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) != ""

    def status(self, repo: Path) -> List[str]:
        return [line for line in self.git(repo, "status", "--porcelain", strip=False).splitlines() if line]

    def stashes(self, repo: Path) -> List[str]:
        return self.git(repo, "stash", "list", "--format=%gs").splitlines()

    def pull_all(self, machine: Path, *args: str, cwd: Path | None = None) -> Tuple[int, str]:
        result = subprocess.run(
            ["bash", str(machine / SCRIPTS / "git_pull_all.sh"), *args],
            cwd=cwd or machine,
            env=self.env,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout + result.stderr


@pytest.fixture(scope="session")
def template(tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    return Workspace.build(tmp_path_factory.mktemp("pull-all-template"))


@pytest.fixture()
def ws(template: Workspace, tmp_path: Path) -> Workspace:
    return Workspace.copy_of(template, tmp_path / "workspace")


def _publish(ws: Workspace, sub: str, number: int, text: str, record: bool = True) -> str:
    """Machine A commits line ``number`` of ``<sub>/f.txt``, pushes it and (optionally) its root pointer."""
    sha = ws.change(ws.A / sub, "f.txt", number, text)
    ws.push(ws.A / sub)
    if record:
        ws.record(ws.A, sub)
        ws.push(ws.A)
    return sha


def _diverge_alpha_pointer(ws: Workspace) -> Tuple[str, str]:
    """A and B each commit in alpha (different lines) and record it; only A pushes."""
    a_sha = _publish(ws, "alpha", 1, "A")
    b_sha = ws.change(ws.B / "alpha", "f.txt", 5, "B")
    ws.record(ws.B, "alpha")
    return a_sha, b_sha


# --- pointer conflicts ------------------------------------------------------------------------


def test_a_pointer_conflict_is_settled_to_the_submodule_merge_holding_both_sides(ws: Workspace) -> None:
    a_sha, b_sha = _diverge_alpha_pointer(ws)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert out.startswith("fetching the root and 2 submodules…")
    alpha = ws.head(ws.B / "alpha")
    assert ws.contains(ws.B / "alpha", a_sha) and ws.contains(ws.B / "alpha", b_sha)
    assert ws.recorded(ws.B, "alpha") == alpha
    assert f"pointer conflict alpha → {alpha[:7]} (holds both sides)" in out
    assert not ws.mid_merge(ws.B)
    assert ws.contains(ws.B, ws.head(ws.B, "origin/dev/main"))
    assert ws.status(ws.B) == []
    for sub in ("alpha", "beta"):  # submodule.recurse=true never detached them
        assert ws.git(ws.B / sub, "branch", "--show-current") == BRANCH


def test_a_rerun_finishes_a_root_merge_stopped_on_pointer_conflicts(ws: Workspace) -> None:
    _diverge_alpha_pointer(ws)
    # What the old alias left behind: alpha merged, the root stopped on `CONFLICT (submodule)`.
    ws.git(ws.B / "alpha", "fetch", "-q", "origin")
    ws.git(ws.B / "alpha", "merge", "-q", "--no-edit", "origin/dev/main")
    ws.git(ws.B, "fetch", "-q", "origin")
    ws.git(ws.B, "merge", "--no-edit", "origin/dev/main", check=False)
    assert ws.mid_merge(ws.B)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert "pointer conflict alpha" in out
    assert not ws.mid_merge(ws.B)
    assert ws.recorded(ws.B, "alpha") == ws.head(ws.B / "alpha")
    message = ws.git(ws.B, "log", "-1", "--format=%B")
    assert message.startswith("Merge") and "#" not in message


def test_a_pointer_whose_submodule_does_not_hold_both_sides_is_left_and_listed(ws: Workspace) -> None:
    _diverge_alpha_pointer(ws)
    ws.git(ws.B / "alpha", "checkout", "-q", "-b", "feature")  # alpha will not be pulled
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert ws.mid_merge(ws.B)
    assert "alpha" in ws.git(ws.B, "ls-files", "-u")
    assert "CONFLICT in alpha" in out


# --- recording what came in ------------------------------------------------------------------


def test_a_submodule_commit_nobody_recorded_is_recorded_by_the_pull(ws: Workspace) -> None:
    a_sha = _publish(ws, "alpha", 1, "A", record=False)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert ws.head(ws.B / "alpha") == a_sha
    assert ws.recorded(ws.B, "alpha") == a_sha
    assert ws.git(ws.B, "log", "-1", "--format=%s") == "chore: sync submodule pointers after pull-all (alpha)"
    assert ws.git(ws.B, "show", "--format=", "--name-only", "HEAD") == "alpha"
    assert "recorded submodule pointers: alpha" in out
    assert ws.status(ws.B) == []


def test_recording_pointers_leaves_other_staged_changes_staged_and_out(ws: Workspace) -> None:
    _publish(ws, "alpha", 1, "A", record=False)
    ws.set_line(ws.B, "README.md", 3, "staged")
    ws.git(ws.B, "add", "README.md")
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert ws.git(ws.B, "show", "--format=", "--name-only", "HEAD") == "alpha"
    assert ws.git(ws.B, "diff", "--cached", "--name-only") == "README.md"


def test_unrecorded_local_commits_with_nothing_incoming_are_left_alone(ws: Workspace) -> None:
    ws.change(ws.B / "alpha", "f.txt", 1, "B local")
    before = ws.head(ws.B)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert "everything up to date" in out
    assert ws.head(ws.B) == before
    assert ws.status(ws.B) == [" M alpha"]


def test_a_pointer_is_never_moved_backwards(ws: Workspace) -> None:
    # A records alpha at a commit on another branch, then pushes newer work to alpha's dev/main.
    ws.git(ws.A / "alpha", "checkout", "-q", "-b", "feature")
    feature = ws.change(ws.A / "alpha", "f.txt", 3, "feature")
    ws.git(ws.A / "alpha", "push", "-q", "origin", "feature")
    ws.record(ws.A, "alpha")
    ws.push(ws.A)
    ws.git(ws.A / "alpha", "checkout", "-q", BRANCH)
    _publish(ws, "alpha", 1, "A", record=False)
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert ws.recorded(ws.B, "alpha") == feature
    assert f"the root records {feature[:7]}, which alpha's dev/main does not contain" in out


# --- conflicts that need a person ------------------------------------------------------------


def test_a_code_conflict_in_a_submodule_stops_the_root_until_it_is_resolved(ws: Workspace) -> None:
    ws.change(ws.B / "alpha", "f.txt", 1, "B")
    _publish(ws, "alpha", 1, "A")
    beta = _publish(ws, "beta", 1, "A", record=False)
    root_before = ws.head(ws.B)
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "CONFLICT in f.txt — resolve, commit, then run git pull-all again" in out
    assert "not merged: finish the submodule merges above first" in out
    assert ws.mid_merge(ws.B / "alpha")
    assert ws.head(ws.B) == root_before and not ws.mid_merge(ws.B)
    assert ws.head(ws.B / "beta") == beta  # the other submodules still pull
    rc, out = ws.pull_all(ws.B)  # still unresolved: listed again, nothing else happens
    assert rc == 1 and "CONFLICT in f.txt" in out
    ws.commit(ws.B / "alpha", {"f.txt": ["A+B", *LINES[1:]]}, "merge")
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert ws.contains(ws.B, ws.head(ws.B, "origin/dev/main"))
    assert ws.recorded(ws.B, "beta") == beta


def test_a_conflict_in_a_root_file_is_left_with_the_pointers_already_settled(ws: Workspace) -> None:
    _diverge_alpha_pointer(ws)
    ws.change(ws.A, "README.md", 1, "A")
    ws.push(ws.A)
    ws.change(ws.B, "README.md", 1, "B")
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "CONFLICT in README.md" in out
    assert "pointer conflict alpha" in out
    assert ws.git(ws.B, "diff", "--name-only", "--diff-filter=U") == "README.md"
    ws.set_line(ws.B, "README.md", 1, "A+B")
    ws.git(ws.B, "add", "README.md")
    rc, out = ws.pull_all(ws.B)  # the resolved merge is committed by the rerun
    assert rc == 0, out
    assert not ws.mid_merge(ws.B)


# --- uncommitted edits in the way ------------------------------------------------------------


def test_uncommitted_edits_in_the_way_are_left_untouched_and_listed(ws: Workspace) -> None:
    alpha_before = ws.head(ws.B / "alpha")
    _publish(ws, "alpha", 1, "A", record=False)
    beta = _publish(ws, "beta", 1, "A", record=False)
    ws.set_line(ws.B / "alpha", "f.txt", 5, "B wip")
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "not merged: uncommitted edits to f.txt would be overwritten" in out
    assert "--autostash" in out
    assert ws.head(ws.B / "alpha") == alpha_before
    assert (ws.B / "alpha" / "f.txt").read_text().splitlines()[4] == "B wip"
    assert ws.head(ws.B / "beta") == beta


def test_autostash_sets_the_edits_aside_merges_and_puts_them_back(ws: Workspace) -> None:
    a_sha = _publish(ws, "alpha", 1, "A", record=False)
    ws.set_line(ws.B / "alpha", "f.txt", 5, "B wip")
    rc, out = ws.pull_all(ws.B, "--autostash")
    assert rc == 0, out
    assert "put back your uncommitted edits to f.txt" in out
    assert ws.head(ws.B / "alpha") == a_sha
    lines = (ws.B / "alpha" / "f.txt").read_text().splitlines()
    assert lines[0] == "A" and lines[4] == "B wip"
    assert ws.status(ws.B / "alpha") == [" M f.txt"]
    assert ws.stashes(ws.B / "alpha") == []


def test_autostash_in_the_root_leaves_the_submodules_on_their_branch(ws: Workspace) -> None:
    ws.change(ws.A, "README.md", 1, "A")
    ws.push(ws.A)
    ws.set_line(ws.B, "README.md", 5, "B wip")
    rc, out = ws.pull_all(ws.B)
    assert rc == 1 and "uncommitted edits to README.md" in out
    rc, out = ws.pull_all(ws.B, "--autostash")
    assert rc == 0, out
    lines = (ws.B / "README.md").read_text().splitlines()
    assert lines[0] == "A" and lines[4] == "B wip"
    for sub in ("alpha", "beta"):
        assert ws.git(ws.B / sub, "branch", "--show-current") == BRANCH


def test_edits_that_clash_on_the_way_back_keep_a_copy_and_are_never_applied_twice(ws: Workspace) -> None:
    _publish(ws, "alpha", 1, "A", record=False)
    ws.set_line(ws.B / "alpha", "f.txt", 1, "B wip")
    rc, out = ws.pull_all(ws.B, "--autostash")
    assert rc == 1
    assert "your uncommitted edits to f.txt clash with the pulled changes" in out
    assert "<<<<<<<" in (ws.B / "alpha" / "f.txt").read_text()
    assert ws.stashes(ws.B / "alpha") == ["On dev/main: git pull-all autostash (applied with conflicts)"]
    clashed = (ws.B / "alpha" / "f.txt").read_text()
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "unresolved conflict in f.txt from putting back your edits" in out
    assert (ws.B / "alpha" / "f.txt").read_text() == clashed


def test_edits_set_aside_across_a_merge_conflict_are_put_back_by_the_next_run(ws: Workspace) -> None:
    ws.change(ws.B / "alpha", "f.txt", 1, "B")
    ws.change(ws.A / "alpha", "g.txt", 1, "A")
    _publish(ws, "alpha", 1, "A", record=False)
    ws.set_line(ws.B / "alpha", "g.txt", 3, "B wip")
    rc, out = ws.pull_all(ws.B, "--autostash")
    assert rc == 1
    assert "CONFLICT in f.txt" in out
    assert "uncommitted edits to g.txt are set aside" in out
    assert ws.stashes(ws.B / "alpha") == ["On dev/main: git pull-all autostash"]
    ws.commit(ws.B / "alpha", {"f.txt": ["A+B", *LINES[1:]]}, "merge")
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert "put back your uncommitted edits to g.txt" in out
    lines = (ws.B / "alpha" / "g.txt").read_text().splitlines()
    assert lines[0] == "A" and lines[2] == "B wip"
    assert ws.stashes(ws.B / "alpha") == []


# --- what is not pulled ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "checkout, expected",
    [
        (["-b", "feature"], "on feature, the root is on dev/main — not pulled"),
        (["--detach"], "detached HEAD — not pulled"),
    ],
)
def test_a_submodule_off_the_roots_branch_is_not_pulled(ws: Workspace, checkout: List[str], expected: str) -> None:
    _publish(ws, "alpha", 1, "A", record=False)
    before = ws.head(ws.B / "alpha")
    ws.git(ws.B / "alpha", "checkout", "-q", *checkout)
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert expected in out
    assert ws.head(ws.B / "alpha") == before


# --- a root without a remote ------------------------------------------------------------------
# `aisland workspace init` makes the root with `git init` and adds no remote: nothing to fetch or merge
# there, and its branch is a local name only, so each submodule is pulled on the branch it is on.


def test_a_root_without_origin_pulls_the_submodules_and_records_what_came_in(ws: Workspace) -> None:
    ws.git(ws.B, "remote", "remove", "origin")
    a_sha = _publish(ws, "alpha", 1, "A", record=False)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert out.splitlines() == [
        "fetching 2 submodules (the root has no origin remote)…",
        "alpha                  merged origin/dev/main (1 commit)",
        "root                   recorded submodule pointers: alpha",
    ]
    assert ws.head(ws.B / "alpha") == a_sha
    assert ws.recorded(ws.B, "alpha") == a_sha
    assert ws.status(ws.B) == []


def test_a_root_without_origin_pulls_each_submodule_on_its_own_branch(ws: Workspace) -> None:
    ws.git(ws.B, "remote", "remove", "origin")
    ws.git(ws.B, "-c", "submodule.recurse=false", "checkout", "-q", "-b", "feature")
    ws.git(ws.A / "alpha", "checkout", "-q", "-b", "feature")
    feature = ws.change(ws.A / "alpha", "f.txt", 3, "feature")
    ws.git(ws.A / "alpha", "push", "-q", "origin", "feature")
    beta = _publish(ws, "beta", 1, "A", record=False)
    ws.git(ws.B / "alpha", "checkout", "-q", "-b", "feature")
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert "alpha                  merged origin/feature (1 commit)" in out
    assert "beta                   merged origin/dev/main (1 commit)" in out
    assert ws.head(ws.B / "alpha") == feature and ws.head(ws.B / "beta") == beta
    assert ws.recorded(ws.B, "alpha") == feature and ws.recorded(ws.B, "beta") == beta


def test_a_root_without_origin_leaves_unrecorded_local_commits_alone(ws: Workspace) -> None:
    ws.git(ws.B, "remote", "remove", "origin")
    ws.git(ws.B, "submodule", "deinit", "-q", "beta")
    ws.change(ws.B / "alpha", "f.txt", 1, "B local")
    before = ws.head(ws.B)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert out.splitlines() == ["fetching 1 submodule (the root has no origin remote)…", "everything up to date"]
    assert ws.head(ws.B) == before
    assert ws.status(ws.B) == [" M alpha"]


def test_a_submodule_whose_updates_the_root_ignores_is_pulled_and_never_recorded(ws: Workspace) -> None:
    # `aisland workspace init --local`: `ignore = all` in .gitmodules, so the root never shows nor records a project's
    # new commits; a submodule without it is recorded as before.
    ws.git(ws.B, "remote", "remove", "origin")
    ws.git(ws.B, "config", "-f", ".gitmodules", "submodule.alpha.ignore", "all")
    ws.git(ws.B, "commit", "-q", "-am", "alpha's updates ignored")
    before = ws.head(ws.B)
    alpha = _publish(ws, "alpha", 1, "A", record=False)
    beta = _publish(ws, "beta", 1, "A", record=False)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert out.splitlines() == [
        "fetching 2 submodules (the root has no origin remote)…",
        "alpha                  merged origin/dev/main (1 commit)",
        "beta                   merged origin/dev/main (1 commit)",
        "root                   recorded submodule pointers: beta",
    ]
    assert ws.head(ws.B / "alpha") == alpha and ws.head(ws.B / "beta") == beta
    assert ws.recorded(ws.B, "alpha") == ws.recorded(ws.B, "alpha", before)
    assert ws.recorded(ws.B, "beta") == beta
    assert ws.status(ws.B) == []


def test_this_machines_ignore_setting_counts_too(ws: Workspace) -> None:
    ws.git(ws.B, "remote", "remove", "origin")
    ws.git(ws.B, "config", "submodule.alpha.ignore", "all")
    ws.git(ws.B, "submodule", "deinit", "-q", "beta")
    _publish(ws, "alpha", 1, "A", record=False)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert out.splitlines() == [
        "fetching 1 submodule (the root has no origin remote)…",
        "alpha                  merged origin/dev/main (1 commit)",
    ]


def _untrack(ws: Workspace, sub: str) -> None:
    ws.git(ws.B, "config", "-f", ".gitmodules", f"submodule.{sub}.ignore", "all")
    ws.git(ws.B, "commit", "-q", "-am", f"{sub} untracked")


def test_an_untracked_submodule_on_no_branch_is_put_on_its_branch_and_pulled(ws: Workspace) -> None:
    # `branch = .` (the root's branch): a clone's `git submodule update` leaves it on the commit the root recorded.
    _untrack(ws, "alpha")
    ws.git(ws.B / "alpha", "checkout", "-q", "--detach")
    alpha = _publish(ws, "alpha", 1, "A", record=False)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert out.splitlines() == [
        "fetching the root and 2 submodules…",
        "alpha                  on dev/main (it was on no branch)",
        "alpha                  merged origin/dev/main (1 commit)",
    ]
    assert ws.git(ws.B / "alpha", "branch", "--show-current") == BRANCH
    assert ws.head(ws.B / "alpha") == alpha


def test_an_untracked_submodule_without_a_local_branch_gets_one_tracking_origin(ws: Workspace) -> None:
    _untrack(ws, "alpha")
    ws.git(ws.B / "alpha", "checkout", "-q", "--detach")
    ws.git(ws.B / "alpha", "branch", "-q", "-D", BRANCH)
    alpha = _publish(ws, "alpha", 1, "A", record=False)
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert "alpha                  on dev/main (it was on no branch)" in out
    assert ws.head(ws.B / "alpha") == alpha
    assert ws.git(ws.B / "alpha", "rev-parse", "--abbrev-ref", f"{BRANCH}@{{u}}") == f"origin/{BRANCH}"


def test_an_untracked_submodule_follows_the_branch_gitmodules_names(ws: Workspace) -> None:
    ws.git(ws.A / "alpha", "checkout", "-q", "-b", "release")
    release = ws.change(ws.A / "alpha", "f.txt", 2, "release")
    ws.git(ws.A / "alpha", "push", "-q", "origin", "release")
    ws.git(ws.B, "config", "-f", ".gitmodules", "submodule.alpha.branch", "release")
    _untrack(ws, "alpha")
    ws.git(ws.B / "alpha", "checkout", "-q", "--detach")
    rc, out = ws.pull_all(ws.B)
    assert "alpha                  on release (it was on no branch)" in out
    assert ws.head(ws.B / "alpha") == release
    # On `release`, the root on dev/main: it is not merged, and says so.
    assert rc == 1
    assert "on release, the root is on dev/main — not pulled" in out


def test_an_untracked_submodule_whose_switch_is_refused_is_left_and_listed(ws: Workspace) -> None:
    _untrack(ws, "alpha")
    ws.git(ws.B / "alpha", "checkout", "-q", "--detach", "HEAD")
    _publish(ws, "alpha", 1, "A", record=False)
    ws.git(ws.B / "alpha", "fetch", "-q", "origin")
    ws.git(ws.B / "alpha", "branch", "-q", "-f", BRANCH, f"origin/{BRANCH}")
    ws.set_line(ws.B / "alpha", "f.txt", 1, "mine")
    before = ws.head(ws.B / "alpha")
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "alpha                on no branch, and git switch dev/main refused: " in out
    assert ws.head(ws.B / "alpha") == before
    assert ws.git(ws.B / "alpha", "branch", "--show-current") == ""


def test_an_inactive_submodule_is_left_untouched(ws: Workspace) -> None:
    ws.git(ws.B, "submodule", "deinit", "-q", "beta")
    ws.git(ws.B, "config", "submodule.beta.active", "false")
    _publish(ws, "beta", 1, "A")
    rc, out = ws.pull_all(ws.B)
    assert rc == 0, out
    assert "fetching the root and 1 submodule…" in out
    assert ws.head(ws.B) == ws.head(ws.A)
    assert list((ws.B / "beta").iterdir()) == []


def test_an_unreachable_remote_is_listed_and_the_rest_still_pull(ws: Workspace) -> None:
    a_sha = _publish(ws, "alpha", 1, "A")
    ws.git(ws.B / "beta", "remote", "set-url", "origin", str(ws.tmp / "missing.git"))
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "beta" in out and "not fetched" in out
    assert ws.head(ws.B / "alpha") == a_sha
    assert ws.recorded(ws.B, "alpha") == a_sha


def test_a_root_off_any_branch_pulls_nothing(ws: Workspace) -> None:
    ws.git(ws.B, "-c", "submodule.recurse=false", "checkout", "-q", "--detach")
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "the root is not on a branch" in out


def test_submodules_that_cannot_be_listed_stop_the_run_before_anything_merges(ws: Workspace) -> None:
    _publish(ws, "alpha", 1, "A")
    before = ws.head(ws.B)
    (ws.B / ".gitmodules").write_text("[[[ not a config file\n")
    rc, out = ws.pull_all(ws.B)
    assert rc == 1
    assert "cannot list the submodules" in out and "nothing pulled" in out
    assert ws.head(ws.B) == before


def test_usage(ws: Workspace) -> None:
    rc, out = ws.pull_all(ws.B, "--help")
    assert rc == 0 and "usage: git pull-all [--autostash]" in out
    rc, out = ws.pull_all(ws.B, "--bogus")
    assert rc == 2 and "usage: git pull-all [--autostash]" in out


# --- the git aliases -------------------------------------------------------------------------


def _install_aliases(ws: Workspace, machine: Path) -> None:
    shutil.copy(SETUP_GIT, machine / SCRIPTS / "setup_git.sh")
    subprocess.run(
        ["bash", str(machine / SCRIPTS / "setup_git.sh")], cwd=machine, env=ws.env, capture_output=True, check=True
    )


def test_the_pull_all_alias_pulls_the_whole_workspace_from_inside_a_submodule(ws: Workspace) -> None:
    _install_aliases(ws, ws.B)
    a_sha = _publish(ws, "alpha", 1, "A")
    result = subprocess.run(["git", "pull-all"], cwd=ws.B / "beta", env=ws.env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ws.head(ws.B / "alpha") == a_sha
    assert ws.head(ws.B) == ws.head(ws.A)


def test_aliases_installed_from_outside_the_workspace_pull_it_from_inside_a_submodule(ws: Workspace) -> None:
    """Installed from a wheel, the scripts sit in `site-packages/aisland/git/`, not in the workspace: the root is
    the argument, and the alias names the script by its path and passes the root it finds."""
    site = ws.tmp / "site" / "aisland" / "git"
    site.mkdir(parents=True)
    shutil.copy(SETUP_GIT, site / "setup_git.sh")
    shutil.copy(PULL_ALL, site / "git_pull_all.sh")
    subprocess.run(
        ["bash", str(site / "setup_git.sh"), str(ws.B)], cwd=ws.tmp, env=ws.env, capture_output=True, check=True
    )
    alias = ws.git(ws.B / "beta", "config", "--local", "alias.pull-all")
    assert alias == (
        '!GIT_PULL_ALL_ROOT="$(git rev-parse --show-superproject-working-tree --show-toplevel | head -n 1)" '
        f'bash "{site / "git_pull_all.sh"}"'
    )
    a_sha = _publish(ws, "alpha", 1, "A")
    result = subprocess.run(["git", "pull-all"], cwd=ws.B / "beta", env=ws.env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ws.head(ws.B / "alpha") == a_sha
    assert ws.head(ws.B) == ws.head(ws.A)


def test_aliases_installed_from_the_checkout_keep_their_form(ws: Workspace) -> None:
    _install_aliases(ws, ws.B)
    assert ws.git(ws.B, "config", "--local", "alias.pull-all") == (
        '!bash "$(git rev-parse --show-superproject-working-tree --show-toplevel | head -n 1)'
        '/aisland/aisland/git/git_pull_all.sh"'
    )


def test_the_push_all_alias_pushes_the_submodules_before_the_root(ws: Workspace) -> None:
    _install_aliases(ws, ws.B)
    ws.change(ws.B / "alpha", "f.txt", 1, "B")
    root = ws.record(ws.B, "alpha")
    origin = ws.tmp / "origin"
    ws.git(ws.B / "alpha", "remote", "set-url", "origin", str(ws.tmp / "missing.git"))
    failed = subprocess.run(["git", "push-all"], cwd=ws.B, env=ws.env, capture_output=True, text=True)
    assert failed.returncode != 0
    assert ws.git(origin / "root.git", "rev-parse", BRANCH) != root  # the root waits for its submodules
    ws.git(ws.B / "alpha", "remote", "set-url", "origin", str(origin / "alpha.git"))
    pushed = subprocess.run(["git", "push-all"], cwd=ws.B, env=ws.env, capture_output=True, text=True)
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr
    assert ws.git(origin / "root.git", "rev-parse", BRANCH) == root
    assert ws.git(origin / "alpha.git", "rev-parse", BRANCH) == ws.head(ws.B / "alpha")


def test_the_push_all_alias_pushes_the_submodules_of_a_root_without_origin(ws: Workspace) -> None:
    _install_aliases(ws, ws.B)
    ws.git(ws.B, "remote", "remove", "origin")
    ws.change(ws.B / "alpha", "f.txt", 1, "B")
    ws.record(ws.B, "alpha")
    pushed = subprocess.run(["git", "push-all"], cwd=ws.B, env=ws.env, capture_output=True, text=True)
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr
    assert pushed.stdout.splitlines()[-1] == "root: no origin remote — not pushed"
    assert ws.git(ws.tmp / "origin" / "alpha.git", "rev-parse", BRANCH) == ws.head(ws.B / "alpha")
