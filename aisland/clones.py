"""``aisland setup`` adds a clone the workspace does not list as a submodule, after asking (user choice 2026-10-23).

In a workspace of submodules, a repository cloned into the workspace folder by hand is invisible: discovery and
``workspace.toml`` read ``.gitmodules`` only, so it is neither listed nor installed. Setup finds each such CLONE — a
direct, non-hidden subfolder that is its own repository (a ``.git`` folder; a submodule's is a file), with an
``origin`` and a ``pyproject.toml``, which ``.gitmodules`` does not list and which is no link to another folder
(:func:`aisland.locations.is_alias`) — asks about it (``-y``: added without asking) and adds it with
``git submodule add -b <the branch it is on> <its origin> <folder>``. git keeps the clone as it is (its history,
checkout and uncommitted edits); ``-f`` is passed only in a local root, whose ``.gitignore`` is ``/*``.

The new project is tracked like the others (:mod:`aisland.tracking`): untracked for everyone when every other project
is, untracked on this machine when every other one is so here, tracked otherwise. A workspace without ``.gitmodules``
that is no local root is a folder of clones, where each clone already is a project: it has none to add.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import click

from aisland import local_workspace, locations, tracking, workspace_file


@dataclass(frozen=True)
class Clone:
    """A clone the workspace does not list: its folder, its ``origin`` and the branch it is on (None: on none)."""

    path: str
    url: str
    branch: Optional[str]


def _out(folder: Path, *args: str) -> Optional[str]:
    run = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else None


def unlisted(root: Path) -> List[Clone]:
    """Every clone in ``root`` that ``.gitmodules`` does not list, by folder name (the module docstring)."""
    if not (root / ".gitmodules").is_file() and not local_workspace.is_local(root):
        return []
    listed = set(local_workspace.submodule_names(root))
    found = []
    for child in sorted(root.iterdir(), key=lambda c: c.name):
        if child.name.startswith(".") or child.name in listed or not child.is_dir():
            continue
        if locations.is_alias(child, root) or not (child / ".git").is_dir() or not (child / "pyproject.toml").is_file():
            continue
        url = _out(child, "remote", "get-url", "origin")
        if url:
            found.append(Clone(child.name, url, _out(child, "branch", "--show-current") or None))
    return found


def _like_the_others(root: Path, project: str) -> List[str]:
    """Untrack ``project`` when every other project is untracked — for everyone, or on this machine only."""
    others = [name for name in tracking.submodule_projects(root) if name != project]
    if not others:
        return []
    if all(not tracking.committed(root, name) for name in others):
        changes = tracking.set_committed(root, (project,), track=False)
    elif all(tracking.machine_choice(root, name) is False for name in others):
        changes = tracking.set_machine(root, (project,), track=False)
    else:
        return []
    return [change.line() for change in changes if change.project == project]


def add_unlisted(root: Path, non_interactive: bool) -> List[str]:
    """Ask about each clone :func:`unlisted` finds and add the ones wanted as submodules; one line per thing done.

    A clone git refuses to add is said and the others still added; ``workspace.toml`` lists each one added."""
    lines: List[str] = []
    force = ["-f"] if local_workspace.is_local(root) else []
    for clone in unlisted(root):
        where = f"on {clone.branch}" if clone.branch else "on no branch"
        if not non_interactive and not click.confirm(
            f"  - {clone.path} is a clone the workspace does not list ({clone.url}, {where}). Add it as a submodule?",
            default=True,
        ):
            continue
        branch = ["-b", clone.branch] if clone.branch else []
        added = subprocess.run(
            ["git", "submodule", "add", "-q", *force, *branch, clone.url, clone.path],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if added.returncode != 0:
            lines.append(f"could not add {clone.path}: {(added.stderr.strip().splitlines() or ['failed'])[-1]}")
            continue
        lines.append(f"added {clone.path} as a submodule ({clone.url}, {clone.branch or 'on no branch'})")
        workspace_file.refresh(root)
        project = next(p.name for p in workspace_file.load(root).projects.values() if p.path == clone.path)
        lines += _like_the_others(root, project)
    return lines
