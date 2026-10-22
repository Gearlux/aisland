"""Tests for project mirrors (`aisland workspace configure mirror`, `aisland workspace mirror sync`, aisland/mirrors.py).

Every test builds a throwaway workspace: a root repository holding two real submodules,
``alpha`` and ``beta``, whose primaries are bare repositories under ``origin/``, plus an empty
bare ``mirror/alpha.git``. ``GIT_CONFIG_GLOBAL`` points at a sandbox file, so the developer's
own git configuration is neither read nor touched.

What a mirror must guarantee, each pinned below:
- the two configuration layers (committed: the project's ``mirror`` in ``workspace.toml``; per-machine:
  ``aisland.mirror.<project>.mirror``) and the ``mirror`` remote that follows the effective one;
- a sync pushes the primary's ``dev/main``, ``main`` and tags, fast-forward only;
- it never forces, never deletes, never overwrites a tag, and refuses a branch the mirror moved;
- a project without a mirror is never contacted, one without a checkout is skipped;
- ``aisland workspace configure mirror`` (the listing) pushes nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Dict, List

import pytest
from click.testing import CliRunner

from aisland import mirrors as m
from aisland.cli import main as cli_main
from aisland.workspace_file import FILE_NAME

BRANCH = "dev/main"


class Workspace:
    """A root with submodules ``alpha`` and ``beta``, their primaries, and alpha's mirror."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp
        for key in list(os.environ):
            if key.startswith("GIT_"):
                monkeypatch.delenv(key)
        env: Dict[str, str] = {
            "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        self.root = tmp / "root"
        self.origin = tmp / "origin"
        self.mirror = tmp / "mirror"
        self.seeds: Dict[str, Path] = {}

    def build(self) -> "Workspace":
        for key, value in (("init.defaultBranch", BRANCH), ("protocol.file.allow", "always")):
            self.git(self.tmp, "config", "--global", key, value)
        for name in ("alpha", "beta"):
            self.git(self.tmp, "init", "-q", "--bare", str(self.origin / f"{name}.git"))
            seed = self.tmp / f"seed-{name}"
            self.git(self.tmp, "clone", "-q", str(self.origin / f"{name}.git"), str(seed))
            self.seeds[name] = seed
            self.commit(name, "init")
            self.git(seed, "branch", "main")
            self.git(seed, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
            self.git(seed, "push", "-q", "origin", BRANCH, "main", "v0.1.0")
        self.git(self.tmp, "init", "-q", "--bare", str(self.mirror / "alpha.git"))
        self.git(self.tmp, "init", "-q", str(self.root))
        for name in ("alpha", "beta"):
            self.git(self.root, "submodule", "add", "-q", str(self.origin / f"{name}.git"), name)
        self.git(self.root, "commit", "-q", "-m", "workspace")
        return self

    def relocate(self) -> "Workspace":
        """Point a copied workspace's remotes at the copy's own primaries."""
        for name in ("alpha", "beta"):
            url = str(self.origin / f"{name}.git")
            self.seeds[name] = self.tmp / f"seed-{name}"
            self.git(self.root, "config", "-f", ".gitmodules", f"submodule.{name}.url", url)
            self.git(self.root, "config", f"submodule.{name}.url", url)
            self.git(self.root / name, "remote", "set-url", "origin", url)
            self.git(self.seeds[name], "remote", "set-url", "origin", url)
        self.git(self.root, "commit", "-q", "-am", "relocate")
        return self

    # --- plumbing ---------------------------------------------------------------------------

    def git(self, repo: Path, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed in {repo}:\n{result.stdout}{result.stderr}")
        return result.stdout.strip()

    def commit(self, name: str, message: str) -> str:
        seed = self.seeds[name]
        path = seed / "f.txt"
        path.write_text((path.read_text() if path.exists() else "") + message + "\n")
        self.git(seed, "add", "f.txt")
        self.git(seed, "commit", "-q", "-m", message)
        return self.git(seed, "rev-parse", "HEAD")

    def publish(self, name: str, message: str) -> str:
        """A new commit on the primary's dev/main, as another machine would push it."""
        sha = self.commit(name, message)
        self.git(self.seeds[name], "push", "-q", "origin", BRANCH)
        return sha

    def mirror_url(self, name: str = "alpha") -> str:
        return str(self.mirror / f"{name}.git")

    def mirror_refs(self, name: str = "alpha") -> Dict[str, str]:
        listing = self.git(self.tmp, "ls-remote", self.mirror_url(name))
        return {ref: sha for sha, ref in (line.split("\t") for line in listing.splitlines())}

    def primary_refs(self, name: str = "alpha") -> Dict[str, str]:
        listing = self.git(self.tmp, "ls-remote", str(self.origin / f"{name}.git"))
        return {ref: sha for sha, ref in (line.split("\t") for line in listing.splitlines())}

    def remote_url(self, name: str = "alpha") -> str:
        return self.git(self.root / name, "config", "--get", "remote.mirror.url", check=False)


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One workspace built per module (a few dozen git calls); each test works on a copy."""
    tmp = tmp_path_factory.mktemp("mirrors-template")
    with pytest.MonkeyPatch.context() as mp:
        Workspace(tmp, mp).build()
    return tmp


@pytest.fixture
def ws(template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    dest = tmp_path / "ws"
    shutil.copytree(template, dest, symlinks=True)
    return Workspace(dest, monkeypatch).relocate()


def _outcomes(result: m.ProjectSync) -> Dict[str, str]:
    return {ref.ref: ref.outcome for ref in result.refs}


# --- configuration layers --------------------------------------------------------------------


class TestSetAndUnset:
    def test_set_records_the_committed_table_and_adds_the_remote(
        self, ws: Workspace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root) == 0

        data = tomllib.loads((ws.root / FILE_NAME).read_text())
        assert data["project"] == {"alpha": {"mirror": ws.mirror_url()}}
        assert ws.remote_url() == ws.mirror_url()
        out = capsys.readouterr().out
        assert "recorded in workspace.toml" in out
        assert "aisland workspace mirror sync alpha" in out

    def test_a_project_without_a_mirror_has_none_and_one_that_is_no_address_is_warned_about(
        self, ws: Workspace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (ws.root / FILE_NAME).write_text('[project."alpha"]\nurl = "u"\n\n[project."beta"]\nurl = "v"\nmirror = 3\n')
        assert m.read_committed_mirrors(ws.root) == {}
        assert f"{ws.root / FILE_NAME}:4: project 'beta' has a mirror that is no address" in capsys.readouterr().out

    def test_the_remote_never_imports_the_mirrors_tags(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        assert ws.git(ws.root / "alpha", "config", "--get", "remote.mirror.tagOpt") == "--no-tags"

    def test_set_names_both_hosts_the_code_moves_between(
        self, ws: Workspace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws.git(ws.root, "config", "-f", ".gitmodules", "submodule.alpha.url", "git@bitbucket.example.com:t/alpha.git")
        m.set_mirror("alpha", "git@github.com:Org/alpha.git", workspace_root=ws.root)
        out = capsys.readouterr().out
        assert "bitbucket.example.com" in out and "github.com" in out

    def test_machine_only_writes_git_config_and_leaves_workspace_toml_alone(self, ws: Workspace) -> None:
        assert m.set_mirror("alpha", ws.mirror_url(), machine_only=True, workspace_root=ws.root) == 0

        assert not (ws.root / FILE_NAME).exists()
        assert ws.git(ws.root, "config", "--local", "--get", "aisland.mirror.alpha.mirror") == ws.mirror_url()
        assert m.effective_mirror(ws.root, "alpha") == ws.mirror_url()
        assert ws.remote_url() == ws.mirror_url()

    def test_the_override_beats_the_committed_url(self, ws: Workspace) -> None:
        m.set_mirror("alpha", "/committed/alpha.git", workspace_root=ws.root)
        m.set_mirror("alpha", ws.mirror_url(), machine_only=True, workspace_root=ws.root)

        assert m.effective_mirror(ws.root, "alpha") == ws.mirror_url()
        assert ws.remote_url() == ws.mirror_url()

    def test_unset_machine_only_falls_back_to_the_committed_url(self, ws: Workspace) -> None:
        m.set_mirror("alpha", "/committed/alpha.git", workspace_root=ws.root)
        m.set_mirror("alpha", ws.mirror_url(), machine_only=True, workspace_root=ws.root)

        assert m.unset_mirror("alpha", machine_only=True, workspace_root=ws.root) == 0
        assert m.effective_mirror(ws.root, "alpha") == "/committed/alpha.git"
        assert ws.remote_url() == "/committed/alpha.git"

    def test_unset_removes_the_entry_and_the_remote(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        assert m.unset_mirror("alpha", workspace_root=ws.root) == 0

        assert not (ws.root / FILE_NAME).exists()
        assert ws.remote_url() == ""

    def test_unset_leaves_a_remote_aisland_did_not_create(self, ws: Workspace) -> None:
        ws.git(ws.root / "alpha", "remote", "add", "mirror", "/hand/made.git")
        assert m.unset_mirror("alpha", workspace_root=ws.root) == 0
        assert ws.remote_url() == "/hand/made.git"

    def test_set_refuses_a_name_that_is_not_a_submodule(
        self, ws: Workspace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert m.set_mirror("gamma", ws.mirror_url(), workspace_root=ws.root) == 1
        assert "'gamma' is not a submodule" in capsys.readouterr().err
        assert not (ws.root / FILE_NAME).exists()

    def test_set_refuses_the_primary_itself(self, ws: Workspace, capsys: pytest.CaptureFixture[str]) -> None:
        assert m.set_mirror("alpha", str(ws.origin / "alpha.git"), workspace_root=ws.root) == 1
        assert "is alpha's primary" in capsys.readouterr().err
        assert not (ws.root / FILE_NAME).exists()

    def test_set_refuses_to_take_over_a_remote_it_did_not_create(
        self, ws: Workspace, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ws.git(ws.root / "alpha", "remote", "add", "mirror", "/hand/made.git")

        assert m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root) == 1
        err = capsys.readouterr().err
        assert "already has a remote named `mirror`" in err
        assert "git -C alpha remote rename mirror" in err
        assert ws.remote_url() == "/hand/made.git"
        assert not (ws.root / FILE_NAME).exists()

    def test_setup_adds_the_remote_a_committed_mirror_names(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        ws.git(ws.root / "alpha", "remote", "remove", "mirror")

        m.apply_all(ws.root)
        assert ws.remote_url() == ws.mirror_url()

    def test_setup_removes_a_remote_whose_mirror_was_unset_elsewhere(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        (ws.root / FILE_NAME).unlink()  # another machine's `aisland workspace configure mirror unset`, pulled

        m.apply_all(ws.root)
        assert ws.remote_url() == ""


# --- sync ------------------------------------------------------------------------------------


class TestSync:
    def test_an_empty_mirror_gets_both_branches_and_the_tags(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)

        [result] = m.sync(workspace_root=ws.root)
        assert result.project == "alpha"
        assert result.state == "synced"
        assert _outcomes(result) == {BRANCH: "pushed", "main": "pushed", "tags": "pushed"}
        assert ws.mirror_refs() == ws.primary_refs()

    def test_a_second_sync_finds_everything_in_sync(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        m.sync(workspace_root=ws.root)

        [result] = m.sync(workspace_root=ws.root)
        assert _outcomes(result) == {BRANCH: "in sync", "main": "in sync", "tags": "in sync"}

    def test_new_primary_commits_are_pushed_and_counted(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        m.sync(workspace_root=ws.root)
        ws.publish("alpha", "two")
        head = ws.publish("alpha", "three")

        [result] = m.sync(workspace_root=ws.root)
        [dev] = [ref for ref in result.refs if ref.ref == BRANCH]
        assert (dev.outcome, dev.count) == ("pushed", 2)
        assert ws.mirror_refs()[f"refs/heads/{BRANCH}"] == head

    def test_the_mirror_follows_the_primary_not_unpushed_local_commits(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        sub = ws.root / "alpha"
        ws.git(sub, "checkout", "-q", BRANCH)
        (sub / "local.txt").write_text("not pushed\n")
        ws.git(sub, "add", "local.txt")
        ws.git(sub, "commit", "-q", "-m", "local only")

        m.sync(workspace_root=ws.root)
        assert ws.mirror_refs()[f"refs/heads/{BRANCH}"] == ws.primary_refs()[f"refs/heads/{BRANCH}"]

    def test_a_branch_the_mirror_moved_is_refused_and_left_alone(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        m.sync(workspace_root=ws.root)
        ws.git(ws.seeds["alpha"], "remote", "add", "mirror", ws.mirror_url())
        ws.commit("alpha", "fix: made on the mirror")
        ws.git(ws.seeds["alpha"], "push", "-q", "mirror", BRANCH)
        moved = ws.mirror_refs()[f"refs/heads/{BRANCH}"]
        ws.git(ws.seeds["alpha"], "reset", "-q", "--hard", f"origin/{BRANCH}")
        ws.publish("alpha", "primary moves too")
        ws.git(ws.seeds["alpha"], "push", "-q", "origin", f"{BRANCH}:main")

        [result] = m.sync(workspace_root=ws.root)
        [dev] = [ref for ref in result.refs if ref.ref == BRANCH]
        assert result.state == "refused"
        assert dev.outcome == "refused"
        assert "1 commit the primary does not" in dev.detail
        assert "fix: made on the mirror" in dev.detail
        assert f"git -C alpha fetch mirror && git -C alpha merge mirror/{BRANCH}" in dev.detail
        assert ws.mirror_refs()[f"refs/heads/{BRANCH}"] == moved
        assert _outcomes(result)["main"] == "pushed"

    def test_a_branch_or_tag_only_on_the_mirror_is_never_deleted(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        seed = ws.seeds["alpha"]
        ws.git(seed, "remote", "add", "mirror", ws.mirror_url())
        ws.git(seed, "push", "-q", "mirror", f"{BRANCH}:refs/heads/feature")
        ws.git(seed, "tag", "mirror-only")
        ws.git(seed, "push", "-q", "mirror", "mirror-only")

        m.sync(workspace_root=ws.root)
        refs = ws.mirror_refs()
        assert "refs/heads/feature" in refs
        assert "refs/tags/mirror-only" in refs

    def test_a_tag_that_differs_on_the_mirror_is_refused_not_overwritten(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        seed = ws.seeds["alpha"]
        ws.git(seed, "remote", "add", "mirror", ws.mirror_url())
        ws.commit("alpha", "elsewhere")
        ws.git(seed, "tag", "-f", "-a", "v0.1.0", "-m", "another v0.1.0")
        ws.git(seed, "push", "-q", "mirror", "v0.1.0")
        theirs = ws.mirror_refs()["refs/tags/v0.1.0"]

        [result] = m.sync(workspace_root=ws.root)
        [tags] = [ref for ref in result.refs if ref.ref == "tags"]
        assert tags.outcome == "refused"
        assert "v0.1.0" in tags.detail
        assert ws.mirror_refs()["refs/tags/v0.1.0"] == theirs

    def test_a_branch_missing_on_the_primary_is_reported_not_failed(self, ws: Workspace) -> None:
        ws.git(ws.seeds["alpha"], "push", "-q", "origin", ":main")
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)

        [result] = m.sync(workspace_root=ws.root)
        assert result.state == "synced"
        assert _outcomes(result)["main"] == "absent"

    def test_a_project_without_a_mirror_is_never_contacted(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        ws.git(ws.root / "beta", "remote", "set-url", "origin", "/nowhere/beta.git")

        results = m.sync(workspace_root=ws.root)
        assert [r.project for r in results] == ["alpha"]

    def test_a_project_without_a_checkout_is_skipped(self, ws: Workspace) -> None:
        m.set_mirror("beta", str(ws.tmp / "mirror" / "beta.git"), workspace_root=ws.root)
        ws.git(ws.root, "submodule", "deinit", "-q", "beta")

        [result] = m.sync("beta", workspace_root=ws.root)
        assert result.state == "skipped"
        assert "no checkout" in result.note

    def test_an_unreachable_mirror_fails_naming_the_project(self, ws: Workspace) -> None:
        m.set_mirror("alpha", str(ws.tmp / "nowhere.git"), workspace_root=ws.root)

        [result] = m.sync(workspace_root=ws.root)
        assert result.state == "failed"
        assert result.note

    def test_network_calls_never_wait_on_a_password_prompt(
        self, ws: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)
        seen: List[Dict[str, str]] = []
        real_run = subprocess.run

        def spy(cmd: List[str], *args: object, **kwargs: object) -> "subprocess.CompletedProcess[str]":
            if cmd[:1] == ["git"] and set(cmd) & {"fetch", "push", "ls-remote"}:
                seen.append(dict(kwargs.get("env") or {}))  # type: ignore[call-overload]
            return real_run(cmd, *args, **kwargs)  # type: ignore[call-overload,no-any-return]

        monkeypatch.setattr(m.subprocess, "run", spy)
        m.sync(workspace_root=ws.root)
        assert seen
        assert all(env.get("GIT_TERMINAL_PROMPT") == "0" for env in seen)

    def test_a_dry_run_pushes_nothing_and_says_what_it_would(self, ws: Workspace) -> None:
        m.set_mirror("alpha", ws.mirror_url(), workspace_root=ws.root)

        [result] = m.sync(workspace_root=ws.root, push=False)
        assert _outcomes(result) == {BRANCH: "to push", "main": "to push", "tags": "to push"}
        assert ws.mirror_refs() == {}


# --- CLI -------------------------------------------------------------------------------------


class TestCli:
    @pytest.fixture(autouse=True)
    def _root(self, ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(m, "get_workspace_root", lambda: ws.root)
        monkeypatch.setattr("aisland.cli.get_workspace_root", lambda: ws.root)

    @staticmethod
    def _set(runner: CliRunner, ws: Workspace) -> None:
        result = runner.invoke(cli_main, ["workspace", "configure", "mirror", "set", "alpha", ws.mirror_url()])
        assert result.exit_code == 0, result.output

    def test_sync_prints_a_row_per_ref_and_exits_0(self, ws: Workspace) -> None:
        runner = CliRunner()
        self._set(runner, ws)

        result = runner.invoke(cli_main, ["workspace", "mirror", "sync"])
        assert result.exit_code == 0, result.output
        assert f"==> alpha → {ws.mirror_url()}" in result.output
        assert f"{BRANCH:<10}pushed (new branch)" in result.output
        assert "tags      pushed v0.1.0" in result.output

    def test_sync_exits_1_when_a_ref_is_refused(self, ws: Workspace) -> None:
        runner = CliRunner()
        self._set(runner, ws)
        runner.invoke(cli_main, ["workspace", "mirror", "sync"])
        seed = ws.seeds["alpha"]
        ws.git(seed, "remote", "add", "mirror", ws.mirror_url())
        ws.commit("alpha", "mirror only")
        ws.git(seed, "push", "-q", "mirror", BRANCH)

        result = runner.invoke(cli_main, ["workspace", "mirror", "sync", "alpha"])
        assert result.exit_code == 1
        assert "REFUSED" in result.output

    def test_the_listing_shows_the_layers_and_the_state(self, ws: Workspace) -> None:
        runner = CliRunner()
        self._set(runner, ws)
        runner.invoke(cli_main, ["workspace", "mirror", "sync"])
        ws.publish("alpha", "two")

        result = runner.invoke(cli_main, ["workspace", "configure", "mirror"])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert lines[0].split() == ["PROJECT", "COMMITTED", "OVERRIDE", "STATE"]
        assert lines[1].split()[:3] == ["alpha", ws.mirror_url(), "-"]
        assert f"{BRANCH} 1 behind" in lines[1] and "main in sync" in lines[1]
        assert ws.mirror_refs()[f"refs/heads/{BRANCH}"] != ws.primary_refs()[f"refs/heads/{BRANCH}"]

    def test_the_listing_with_nothing_configured_says_how_to_start(self) -> None:
        result = CliRunner().invoke(cli_main, ["workspace", "configure", "mirror"])
        assert result.exit_code == 0
        assert result.output.splitlines() == [
            "No project has a mirror.",
            "Set one with: aisland workspace configure mirror set <project> <url>",
        ]

    def test_unset_through_the_cli(self, ws: Workspace) -> None:
        runner = CliRunner()
        self._set(runner, ws)
        result = runner.invoke(cli_main, ["workspace", "configure", "mirror", "unset", "alpha"])
        assert result.exit_code == 0
        assert ws.remote_url() == ""

    @pytest.mark.parametrize("command", [["mirror", "list"], ["mirror", "sync"]])
    def test_the_mirror_group_is_gone(self, command: List[str]) -> None:
        result = CliRunner().invoke(cli_main, command)
        assert result.exit_code == 2
        assert "No such command 'mirror'" in result.output
