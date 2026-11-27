"""A session started at a workspace with a root project works in a worktree of the whole workspace.

Claude Code makes a session's worktree with `git worktree` of the folder it starts in; at such a workspace that folder
is a repository recording no project, so a worktree of it would hold no project's code. Its `WorktreeCreate` hook
replaces that: `aisland workspace worktree create` makes `<workspace>-worktrees/<name>/` beside the workspace — outside
every repository, as Claude Code requires of a folder a hook makes — holding a git worktree of each project this
machine has, each on `dev/<name>`, with the files a session reads at the root. `remove` takes it away again once no
project holds uncommitted work, keeping each branch that got commits; `env` puts the worktree's projects first on a
session's `PYTHONPATH`, since the shared venv imports the main checkouts. Setup writes the three hook entries into the
root's `.claude/settings.json`, keeping every other entry there.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from aisland import cli, machine_config, venv
from aisland.errors import AislandError
from aisland.setup import claude_hooks, worktrees
from aisland.workspace import activation, root_project
from tests.test_root_project import Rooted, _get, _git, _set_up, builtin_set, isolated_git, rooted

__all__ = ["builtin_set", "isolated_git", "rooted"]  # the fixtures this module takes from the root project's tests


@pytest.fixture
def workspace(rooted: Rooted, builtin_set: None) -> Rooted:
    """After setup and `aisland workspace init`: `acme` (the root project) and `models` cloned, `tools` off, the
    folder a repository that records no project, a `.venv` and a `.env` at the root."""
    _set_up(rooted)
    (rooted.root / ".venv").mkdir()
    (rooted.root / ".env").write_text("DATA_ROOT=/data\n")
    result = CliRunner().invoke(cli.main, ["workspace", "init", str(rooted.root)])
    assert result.exit_code == 0, result.output
    return rooted


def _folder(workspace: Rooted, name: str = "bold-oak") -> Path:
    return workspace.root.parent / "work-worktrees" / name


def _branch(repo: Path, name: str = "bold-oak") -> str | None:
    return _get(repo, "rev-parse", "--verify", "-q", f"refs/heads/dev/{name}")


# --- create ---------------------------------------------------------------------------------------------------------


def test_create_makes_a_worktree_of_every_project_beside_the_workspace(workspace: Rooted) -> None:
    made = worktrees.create(workspace.root, "bold-oak")
    assert made.folder == _folder(workspace)
    assert sorted(made.projects) == ["acme", "models"]
    for project in ("acme", "models"):
        assert _git(made.folder / project, "branch", "--show-current") == "dev/bold-oak"
        assert _git(made.folder / project, "rev-parse", "HEAD") == _git(workspace.root / project, "rev-parse", "HEAD")
    assert _git(workspace.root / "models", "branch", "--show-current") == "dev/main"  # the checkout is left as it was


def test_the_worktree_holds_the_files_a_session_reads_at_the_root(workspace: Rooted) -> None:
    """Its `workspace.toml` and the root project's files are the worktree's own root project's, so an edit lands on
    its branch; the venv and `.env` are the workspace's."""
    folder = worktrees.create(workspace.root, "bold-oak").folder
    assert os.readlink(folder / "workspace.toml") == "acme/workspace.toml"
    assert (folder / "TASKS.md").resolve() == (folder / "acme" / "TASKS.md").resolve()
    assert (folder / ".venv").resolve() == (workspace.root / ".venv").resolve()
    assert (folder / ".env").resolve() == (workspace.root / ".env").resolve()
    assert root_project.rooted(folder)


def test_a_project_switched_off_here_is_left_out(workspace: Rooted) -> None:
    machine_config.put(workspace.root, "aisland.active.tools.active", True)
    activation.reconcile(workspace.root)
    machine_config.put(workspace.root, "aisland.active.tools.active", False)
    assert (workspace.root / "tools" / ".git").exists()  # an off project keeps its clone
    assert "tools" not in worktrees.create(workspace.root, "bold-oak").projects


def test_the_create_hook_prints_the_folder_as_its_one_line_of_output(workspace: Rooted) -> None:
    """Claude Code reads the folder from the last line of stdout; the rest goes to stderr."""
    hook = {"session_id": "s", "cwd": str(workspace.root), "hook_event_name": "WorktreeCreate", "name": "bold-oak"}
    result = CliRunner().invoke(cli.main, ["workspace", "worktree", "create", "--hook"], input=json.dumps(hook))
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [str(_folder(workspace))]


@pytest.mark.parametrize("name", ["", "../up", "a/b", ".hidden", "-x"])
def test_a_name_that_is_no_plain_folder_name_is_refused(workspace: Rooted, name: str) -> None:
    with pytest.raises(AislandError) as refusal:
        worktrees.create(workspace.root, name)
    assert refusal.value.format_message() == (
        f"{name!r} is no worktree name: letters, digits, '.', '_' and '-', starting with a letter or digit"
    )


def test_a_name_in_use_is_refused(workspace: Rooted) -> None:
    worktrees.create(workspace.root, "bold-oak")
    with pytest.raises(AislandError) as refusal:
        worktrees.create(workspace.root, "bold-oak")
    assert refusal.value.format_message() == (
        f"{_folder(workspace)} exists already: pick another name, or remove it with "
        f"aisland workspace worktree remove {_folder(workspace)}"
    )


def test_a_branch_a_project_has_already_is_refused_before_anything_is_made(workspace: Rooted) -> None:
    _git(workspace.root / "models", "branch", "dev/bold-oak")
    with pytest.raises(AislandError) as refusal:
        worktrees.create(workspace.root, "bold-oak")
    assert refusal.value.format_message() == "dev/bold-oak exists already in models: pick another name"
    assert not _folder(workspace).exists()
    assert _branch(workspace.root / "acme") is None


def test_a_workspace_without_a_root_project_has_no_workspace_worktree(tmp_path: Path) -> None:
    with pytest.raises(AislandError) as refusal:
        worktrees.create(tmp_path, "bold-oak")
    assert refusal.value.format_message() == (
        f"{tmp_path} has no root project: a workspace worktree spans the clones of a workspace with one"
    )


# --- the session's environment ---------------------------------------------------------------------------------------


def test_a_session_in_the_worktree_gets_its_projects_first_on_the_python_path(
    workspace: Rooted, tmp_path: Path
) -> None:
    """The shared venv imports the main checkouts; the worktree's projects (those with a `pyproject.toml`) go first."""
    folder = worktrees.create(workspace.root, "bold-oak").folder
    env_file = tmp_path / "session.env"
    hook = {"cwd": str(folder / "models"), "hook_event_name": "SessionStart", "source": "startup"}
    result = CliRunner(env={"CLAUDE_ENV_FILE": str(env_file)}).invoke(
        cli.main, ["workspace", "worktree", "env", "--hook"], input=json.dumps(hook)
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == ""  # a SessionStart hook's stdout reaches the session as context
    assert env_file.read_text() == f'export PYTHONPATH="{folder / "models"}${{PYTHONPATH:+:$PYTHONPATH}}"\n'


def test_a_session_outside_every_worktree_gets_nothing(workspace: Rooted, tmp_path: Path) -> None:
    env_file = tmp_path / "session.env"
    hook = {"cwd": str(workspace.root), "hook_event_name": "SessionStart", "source": "startup"}
    result = CliRunner(env={"CLAUDE_ENV_FILE": str(env_file)}).invoke(
        cli.main, ["workspace", "worktree", "env", "--hook"], input=json.dumps(hook)
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert not env_file.exists()


# --- remove and list -------------------------------------------------------------------------------------------------


def test_remove_takes_every_project_worktree_and_the_folder_away(workspace: Rooted) -> None:
    folder = worktrees.create(workspace.root, "bold-oak").folder
    assert worktrees.remove(folder) == []
    assert not folder.exists()
    for project in ("acme", "models"):
        assert _branch(workspace.root / project) is None
        assert str(folder / project) not in _git(workspace.root / project, "worktree", "list")
    assert (workspace.root / ".venv").is_dir()  # the links went, never what they point at
    assert (workspace.root / ".env").read_text() == "DATA_ROOT=/data\n"


def test_remove_keeps_a_branch_that_got_commits_and_says_so(workspace: Rooted) -> None:
    folder = worktrees.create(workspace.root, "bold-oak").folder
    (folder / "models" / "new.py").write_text("x = 1\n")
    _git(folder / "models", "add", "new.py")
    _git(folder / "models", "commit", "-q", "-m", "new")
    assert worktrees.remove(folder) == ["kept dev/bold-oak in models: 1 commit since the worktree was made"]
    assert _branch(workspace.root / "models") is not None
    assert _branch(workspace.root / "acme") is None


def test_remove_refuses_uncommitted_work_and_removes_nothing(workspace: Rooted) -> None:
    folder = worktrees.create(workspace.root, "bold-oak").folder
    (folder / "models" / "new.py").write_text("x = 1\n")
    with pytest.raises(AislandError) as refusal:
        worktrees.remove(folder)
    assert refusal.value.format_message() == (
        f"{folder} is kept: uncommitted work in models (1 file) — commit or discard it, then remove the worktree again"
    )
    assert (folder / "acme").is_dir() and _branch(workspace.root / "acme") is not None


def test_remove_refuses_a_file_that_belongs_to_no_project(workspace: Rooted) -> None:
    folder = worktrees.create(workspace.root, "bold-oak").folder
    (folder / "notes.txt").write_text("a session's note\n")
    with pytest.raises(AislandError) as refusal:
        worktrees.remove(folder)
    assert refusal.value.format_message() == (
        f"{folder} is kept: notes.txt belongs to no project — move it into one or delete it, then remove the worktree "
        "again"
    )
    assert (folder / "notes.txt").exists()


def test_the_remove_hook_reads_the_folder_from_its_json(workspace: Rooted) -> None:
    folder = worktrees.create(workspace.root, "bold-oak").folder
    hook = {"hook_event_name": "WorktreeRemove", "worktree_path": str(folder)}
    result = CliRunner().invoke(cli.main, ["workspace", "worktree", "remove", "--hook"], input=json.dumps(hook))
    assert result.exit_code == 0, result.output
    assert not folder.exists()


def test_a_folder_that_is_no_workspace_worktree_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AislandError) as refusal:
        worktrees.remove(tmp_path)
    assert refusal.value.format_message() == f"{tmp_path} is no workspace worktree: it holds no .aisland-worktree.json"


def test_list_names_each_worktree_with_its_changes_and_commits(workspace: Rooted) -> None:
    first = worktrees.create(workspace.root, "bold-oak").folder
    second = worktrees.create(workspace.root, "calm-fir").folder
    (first / "models" / "new.py").write_text("x = 1\n")
    _git(second / "acme", "commit", "-q", "--allow-empty", "-m", "a note")
    result = CliRunner().invoke(cli.main, ["workspace", "worktree", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "worktrees": [
            {"name": "bold-oak", "folder": str(first), "branch": "dev/bold-oak", "changed": ["models"], "commits": {}},
            {
                "name": "calm-fir",
                "folder": str(second),
                "branch": "dev/calm-fir",
                "changed": [],
                "commits": {"acme": 1},
            },
        ]
    }


# --- the hook entries setup writes -----------------------------------------------------------------------------------


def _hooks(root: Path) -> dict[str, list[dict[str, object]]]:
    hooks: dict[str, list[dict[str, object]]] = json.loads((root / ".claude" / "settings.json").read_text())["hooks"]
    return hooks


def _entry(root: Path, verb: str) -> dict[str, object]:
    command = f'"{venv.venv_bin(root) / "aisland"}" workspace worktree {verb} --hook'
    return {"hooks": [{"type": "command", "command": command}]}


def test_setup_writes_the_three_worktree_hooks_into_the_roots_claude_settings(workspace: Rooted) -> None:
    assert claude_hooks.write_hooks(workspace.root) == [".claude/settings.json: the worktree hooks"]
    assert _hooks(workspace.root) == {
        "WorktreeCreate": [_entry(workspace.root, "create")],
        "WorktreeRemove": [_entry(workspace.root, "remove")],
        "SessionStart": [_entry(workspace.root, "env")],
    }
    assert claude_hooks.write_hooks(workspace.root) == []  # nothing to change, nothing said


def test_a_worktree_shares_the_roots_claude_settings(workspace: Rooted) -> None:
    """A CLI session reads the worktree's project settings, a desktop one the root's: both find the same hooks."""
    claude_hooks.write_hooks(workspace.root)
    folder = worktrees.create(workspace.root, "bold-oak").folder
    assert (folder / ".claude" / "settings.json").resolve() == (workspace.root / ".claude" / "settings.json").resolve()


def test_the_persons_own_settings_and_hooks_are_kept(workspace: Rooted) -> None:
    settings = workspace.root / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    own = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo hello"}]}
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"SessionStart": [own]}}))
    claude_hooks.write_hooks(workspace.root)
    assert json.loads(settings.read_text())["permissions"] == {"allow": ["Bash(ls)"]}
    assert _hooks(workspace.root)["SessionStart"] == [own, _entry(workspace.root, "env")]


def test_an_older_copy_of_aislands_hooks_is_replaced_not_doubled(workspace: Rooted) -> None:
    settings = workspace.root / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    old = {"hooks": [{"type": "command", "command": '"/old/venv/bin/aisland" workspace worktree create --hook'}]}
    settings.write_text(json.dumps({"hooks": {"WorktreeCreate": [old]}}))
    claude_hooks.write_hooks(workspace.root)
    assert _hooks(workspace.root)["WorktreeCreate"] == [_entry(workspace.root, "create")]


def test_a_workspace_without_a_root_project_loses_aislands_hooks_and_keeps_the_persons(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    own = {"hooks": [{"type": "command", "command": "echo hello"}]}
    ours = _entry(tmp_path, "env")
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [own, ours], "WorktreeCreate": [_entry(tmp_path, "create")]}})
    )
    assert claude_hooks.write_hooks(tmp_path) == [
        ".claude/settings.json: the worktree hooks taken away (no root project)"
    ]
    assert json.loads(settings.read_text()) == {"hooks": {"SessionStart": [own]}}


def test_a_settings_file_aisland_cannot_read_is_left_and_named(workspace: Rooted) -> None:
    settings = workspace.root / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text("{not json")
    assert claude_hooks.write_hooks(workspace.root) == [
        ".claude/settings.json is no JSON aisland can read: the worktree hooks are not written there"
    ]
    assert settings.read_text() == "{not json"
