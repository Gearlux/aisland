"""`aisland wheels prune` and `aisland wheels reset` over real git repositories and the real git-filter-repo.

Each test builds a bare origin, a workspace whose project is a clone of it with a history of committed wheels — a
first version no ref holds, a second a tag holds, a third at the branch's tip — and, where a test needs it, a
second clone standing for another machine. Every git call costs tens of milliseconds, so the fast run keeps the
dry run, one apply, one refusal and the command line; the rest is the slow run's.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import git_filter_repo
import pytest
from click.testing import CliRunner

from aisland import locations
from aisland.cli import main
from aisland.wheels import filter_repo
from aisland.wheels import prune as wp

WHEEL = "wheels/models-0.1.0-py3-none-any.whl"


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration (a signing key, a hook, a default branch) reaches these repositories."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def _ok(path: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=path, capture_output=True).returncode == 0


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_wheel(repo: Path, message: str) -> str:
    """Commit a new version of the wheel; its blob id."""
    (repo / WHEEL).parent.mkdir(exist_ok=True)
    (repo / WHEEL).write_bytes(os.urandom(4096))
    _commit(repo, message)
    return _git(repo, "rev-parse", f"HEAD:{WHEEL}")


def _workspace(root: Path) -> Path:
    """A workspace root repository whose `.gitmodules` lists the project `models`."""
    root.mkdir(parents=True)
    (root / "TASKS.md").write_text("")
    (root / ".gitmodules").write_text('[submodule "models"]\n\tpath = models\n\turl = ../origin/models.git\n')
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "the workspace")
    return root


def _refs(repo: Path) -> Dict[str, str]:
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split() for line in out.splitlines())


def _gone(repo: Path, blob: str) -> bool:
    return not _ok(repo, "cat-file", "-e", blob)


def _gc(bare: Path) -> None:
    _git(bare, "reflog", "expire", "--expire=now", "--all")
    _git(bare, "gc", "-q", "--prune=now")


@dataclass
class Setup:
    root: Path
    origin: Path
    repo: Path
    v1: str  # held by nothing: it goes
    v2: str  # held by the tag `v-two`
    v3: str  # at main's tip


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    origin = tmp_path / "origin" / "models.git"
    origin.parent.mkdir()
    _git(origin.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    root = _workspace(tmp_path / "workspace")
    repo = root / "models"
    _git(root, "clone", "-q", str(origin), "models")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "models"\nversion = "0.1.0"\n')
    _commit(repo, "the project")
    v1 = _new_wheel(repo, "wheels 1")
    (repo / "code.py").write_text("one\n")
    _commit(repo, "code 1")
    v2 = _new_wheel(repo, "wheels 2")
    _git(repo, "tag", "-a", "v-two", "-m", "the second")
    (repo / "code.py").write_text("two\n")
    _commit(repo, "code 2")
    v3 = _new_wheel(repo, "wheels 3")
    _git(repo, "push", "-q", "-u", "origin", "main", "--tags")
    return Setup(root, origin, repo, v1, v2, v3)


def _second_clone(tmp_path: Path, setup: Setup, name: str = "other") -> Path:
    """Another machine's workspace holding its own clone of origin."""
    root = _workspace(tmp_path / name)
    _git(root, "clone", "-q", str(setup.origin), "models")
    return root


# --- prune: what it finds --------------------------------------------------------------------------


@pytest.mark.slow
def test_without_apply_it_names_what_goes_and_changes_nothing(setup: Setup) -> None:
    before = _refs(setup.repo)
    report = wp.prune(setup.root, "models")
    assert report.stripped == [setup.v1]
    assert (report.versions, report.kept, report.applied) == (3, 2, False)
    assert report.holders == ["main", "origin/main", "v-two"]
    assert _refs(setup.repo) == before
    assert not _gone(setup.repo, setup.v1)
    text = wp.render_prune(report)
    assert text.splitlines()[0].startswith("models: 1 of 3 wheel versions are held by no branch or tag (4 kB)")
    assert "aisland wheels prune models --apply   rewrites history and force-pushes origin" in text


@pytest.mark.slow
def test_a_wheel_only_this_clones_branch_holds_is_kept(setup: Setup) -> None:
    _git(setup.repo, "branch", "keep-one", "HEAD~4")  # "wheels 1": a local branch now holds v1
    report = wp.prune(setup.root, "models")
    assert report.stripped == []
    assert wp.render_prune(report) == (
        "models: every one of its 3 wheel versions is held by a branch or a tag — nothing to prune"
    )


@pytest.mark.slow
def test_nothing_to_strip_applies_nothing(setup: Setup) -> None:
    _git(setup.repo, "tag", "old", "HEAD~4")
    _git(setup.repo, "push", "-q", "origin", "old")
    before = _refs(setup.repo)
    report = wp.prune(setup.root, "models", apply=True)
    assert (report.applied, report.stripped) == (False, [])
    assert _refs(setup.repo) == before


# --- prune --apply ------------------------------------------------------------------------------------


def test_apply_strips_the_unheld_version_everywhere_and_keeps_every_tip_and_tag(setup: Setup) -> None:
    trees = {ref: _git(setup.origin, "rev-parse", f"{ref}^{{tree}}") for ref in ("main", "v-two")}
    old_main = _git(setup.origin, "rev-parse", "main")
    report = wp.prune(setup.root, "models", apply=True)

    assert report.applied and report.pushed == ["main", "v-two", "wheels-prune/map"]
    assert report.rewritten == 5  # "wheels 1" and every commit after it
    assert _git(setup.origin, "rev-parse", "main") != old_main
    assert {ref: _git(setup.origin, "rev-parse", f"{ref}^{{tree}}") for ref in trees} == trees
    _gc(setup.origin)
    assert _gone(setup.origin, setup.v1) and not _gone(setup.origin, setup.v2) and not _gone(setup.origin, setup.v3)
    assert _gone(setup.repo, setup.v1)
    assert _git(setup.repo, "rev-parse", "main") == _git(setup.origin, "rev-parse", "main")
    assert _git(setup.repo, "status", "--porcelain", "--untracked-files=no") == ""
    # The commit that added v1 stays, emptied of it; no message changed.
    assert _git(setup.repo, "log", "--format=%s", "main") == "\n".join(
        ["wheels 3", "code 2", "wheels 2", "code 1", "wheels 1", "the project"]
    )
    assert Path(report.backup or "").is_file()
    assert report.size_after is not None and report.size_before is not None
    text = wp.render_prune(report)
    assert "models: 1 of 3 wheel versions stripped (4 kB), 5 commits rewritten" in text
    assert "Every other clone of models: aisland wheels reset models, before its next pull" in text


def test_origin_keeps_a_record_of_the_versions_and_the_commits(setup: Setup) -> None:
    old = _git(setup.repo, "rev-list", "main").split()
    wp.prune(setup.root, "models", apply=True)
    new = _git(setup.repo, "rev-list", "main").split()
    record = _git(setup.origin, "rev-parse", wp.PRUNE_REF)
    assert _git(setup.origin, "show", f"{record}:{wp.STRIPPED_FILE}") == setup.v1
    pairs = dict(line.split() for line in _git(setup.origin, "show", f"{record}:{wp.COMMITS_FILE}").splitlines())
    assert pairs == {o: n for o, n in zip(old, new) if o != n}
    assert _git(setup.origin, "log", "-1", "--format=%s", record) == (
        "wheels prune: 1 wheel versions stripped, 5 commits rewritten"
    )


def test_a_second_prune_chains_its_record_on_the_first(setup: Setup) -> None:
    wp.prune(setup.root, "models", apply=True)
    first = _git(setup.origin, "rev-parse", wp.PRUNE_REF)
    _git(setup.repo, "tag", "-d", "v-two")
    _git(setup.repo, "push", "-q", "origin", ":refs/tags/v-two")
    report = wp.prune(setup.root, "models", apply=True)
    assert report.stripped == [setup.v2]
    assert _git(setup.origin, "rev-parse", f"{wp.PRUNE_REF}^") == first


def test_a_stash_survives_the_prune(setup: Setup) -> None:
    (setup.repo / "code.py").write_text("work in progress\n")
    _git(setup.repo, "stash", "-q")
    wp.prune(setup.root, "models", apply=True)
    _git(setup.repo, "stash", "pop", "-q")
    assert (setup.repo / "code.py").read_text() == "work in progress\n"


def test_a_detached_head_follows_its_rewritten_commit(setup: Setup) -> None:
    _git(setup.repo, "checkout", "-q", "--detach", "HEAD~1")
    message = _git(setup.repo, "log", "-1", "--format=%s")
    wp.prune(setup.root, "models", apply=True)
    assert not _ok(setup.repo, "symbolic-ref", "-q", "HEAD")
    assert _git(setup.repo, "log", "-1", "--format=%s") == message
    assert _git(setup.repo, "rev-parse", "HEAD") == _git(setup.repo, "rev-parse", "main~1")
    assert not _ok(setup.repo, "rev-parse", "--verify", "-q", "refs/wheels-prune/head")


def test_the_root_records_the_rewritten_twin_of_its_commit_alone(setup: Setup) -> None:
    recorded = _git(setup.repo, "rev-parse", "HEAD~1")
    _git(setup.root, "update-index", "--add", "--cacheinfo", f"160000,{recorded},models")
    _commit_index(setup.root, "record models")
    (setup.root / "staged.txt").write_text("mine\n")
    _git(setup.root, "add", "staged.txt")

    report = wp.prune(setup.root, "models", apply=True)

    twin = _git(setup.repo, "rev-parse", "main~1")
    assert report.root_pointer == (recorded, twin)
    assert _git(setup.root, "rev-parse", "HEAD:models") == twin
    assert _git(setup.root, "log", "-1", "--format=%s") == (
        "chore: sync submodule pointer — models: wheels pruned from its history"
    )
    assert _git(setup.root, "show", "--name-only", "--format=", "HEAD") == "models"
    assert _git(setup.root, "diff", "--cached", "--name-only") == "staged.txt"


def _commit_index(root: Path, message: str) -> None:
    _git(root, "commit", "-q", "-m", message)


def test_a_root_recording_an_untouched_commit_is_left(setup: Setup) -> None:
    recorded = _git(setup.repo, "rev-parse", "HEAD~5")  # "the project": before any wheel
    _git(setup.root, "update-index", "--add", "--cacheinfo", f"160000,{recorded},models")
    _commit_index(setup.root, "record models")
    head = _git(setup.root, "rev-parse", "HEAD")
    report = wp.prune(setup.root, "models", apply=True)
    assert report.root_pointer is None
    assert _git(setup.root, "rev-parse", "HEAD") == head


# --- prune: refusals -------------------------------------------------------------------------------


def _refused(setup: Setup, match: str) -> None:
    before = _refs(setup.repo)
    with pytest.raises(wp.PruneError, match=match):
        wp.prune(setup.root, "models", apply=True)
    after = _refs(setup.repo)
    assert {k: v for k, v in after.items() if not k.startswith("refs/remotes/")} == {
        k: v for k, v in before.items() if not k.startswith("refs/remotes/")
    }
    assert not _ok(setup.origin, "rev-parse", "--verify", "-q", wp.PRUNE_REF)


@pytest.mark.slow
def test_a_change_no_commit_holds_is_refused(setup: Setup) -> None:
    (setup.repo / "code.py").write_text("changed\n")
    _refused(setup, r"models has changes no commit holds: M code.py")


@pytest.mark.slow
def test_a_branch_with_commits_origin_lacks_is_refused(setup: Setup) -> None:
    (setup.repo / "code.py").write_text("three\n")
    _commit(setup.repo, "unpushed")
    _refused(setup, r"models: main has 1 commit origin/main lacks — push or drop them first")


@pytest.mark.slow
def test_a_branch_behind_origin_is_refused(tmp_path: Path, setup: Setup) -> None:
    other = _second_clone(tmp_path, setup) / "models"
    (other / "code.py").write_text("theirs\n")
    _commit(other, "theirs")
    _git(other, "push", "-q", "origin", "main")
    _refused(setup, r"models: main is 1 commit behind origin/main — pull first")


@pytest.mark.slow
def test_a_tag_that_differs_from_origins_is_refused(setup: Setup) -> None:
    _git(setup.repo, "tag", "-f", "-a", "v-two", "-m", "moved", "HEAD")
    _refused(setup, r"models: tag v-two differs from origin's — make them one first")


@pytest.mark.slow
def test_without_apply_the_refusals_are_listed_not_raised(setup: Setup) -> None:
    (setup.repo / "code.py").write_text("changed\n")
    report = wp.prune(setup.root, "models")
    assert report.problems == ["models has changes no commit holds: M code.py"]
    assert "--apply would stop:\n  models has changes no commit holds: M code.py" in wp.render_prune(report)


@pytest.mark.slow
def test_without_git_filter_repo_apply_names_the_extra_and_a_dry_run_still_works(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wp.importlib.util, "find_spec", lambda name: None)
    assert wp.prune(setup.root, "models").stripped == [setup.v1]
    with pytest.raises(wp.PruneError, match=r"uv pip install 'aisland\[prune\]'"):
        wp.prune(setup.root, "models", apply=True)


@pytest.mark.slow
def test_a_project_without_origin_is_refused(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "workspace")
    repo = root / "models"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "models"\nversion = "0.1.0"\n')
    _commit(repo, "the project")
    with pytest.raises(wp.PruneError, match="models has no remote 'origin'"):
        wp.prune(root, "models")


def test_a_push_made_meanwhile_refuses_the_whole_push_and_the_backup_restores(
    tmp_path: Path, setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _second_clone(tmp_path, setup) / "models"
    real_strip = wp.strip
    before = _refs(setup.repo)

    def strip_while_someone_pushes(*args, **kwargs):  # type: ignore[no-untyped-def]
        (other / "code.py").write_text("theirs\n")
        _commit(other, "theirs")
        _git(other, "push", "-q", "origin", "main")
        return real_strip(*args, **kwargs)

    monkeypatch.setattr(wp, "strip", strip_while_someone_pushes)
    with pytest.raises(wp.PruneError, match="origin refused the push") as refused:
        wp.prune(setup.root, "models", apply=True)
    assert _git(setup.origin, "rev-parse", "main") == _git(other, "rev-parse", "HEAD")
    assert _git(setup.origin, "rev-parse", "v-two") == before["refs/tags/v-two"]
    restore = str(refused.value.message).rsplit("put it back with: ", 1)[1]
    subprocess.run(restore, shell=True, check=True, capture_output=True)
    assert {k: v for k, v in _refs(setup.repo).items() if k in before} == before


# --- reset --------------------------------------------------------------------------------------------


def test_reset_brings_another_clone_onto_the_pruned_history_keeping_its_work(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    other = other_root / "models"
    (other / "code.py").write_text("mine\n")
    _commit(other, "unpushed")
    _git(other, "tag", "local-only", "HEAD~3")  # "wheels 2": a commit the prune rewrites
    wp.prune(setup.root, "models", apply=True)

    report = wp.reset(other_root, "models")

    assert report.prunes == 1
    assert _git(other, "rev-parse", "main~1") == _git(setup.origin, "rev-parse", "main")
    assert _git(other, "log", "-1", "--format=%s") == "unpushed"
    assert (other / "code.py").read_text() == "mine\n"
    assert report.ahead == {"main": 1}
    assert _git(other, "rev-parse", "local-only") == _git(setup.origin, "rev-parse", "main~2")
    assert _git(other, "rev-parse", "v-two") == _git(setup.origin, "rev-parse", "v-two")
    assert report.tags_differing == []
    assert _gone(other, setup.v1)
    assert {ref for ref, _old, _new in report.moved} == {"main", "local-only", "v-two"}
    assert _git(other, "status", "--porcelain", "--untracked-files=no") == ""
    text = wp.render_reset(report)
    assert text.splitlines()[0] == "models: 1 prune taken"
    assert "  main: 1 commit origin lacks, kept on top of origin's" in text


def test_reset_takes_every_prune_it_missed(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    wp.prune(setup.root, "models", apply=True)
    _git(setup.repo, "tag", "-d", "v-two")
    _git(setup.repo, "push", "-q", "origin", ":refs/tags/v-two")
    wp.prune(setup.root, "models", apply=True)
    other = other_root / "models"
    _git(other, "tag", "-d", "v-two")

    report = wp.reset(other_root, "models")

    assert report.prunes == 2
    assert _git(other, "rev-parse", "main") == _git(setup.origin, "rev-parse", "main")
    assert _gone(other, setup.v1) and _gone(other, setup.v2)


def test_reset_a_second_time_changes_nothing(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    wp.prune(setup.root, "models", apply=True)
    wp.reset(other_root, "models")
    before = _refs(other_root / "models")
    report = wp.reset(other_root, "models")
    assert report.prunes == 0 and report.backup is None and report.root_pointer is None
    assert _refs(other_root / "models") == before
    assert wp.render_reset(report) == "models: already on origin's history — nothing to reset"


@pytest.mark.slow
def test_reset_with_nothing_pruned_on_origin_changes_nothing(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    before = _refs(other_root / "models")
    assert wp.reset(other_root, "models").prunes == 0
    assert _refs(other_root / "models") == before


def test_reset_sets_uncommitted_changes_aside_and_puts_them_back(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    other = other_root / "models"
    wp.prune(setup.root, "models", apply=True)
    (other / "code.py").write_text("staged\n")
    _git(other, "add", "code.py")
    (other / "code.py").write_text("staged, then edited\n")
    (other / "pyproject.toml").write_text('[project]\nname = "models"\nversion = "0.2.0"\n')
    (other / "notes.txt").write_text("untracked\n")

    report = wp.reset(other_root, "models")

    assert report.prunes == 1
    assert _git(other, "rev-parse", "main") == _git(setup.origin, "rev-parse", "main")
    assert (other / "code.py").read_text() == "staged, then edited\n"
    assert _git(other, "show", ":code.py") == "staged"
    assert "0.2.0" in (other / "pyproject.toml").read_text()
    assert (other / "notes.txt").read_text() == "untracked\n"
    assert _git(other, "stash", "list") == ""
    assert report.set_aside == ["MM code.py", "M pyproject.toml"]
    assert "  uncommitted changes set aside and put back: code.py, pyproject.toml" in wp.render_reset(report)


@pytest.mark.slow
def test_a_reset_that_fails_after_setting_changes_aside_says_where_they_are(
    tmp_path: Path, setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_root = _second_clone(tmp_path, setup)
    other = other_root / "models"
    wp.prune(setup.root, "models", apply=True)
    (other / "code.py").write_text("mine\n")

    def failing_strip(repo: Path, *args: object, **kwargs: object) -> Dict[str, str]:
        raise wp.PruneError("models: git-filter-repo failed: broken")

    monkeypatch.setattr(wp, "strip", failing_strip)
    with pytest.raises(wp.PruneError) as failed:
        wp.reset(other_root, "models")
    put_back = (
        f"git -C {other} stash apply --index refs/wheels-prune/set-aside && "
        f"git -C {other} update-ref -d refs/wheels-prune/set-aside"
    )
    assert failed.value.message.endswith(f"put them back with: {put_back}")
    subprocess.run(put_back, shell=True, check=True, capture_output=True)
    assert (other / "code.py").read_text() == "mine\n"


def test_git_filter_repo_reads_a_config_value_written_over_several_lines(tmp_path: Path) -> None:
    """git-filter-repo 2.47.0 reads `git config --list` as `key=value` lines; aisland runs it with a reader of
    `--null`'s output, so an alias of several lines (aisland's own, before 2026-10-29) no longer stops it."""
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "alias.many", "!f() {\n    echo one;\n}; f")
    with (repo / ".git" / "config").open("a") as config:
        config.write("[core]\n\tflag\n")  # a key with no value at all
    with pytest.raises(ValueError):
        git_filter_repo.GitUtils.get_config_settings(str(repo))
    settings = filter_repo.config_settings(str(repo))
    assert settings[b"alias.many"] == b"!f() {\n    echo one;\n}; f"
    assert settings[b"core.flag"] == b""
    assert settings[b"core.bare"] == b"false"
    # The reader aisland replaces is still git-filter-repo's: replacing a name it no longer has would do nothing.
    assert callable(getattr(git_filter_repo.GitUtils, "get_config_settings"))


@pytest.mark.slow
def test_prune_and_reset_run_with_an_alias_of_several_lines_in_the_config(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    other = other_root / "models"
    for repo in (setup.repo, other):
        _git(repo, "config", "alias.tag-all", '!f() {\n    git tag -a "$1";\n}; f')
    assert wp.prune(setup.root, "models", apply=True).applied
    assert wp.reset(other_root, "models").prunes == 1
    assert _git(other, "rev-parse", "main") == _git(setup.origin, "rev-parse", "main")
    assert "\n" in _git(other, "config", "alias.tag-all")  # the user's config is left as it was


def _record_in_root(root: Path, commit: str, *, untracked: bool = False) -> None:
    if untracked:
        (root / ".gitmodules").write_text(
            '[submodule "models"]\n\tpath = models\n\turl = ../origin/models.git\n\tignore = all\n'
        )
        _git(root, "add", ".gitmodules")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},models")
    _commit_index(root, "record models")


@pytest.mark.slow
def test_reset_records_the_rewritten_twin_of_the_roots_commit_alone(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    recorded = _git(other_root / "models", "rev-parse", "HEAD~1")
    _record_in_root(other_root, recorded)
    (other_root / "staged.txt").write_text("mine\n")
    _git(other_root, "add", "staged.txt")
    wp.prune(setup.root, "models", apply=True)

    report = wp.reset(other_root, "models")

    twin = _git(setup.origin, "rev-parse", "main~1")
    assert report.root_pointer == (recorded, twin)
    assert _git(other_root, "rev-parse", "HEAD:models") == twin
    assert _git(other_root, "log", "-1", "--format=%s") == (
        "chore: sync submodule pointer — models: wheels pruned from its history"
    )
    assert _git(other_root, "show", "--name-only", "--format=", "HEAD") == "models"
    assert _git(other_root, "diff", "--cached", "--name-only") == "staged.txt"
    assert (
        f"  the root records models at {twin[:7]} (was {recorded[:7]}), committed; push the root as usual"
        in wp.render_reset(report)
    )


@pytest.mark.slow
def test_a_clone_already_reset_still_moves_an_untracked_projects_pointer(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    recorded = _git(other_root / "models", "rev-parse", "HEAD")
    wp.prune(setup.root, "models", apply=True)
    wp.reset(other_root, "models")
    _record_in_root(other_root, recorded, untracked=True)  # the root still names the commit from before the prune

    report = wp.reset(other_root, "models")

    twin = _git(setup.origin, "rev-parse", "main")
    assert report.prunes == 0 and report.backup is None
    assert report.root_pointer == (recorded, twin)
    assert _git(other_root, "rev-parse", "HEAD:models") == twin
    assert wp.render_reset(report) == (
        "models: already on origin's history — nothing to reset\n"
        f"  the root records models at {twin[:7]} (was {recorded[:7]}), committed; push the root as usual"
    )


@pytest.mark.slow
def test_reset_leaves_a_root_recording_a_commit_no_prune_rewrote(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    _record_in_root(other_root, _git(other_root / "models", "rev-parse", "HEAD~5"))  # before any wheel
    head = _git(other_root, "rev-parse", "HEAD")
    wp.prune(setup.root, "models", apply=True)
    assert wp.reset(other_root, "models").root_pointer is None
    assert _git(other_root, "rev-parse", "HEAD") == head


def test_reset_moves_a_detached_head_with_its_commit(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    other = other_root / "models"
    _git(other, "checkout", "-q", "--detach", "HEAD~2")
    wp.prune(setup.root, "models", apply=True)
    wp.reset(other_root, "models")
    assert not _ok(other, "symbolic-ref", "-q", "HEAD")
    assert _git(other, "rev-parse", "HEAD") == _git(setup.origin, "rev-parse", "main~2")


def test_reset_names_a_tag_that_differs_from_origins_and_leaves_it(tmp_path: Path, setup: Setup) -> None:
    other_root = _second_clone(tmp_path, setup)
    other = other_root / "models"
    _git(other, "tag", "-f", "-a", "v-two", "-m", "mine", "HEAD")
    wp.prune(setup.root, "models", apply=True)
    report = wp.reset(other_root, "models")
    assert report.tags_differing == ["v-two"]
    assert "tags that differ from origin's, left as they are: v-two" in wp.render_reset(report)


# --- the command line --------------------------------------------------------------------------------


def _cli(root: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    result = CliRunner().invoke(main, ["wheels", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_the_commands_print_the_reports_and_their_json(
    tmp_path: Path, setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_root = _second_clone(tmp_path, setup)
    assert "1 of 3 wheel versions are held by no branch or tag" in _cli(setup.root, monkeypatch, "prune", "models")
    data = json.loads(_cli(setup.root, monkeypatch, "prune", "models", "--json"))
    assert data["stripped"] == [setup.v1] and data["applied"] is False
    assert "stripped" in _cli(setup.root, monkeypatch, "prune", "models", "--apply")
    reset: Optional[Dict[str, object]] = json.loads(_cli(other_root, monkeypatch, "reset", "models", "--json"))
    assert reset is not None and reset["prunes"] == 1


@pytest.mark.slow
def test_a_refusal_is_one_error_line_and_exit_1(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    (setup.repo / "code.py").write_text("changed\n")
    monkeypatch.setattr(locations, "workspace_root", lambda: setup.root)
    result = CliRunner().invoke(main, ["wheels", "prune", "models", "--apply"])
    assert result.exit_code == 1
    assert result.output.startswith("Error: models has changes no commit holds: M code.py")


def test_the_commands_printed_to_put_a_clone_back_survive_a_path_with_spaces(tmp_path: Path) -> None:
    """They are pasted at the worst moment: a folder such as `~/My Work/models` must stay one word."""
    repo, bundle = tmp_path / "My Work" / "models", tmp_path / "My Work" / "models.bundle"
    restore = shlex.split(wp._restore_command(repo, bundle))
    assert restore[:4] == ["git", "-C", str(repo), "fetch"] and str(bundle) in restore
    assert shlex.split(wp._put_back_command(repo))[:3] == ["git", "-C", str(repo)]
