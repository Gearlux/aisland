"""Workspace worktrees: a session started at a workspace with a root project works in a worktree of the whole
workspace (``aisland workspace worktree``).

Claude Code makes a session's worktree with ``git worktree`` of the folder it starts in. At a workspace with a root
project that folder is a repository recording no project (:mod:`aisland.setup.init`), so a worktree of it would hold
no project's code; the workspace's ``WorktreeCreate`` hook (:mod:`aisland.setup.claude_hooks`) runs :func:`create`
instead. It makes ``<workspace>-worktrees/<name>/`` beside the workspace — outside every repository, as Claude Code
requires of a folder a hook makes — holding a git worktree of each project this machine has, the root project's
included, each on a branch ``dev/<name>`` of its own from the clone's commit. Beside them are the files a session
reads at the root: ``workspace.toml`` and the root project's files from the worktree's own root project (setup's
harness step, run there), so an edit lands on its branch, and the workspace's venv, ``.env``, MCP servers, shell file,
rules and Claude Code settings, linked as they are.

:func:`remove` takes a worktree away once no project holds uncommitted work and nothing outside the projects was left
there, deleting each branch that got no commit and keeping the others, named. :func:`session_env` is the
``PYTHONPATH`` line a session in a worktree needs: the shared venv imports the main checkouts, so the worktree's
projects go first.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from aisland import atomic, console, gitcmd, locations
from aisland.errors import AislandError
from aisland.setup.harness import AGENTS, install_harness
from aisland.workspace import root_project
from aisland.workspace.model import Workspace

#: The folder beside the workspace that holds its worktrees: ``<workspace folder>-worktrees``.
SUFFIX = "-worktrees"
#: The file that makes a folder a workspace worktree, and says what it holds.
MARKER = ".aisland-worktree.json"
#: The version of :data:`MARKER`'s shape.
FORMAT = 1
#: Each project's branch in a worktree: ``dev/<name>`` — every commit of the workspace goes to a ``dev/*`` branch.
BRANCH_PREFIX = "dev/"
#: The workspace's own files a session at its root reads, linked into each worktree as they are.
SHARED = (
    ".venv",
    ".env",
    ".mcp.json",
    "project.bashrc",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".claude/settings.json",
)
#: What a worktree holds beside its projects that is aisland's: the marker, the harness's folders and its ignore list.
_MADE = (MARKER, AGENTS, ".claude", ".gitignore", "AGENTS.md")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class Worktree:
    """A workspace worktree: its ``name``, its ``folder``, the ``workspace`` it was made from, and each project's path
    with the commit its branch started at."""

    name: str
    folder: Path
    workspace: Path
    projects: Dict[str, str] = field(default_factory=dict)

    @property
    def branch(self) -> str:
        return BRANCH_PREFIX + self.name


def location(root: Path) -> Path:
    """The folder that holds the worktrees of the workspace at ``root``: beside it, outside its repository."""
    return root.parent / f"{root.name}{SUFFIX}"


def _projects(root: Path) -> List[str]:
    """The folders a worktree takes: each project of ``workspace.toml`` this machine has a clone of and installs
    (:attr:`~aisland.workspace.model.Workspace.left_out` names the others), and the root project."""
    workspace = Workspace(root)
    out = {folder.resolve() for folder in workspace.left_out}
    paths = [
        row.path
        for row in workspace.file.projects.values()
        if (root / row.path / ".git").exists() and (root / row.path).resolve() not in out
    ]
    own = root_project.find(root)
    if own is not None and own.name not in paths:
        paths.append(own.name)
    return sorted(paths)


def _link_shared(root: Path, folder: Path) -> None:
    for name in SHARED:
        if os.path.lexists(root / name):
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(root / name, folder / name)


def _write_marker(worktree: Worktree) -> None:
    data = {
        "format": FORMAT,
        "name": worktree.name,
        "workspace": str(worktree.workspace),
        "projects": worktree.projects,
    }
    atomic.write_text(worktree.folder / MARKER, json.dumps(data, indent=2) + "\n")


def _undo(root: Path, folder: Path, made: Dict[str, str], branch: str) -> None:
    """Take away what a :func:`create` that failed half way made: its worktrees, their branches and the folder."""
    for path in made:
        gitcmd.git(root / path, "worktree", "remove", "--force", str(folder / path))
        gitcmd.git(root / path, "branch", "-D", branch)
    shutil.rmtree(folder, ignore_errors=True)


def create(root: Path, name: str) -> Worktree:
    """Make the worktree ``name`` of the workspace at ``root`` (the module docstring); every refusal comes first."""
    if not locations.rooted(root):
        raise AislandError(f"{root} has no root project: a workspace worktree spans the clones of a workspace with one")
    if not _NAME.fullmatch(name):
        raise AislandError(
            f"{name!r} is no worktree name: letters, digits, '.', '_' and '-', starting with a letter or digit"
        )
    folder = location(root) / name
    if os.path.lexists(folder):
        raise AislandError(
            f"{folder} exists already: pick another name, or remove it with aisland workspace worktree remove {folder}"
        )
    projects = _projects(root)
    branch = BRANCH_PREFIX + name
    taken = [
        path
        for path in projects
        if gitcmd.git(root / path, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0
    ]
    if taken:
        raise AislandError(f"{branch} exists already in {', '.join(taken)}: pick another name")
    folder.mkdir(parents=True)
    made: Dict[str, str] = {}
    try:
        for path in projects:
            base = gitcmd.git(root / path, "rev-parse", "HEAD", check=True).stdout.strip()
            gitcmd.git(root / path, "worktree", "add", "-q", "-b", branch, str(folder / path), base, check=True)
            made[path] = base
            console.echo(f"  - {path}: a worktree on {branch}", err=True)
        _link_shared(root, folder)
        root_project.link(folder)
        for line in install_harness(folder):
            console.echo(f"  - {line}", err=True)
        worktree = Worktree(name, folder, root, made)
        _write_marker(worktree)
    except (AislandError, OSError) as exc:
        _undo(root, folder, made, branch)
        raise AislandError(f"no worktree made, and what was made is taken away again: {exc}") from exc
    return worktree


def read(folder: Path) -> Optional[Worktree]:
    """The worktree at ``folder``, or None for a folder holding no :data:`MARKER` aisland can read."""
    try:
        data: object = json.loads((folder / MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    name, workspace, projects = data.get("name"), data.get("workspace"), data.get("projects")
    if not isinstance(name, str) or not isinstance(workspace, str) or not isinstance(projects, dict):
        return None
    return Worktree(name, folder, Path(workspace), {str(path): str(base) for path, base in projects.items()})


def find(path: Path) -> Optional[Worktree]:
    """The worktree ``path`` is in — the folder itself or a folder below it —, or None."""
    for candidate in (path, *path.parents):
        found = read(candidate)
        if found is not None:
            return found
    return None


def listed(root: Path) -> List[Worktree]:
    """The worktrees of the workspace at ``root``, by name."""
    try:
        children = sorted(location(root).iterdir())
    except OSError:
        return []
    found = [read(child) for child in children if child.is_dir()]
    return [worktree for worktree in found if worktree is not None and worktree.workspace.resolve() == root.resolve()]


def changed(worktree: Worktree) -> Dict[str, int]:
    """Each project of ``worktree`` holding uncommitted work, with the number of files git names."""
    out: Dict[str, int] = {}
    for path in worktree.projects:
        if (worktree.folder / path).is_dir():
            lines = gitcmd.git(worktree.folder / path, "status", "--porcelain").stdout.splitlines()
            if lines:
                out[path] = len(lines)
    return out


def commits(worktree: Worktree) -> Dict[str, int]:
    """Each project of ``worktree`` whose branch got commits since the worktree was made, with their number."""
    out: Dict[str, int] = {}
    for path, base in worktree.projects.items():
        run = gitcmd.git(worktree.workspace / path, "rev-list", "--count", f"{base}..refs/heads/{worktree.branch}")
        count = int(run.stdout.strip()) if run.returncode == 0 and run.stdout.strip().isdigit() else 0
        if count:
            out[path] = count
    return out


def _strays(worktree: Worktree) -> List[str]:
    """What the worktree's folder holds beside its projects that aisland did not put there: a session's file."""
    projects = {path.split("/", 1)[0] for path in worktree.projects}
    return sorted(
        child.name
        for child in worktree.folder.iterdir()
        if child.name not in projects and child.name not in _MADE and not child.is_symlink()
    )


def _them(names: List[str]) -> str:
    return "it" if len(names) == 1 else "them"


def remove(folder: Path) -> List[str]:
    """Take the worktree at ``folder`` away (the module docstring); the lines naming each branch kept."""
    worktree = read(folder)
    if worktree is None:
        raise AislandError(f"{folder} is no workspace worktree: it holds no {MARKER}")
    dirty = changed(worktree)
    if dirty:
        what = ", ".join(f"{path} ({count} file{'' if count == 1 else 's'})" for path, count in dirty.items())
        raise AislandError(
            f"{folder} is kept: uncommitted work in {what} — commit or discard it, then remove the worktree again"
        )
    strays = _strays(worktree)
    if strays:
        verb = "belongs" if len(strays) == 1 else "belong"
        raise AislandError(
            f"{folder} is kept: {', '.join(strays)} {verb} to no project — move {_them(strays)} into one or delete "
            f"{_them(strays)}, then remove the worktree again"
        )
    counts = commits(worktree)
    lines: List[str] = []
    for path in worktree.projects:
        repo = worktree.workspace / path
        if (folder / path).exists():
            gitcmd.git(repo, "worktree", "remove", str(folder / path), check=True)
        else:
            gitcmd.git(repo, "worktree", "prune")
        count = counts.get(path, 0)
        if count:
            lines.append(
                f"kept {worktree.branch} in {path}: {count} commit{'' if count == 1 else 's'} since the worktree was made"
            )
        else:
            gitcmd.git(repo, "branch", "-D", worktree.branch)  # no commit of its own: nothing is lost
    shutil.rmtree(folder)
    return lines


def python_path(worktree: Worktree) -> List[Path]:
    """The worktree's project folders a Python import resolves in — each holding a ``pyproject.toml``, as the editor's
    ``extraPaths`` list them (:func:`aisland.setup.editor_files.write_pyrightconfig`)."""
    return [
        worktree.folder / path for path in worktree.projects if (worktree.folder / path / "pyproject.toml").is_file()
    ]


def session_env(cwd: Path) -> Optional[str]:
    """The shell line that puts the worktree's projects first on ``PYTHONPATH`` for a session in ``cwd``, or None
    outside every worktree."""
    worktree = find(cwd)
    paths = python_path(worktree) if worktree is not None else []
    if not paths:
        return None
    joined = os.pathsep.join(str(path) for path in paths)
    return f'export PYTHONPATH="{joined}${{PYTHONPATH:+:$PYTHONPATH}}"'
