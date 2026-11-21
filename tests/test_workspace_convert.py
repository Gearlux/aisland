"""`aisland workspace convert`: a workspace of submodules becomes a folder of clones around its root project.

A superproject keeps every project's repository in its own `.git/modules/<name>`, each project folder holding only a
`.git` file that points there, and this machine's choices in its `.git/config`. Converting moves each repository into
its folder (`<name>/.git`, `core.worktree` gone, its worktrees repaired), this machine's `aisland.*` settings into the
root project's `.git/config`, and the old root repository with the files it tracks into aisland's backup store — never
deleted. What it does not track (`.env`, `.venv`, the clones beside it) stays. Work no remote has refuses it before
anything moves, and so does a missing root project: the workspace's shared files must already live in one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import pytest
from click.testing import CliRunner

from aisland import backups, cli, convert, locations
from aisland.errors import AislandError
from aisland.workspace import activation
from aisland.workspace import file as workspace_file
from aisland.workspace import root_project

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


def _remote(tmp_path: Path, name: str, files: Dict[str, str]) -> Path:
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    for path, text in files.items():
        (repo / path).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        _git(repo, *args)
    return repo


@dataclass
class Old:
    """`work/`, a superproject on this machine: `models` checked out, `tools` off here (its repository only in
    `.git/modules`), this machine's choices in `.git/config`, and the root project `acme` already cloned beside them."""

    root: Path
    project: Path
    tmp: Path


@pytest.fixture
def old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Old:
    models = _remote(tmp_path, "models", {"pyproject.toml": '[project]\nname = "models"\nversion = "0.1.0"\n'})
    tools = _remote(tmp_path, "tools", {"pyproject.toml": '[project]\nname = "tools"\nversion = "0.1.0"\n'})
    listed = textwrap.dedent(
        f"""\
        [workspace]
        format = 1
        root = "acme"

        [project."acme"]
        url = "{tmp_path / 'remote' / 'acme'}"
        branch = "main"

        [project."models"]
        url = "{models}"
        branch = "main"

        [project."tools"]
        url = "{tools}"
        branch = "main"
        active = false
        """
    )
    acme = _remote(tmp_path, "acme", {"workspace.toml": listed, "AGENTS.md": "# acme rules\n", "TASKS.md": "# tasks\n"})
    origin = tmp_path / "remote" / "root.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", str(origin))
    (root / "AGENTS.md").write_text("@.agents/rules.md\n\n# acme rules\n")
    (root / "TASKS.md").write_text("# tasks\n")
    (root / ".gitignore").write_text(".env\n.venv/\n")
    (root / "workspace.toml").write_text(f'[workspace]\nformat = 1\n\n[project."models"]\nurl = "{models}"\n')
    _git(root, "submodule", "add", "-q", str(models), "models")
    _git(root, "submodule", "add", "-q", str(tools), "tools")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "workspace")
    _git(root, "push", "-q", "-u", "origin", "main")
    _git(root, "submodule", "deinit", "-q", "-f", "tools")  # off on this machine: its repository only in .git/modules
    _git(root, "config", "--local", "aisland.active.tools.active", "false")
    _git(root, "config", "--local", "aisland.editor.theme", "Solarized Dark")
    (root / ".env").write_text("DATA_ROOT=/data\n")
    (root / ".venv").mkdir()
    _git(root, "clone", "-q", str(acme), "acme")  # the root project, cloned beside the projects
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return Old(root, root / "acme", tmp_path)


def _kept(old: Old) -> Path:
    """Where the one backup the conversion made of the old root holds its files (beside its `manifest.json`)."""
    stamps = [stamp for stamp in (backups.backup_root() / ".workspace-root").iterdir() if stamp.is_dir()]
    assert len(stamps) == 1, stamps
    assert (stamps[0] / backups.MANIFEST_NAME).is_file()
    return stamps[0] / backups.PAYLOAD_DIRNAME


def _tree(root: Path) -> List[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


# --- before converting: nothing changes ----------------------------------------------------------------------------


def test_before_converting_setup_leaves_the_workspace_as_it_is_and_says_so(old: Old) -> None:
    """A root project cloned into a workspace of submodules is used only once `convert` has run: until then setup
    keeps the workspace's own `workspace.toml` — refusing it would stop every setup half way through a migration."""
    before = (old.root / "workspace.toml").read_text()
    assert root_project.link(old.root) == [
        "acme is a root project: aisland workspace convert makes this workspace one around it"
    ]
    assert not (old.root / "workspace.toml").is_symlink()
    assert (old.root / "workspace.toml").read_text() == before


# --- each project: a clone holding its own repository ---------------------------------------------------------------


def test_every_submodule_becomes_a_clone_holding_its_own_repository(old: Old) -> None:
    models = old.root / "models"
    _git(models, "switch", "-q", "-c", "feature")
    _git(models, "commit", "-q", "--allow-empty", "-m", "work no remote has")
    (models / "pyproject.toml").write_text((models / "pyproject.toml").read_text() + "# edited\n")
    _git(models, "stash", "push", "-q", "-m", "set aside")
    (models / "notes.txt").write_text("uncommitted\n")
    convert.convert_workspace(old.root)
    assert (models / ".git").is_dir()
    assert _git(models, "rev-parse", "--show-toplevel") == str(models.resolve())
    assert _get(models, "config", "--local", "--get", "core.worktree") is None
    assert _git(models, "branch", "--show-current") == "feature"
    assert _git(models, "log", "-1", "--format=%s") == "work no remote has"
    assert "set aside" in _git(models, "stash", "list")
    assert _git(models, "status", "--porcelain") == "?? notes.txt"


def test_a_worktree_of_a_submodule_still_works_afterwards(old: Old) -> None:
    models = old.root / "models"
    worktree = old.tmp / "models-feature"
    _git(models, "worktree", "add", "-q", "-b", "feature", str(worktree))
    convert.convert_workspace(old.root)
    assert _git(worktree, "branch", "--show-current") == "feature"
    assert str(worktree.resolve()) in _git(models, "worktree", "list")


def test_this_machines_choices_move_into_the_root_project(old: Old) -> None:
    convert.convert_workspace(old.root)
    assert _git(old.project, "config", "--local", "--get", "aisland.active.tools.active") == "false"
    assert _git(old.project, "config", "--local", "--get", "aisland.editor.theme") == "Solarized Dark"
    assert _git(old.project, "status", "--porcelain") == ""


# --- the old root: into the backup store, never deleted -------------------------------------------------------------


def test_the_old_root_goes_into_the_backup_store_and_this_machines_files_stay(old: Old) -> None:
    lines = convert.convert_workspace(old.root)
    kept = _kept(old)
    assert not (old.root / ".git").exists()
    for name in (".gitmodules", "AGENTS.md", "TASKS.md", ".gitignore", "workspace.toml"):
        assert not (old.root / name).exists(), name
    assert (kept / ".git" / "HEAD").is_file()
    assert (kept / "TASKS.md").read_text() == "# tasks\n"
    assert (old.root / ".env").read_text() == "DATA_ROOT=/data\n"
    assert (old.root / ".venv").is_dir()
    assert (old.root / "models" / "pyproject.toml").is_file()
    assert (old.root / "acme" / "workspace.toml").is_file()
    assert f"tools: off on this machine; its repository is kept in {kept / '.git' / 'modules' / 'tools'}" in lines
    assert (kept / ".git" / "modules" / "tools" / "HEAD").is_file()


def test_after_converting_setup_finds_every_project_where_it_is(old: Old, capsys: pytest.CaptureFixture[str]) -> None:
    convert.convert_workspace(old.root)
    capsys.readouterr()
    root_project.link(old.root)
    workspace_file.refresh(old.root)
    activation.reconcile(old.root)
    assert "clone models" not in capsys.readouterr().out
    assert (old.root / "workspace.toml").is_symlink()
    assert _git(old.project, "status", "--porcelain") == ""  # every row was right: nothing to commit


# --- refused before anything moves ----------------------------------------------------------------------------------


@pytest.mark.parametrize("work", ["uncommitted", "unpushed"])
def test_work_no_remote_has_refuses_before_anything_moves(old: Old, work: str) -> None:
    if work == "uncommitted":
        (old.root / "TASKS.md").write_text("# tasks\n- not committed\n")
        why = "TASKS.md has changes no commit holds"
    else:
        _git(old.root, "commit", "-q", "--allow-empty", "-m", "local")
        why = "main has 1 commit origin lacks"
    before = _tree(old.root)
    with pytest.raises(AislandError) as refusal:
        convert.convert_workspace(old.root)
    assert refusal.value.format_message() == (
        f"not converted: the root repository holds work no remote has ({why}). "
        "Push it, or carry it into the root project, first."
    )
    assert _tree(old.root) == before


@pytest.mark.parametrize("state", ["not cloned", "naming another"])
def test_without_its_root_project_a_workspace_is_not_converted(old: Old, state: str) -> None:
    if state == "not cloned":
        shutil.rmtree(old.project)
    else:
        listed = old.project / "workspace.toml"
        listed.write_text(listed.read_text().replace('root = "acme"', 'root = "models"'))
    before = _tree(old.root)
    with pytest.raises(AislandError) as refusal:
        convert.convert_workspace(old.root)
    assert refusal.value.format_message() == (
        "not converted: no root project here. Clone it into this folder first: "
        "a clone whose workspace.toml names it as [workspace] root."
    )
    assert _tree(old.root) == before


def test_a_worktree_of_the_root_refuses_before_anything_moves(old: Old) -> None:
    """Its `.git` file points into the root's `.git`, which goes into the backup store."""
    worktree = old.tmp / "work-session"
    _git(old.root, "worktree", "add", "-q", "-b", "session", str(worktree))
    before = _tree(old.root)
    with pytest.raises(AislandError) as refusal:
        convert.convert_workspace(old.root)
    assert refusal.value.format_message() == (
        f"not converted: the root repository has worktrees of its own ({worktree.resolve()}): their .git files "
        "would point at nothing. Remove each with git worktree remove <path> first."
    )
    assert _tree(old.root) == before


def test_a_project_holding_submodules_of_its_own_refuses_before_anything_moves(old: Old) -> None:
    models = old.root / "models"
    inner = _remote(old.tmp, "inner", {"README.md": "inner\n"})
    _git(models, "submodule", "add", "-q", str(inner), "inner")
    _git(models, "commit", "-q", "-m", "inner")
    _git(old.root, "add", "models")
    _git(old.root, "commit", "-q", "-m", "models moved")
    _git(old.root, "push", "-q")
    before = _tree(old.root)
    with pytest.raises(AislandError) as refusal:
        convert.convert_workspace(old.root)
    assert refusal.value.format_message() == (
        "not converted: models holds submodules of its own, whose repositories would lose their way. "
        "Convert those by hand first."
    )
    assert _tree(old.root) == before


# --- the second machine, a second run, the command ------------------------------------------------------------------


def test_it_converts_after_a_pull_already_took_the_submodules_out(old: Old) -> None:
    """On the second machine the root may have pulled the commit that drops them: `.gitmodules` and the gitlinks are
    gone, while the repositories are still in `.git/modules`."""
    _git(old.root, "rm", "-q", "--cached", "models")
    _git(old.root, "rm", "-q", "--cached", "tools")
    _git(old.root, "rm", "-q", ".gitmodules")
    _git(old.root, "commit", "-q", "-m", "the projects are workspace.toml's")
    _git(old.root, "push", "-q")
    convert.convert_workspace(old.root)
    models = old.root / "models"
    assert (models / ".git").is_dir()
    assert _git(models, "log", "-1", "--format=%s") == "first"
    assert (_kept(old) / ".git" / "modules" / "tools" / "HEAD").is_file()


def test_a_second_run_changes_nothing(old: Old) -> None:
    convert.convert_workspace(old.root)
    before = _tree(old.root)
    assert convert.convert_workspace(old.root) == ["nothing to convert: the workspace has no root repository"]
    assert _tree(old.root) == before


def test_aisland_workspace_convert_converts_the_workspace(old: Old) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "convert"])
    assert result.exit_code == 0, result.output
    assert (old.root / "models" / ".git").is_dir()
    assert not (old.root / ".git").exists()
