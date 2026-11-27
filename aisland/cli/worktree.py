"""`aisland workspace worktree …`: a worktree of the whole workspace for a session started at its root
(:mod:`aisland.setup.worktrees`). Each verb Claude Code runs as a hook takes `--hook`, which reads the hook's JSON on
stdin (:mod:`aisland.setup.claude_hooks` writes the entries).
"""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import click

from aisland.cli._base import Group
from aisland.errors import AislandError
from aisland.setup import worktrees
from aisland.workspace.projects import get_workspace_root


def _hook_input() -> Dict[str, object]:
    """The JSON object Claude Code hands a hook on stdin."""
    text = click.get_text_stream("stdin").read()
    try:
        data: object = json.loads(text or "{}")
    except ValueError as exc:
        raise AislandError(f"the hook's input is no JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AislandError("the hook's input is no JSON object")
    return {str(key): value for key, value in data.items()}


def _field(data: Dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise AislandError(f"the hook's input names no {key}")
    return value


@click.group("worktree", cls=Group)
def worktree_group() -> None:
    """A worktree of the whole workspace for a session started at its root: one git worktree of each project, beside
    the workspace, each on its own dev/NAME branch."""


@worktree_group.command("create")
@click.argument("name", required=False)
@click.option("--hook", is_flag=True, help="Read the name from Claude Code's WorktreeCreate hook input on stdin.")
def worktree_create(name: Optional[str], hook: bool) -> None:
    """Make <workspace>-worktrees/NAME beside the workspace — each project a git worktree on dev/NAME — and print its
    folder (the progress goes to stderr)."""
    if hook:
        name = _field(_hook_input(), "name")
    if not name:
        raise click.UsageError("name the worktree, or pass --hook")
    click.echo(str(worktrees.create(get_workspace_root(), name).folder))


@worktree_group.command("remove")
@click.argument("folder", required=False, type=click.Path(path_type=Path))
@click.option("--hook", is_flag=True, help="Read the folder from Claude Code's WorktreeRemove hook input on stdin.")
def worktree_remove(folder: Optional[Path], hook: bool) -> None:
    """Take the worktree at FOLDER away: refused while a project holds uncommitted work; a branch that got commits is
    kept, and named."""
    if hook:
        folder = Path(_field(_hook_input(), "worktree_path"))
    if folder is None:
        raise click.UsageError("name the worktree's folder, or pass --hook")
    for line in worktrees.remove(folder.absolute()):
        click.echo(line)


@worktree_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print the worktrees as one JSON object.")
def worktree_list(as_json: bool) -> None:
    """The workspace's worktrees: each one's folder, the projects holding uncommitted work, the branches with commits."""
    rows: List[Dict[str, object]] = []
    for worktree in worktrees.listed(get_workspace_root()):
        rows.append(
            {
                "name": worktree.name,
                "folder": str(worktree.folder),
                "branch": worktree.branch,
                "changed": sorted(worktrees.changed(worktree)),
                "commits": worktrees.commits(worktree),
            }
        )
    if as_json:
        click.echo(json.dumps({"worktrees": rows}, indent=2))
        return
    if not rows:
        click.echo("no worktree")
    for row in rows:
        changed = ", ".join(str(name) for name in row["changed"]) if isinstance(row["changed"], list) else ""
        commits = row["commits"] if isinstance(row["commits"], dict) else {}
        made = ", ".join(f"{path} ({count})" for path, count in commits.items())
        click.echo(f"{row['name']}  {row['folder']}")
        click.echo(f"    uncommitted work: {changed or 'none'}")
        click.echo(f"    commits on {row['branch']}: {made or 'none'}")


@worktree_group.command("env")
@click.argument("folder", required=False, type=click.Path(path_type=Path))
@click.option(
    "--hook",
    is_flag=True,
    help="Read the session's folder from Claude Code's SessionStart hook input on stdin, and append the line to the "
    "file $CLAUDE_ENV_FILE names.",
)
def worktree_env(folder: Optional[Path], hook: bool) -> None:
    """The shell line a session in a worktree needs: the worktree's projects first on PYTHONPATH, since the shared venv
    imports the main checkouts. Nothing outside a worktree."""
    if hook:
        cwd = _hook_input().get("cwd")
        line = worktrees.session_env(Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd())
        target = os.environ.get("CLAUDE_ENV_FILE")
        if line and target:
            with Path(target).open("a", encoding="utf-8") as out:
                out.write(line + "\n")
        return
    line = worktrees.session_env((folder or Path.cwd()).absolute())
    if line:
        click.echo(line)
