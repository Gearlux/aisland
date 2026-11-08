"""``aisland setup`` adds a clone the workspace does not list as a submodule, by itself.

In a workspace of submodules, a repository cloned into the workspace folder by hand is invisible: discovery and
``workspace.toml`` read ``.gitmodules`` only, so it is neither listed nor installed. Setup finds each such CLONE — a
direct, non-hidden subfolder that is its own repository (a ``.git`` folder; a submodule's is a file), with an
``origin`` and a ``pyproject.toml``, which ``.gitmodules`` does not list and which is no link to another folder
(:func:`aisland.locations.is_alias`) — and adds it, never asking, with
``git submodule add -b <the branch it is on> <its origin> <folder>``. git keeps the clone as it is (its history,
checkout and uncommitted edits); ``-f`` is passed only in a local root, whose ``.gitignore`` is ``/*``.

The new project is tracked as the workspace says (:mod:`aisland.workspace.tracking`): untracked when ``workspace.toml`` has
``[workspace] track = false`` (``init --untracked``, ``aisland workspace track unset``) or this machine untracks every
project, tracked otherwise. So in such a workspace a clone needs only ``aisland setup``: it becomes a submodule, is
listed in ``workspace.toml``, untracked, in the editor's workspace file, and committed in a local root. A workspace without ``.gitmodules``
that is no local root is a folder of clones, where each clone already is a project: it has none to add.

``aisland workspace init`` adds them too when it makes the root itself (``git init`` in a folder that already held
clones): :func:`found` is the same search without that exception, and :func:`add` adds what it found.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from aisland import gitcmd, locations
from aisland.workspace import file as workspace_file
from aisland.workspace import local as local_workspace
from aisland.workspace import tracking


@dataclass(frozen=True)
class Clone:
    """A clone the workspace does not list: its folder, its ``origin`` and the branch it is on (None: on none)."""

    path: str
    url: str
    branch: Optional[str]


def unlisted(root: Path) -> List[Clone]:
    """Every clone in ``root`` that ``.gitmodules`` does not list, by folder name (the module docstring); none in a
    folder of clones (no ``.gitmodules``, no local root)."""
    if not (root / ".gitmodules").is_file() and not local_workspace.is_local(root):
        return []
    return found(root)


def found(root: Path) -> List[Clone]:
    """Every clone in ``root`` that ``.gitmodules`` does not list, by folder name, whether or not ``root`` has a
    ``.gitmodules``: what ``init`` adds in the root it has just made."""
    listed = set(local_workspace.submodule_names(root))
    found = []
    for child in sorted(root.iterdir(), key=lambda c: c.name):
        if child.name.startswith(".") or child.name in listed or not child.is_dir():
            continue
        if locations.is_alias(child, root) or not (child / ".git").is_dir() or not (child / "pyproject.toml").is_file():
            continue
        url = gitcmd.remote_url(child)
        if url:
            found.append(Clone(child.name, url, gitcmd.read(child, "branch", "--show-current") or None))
    return found


def add_unlisted(root: Path) -> List[str]:
    """Add each clone :func:`unlisted` finds as a submodule, without asking; one line per thing done."""
    return add(root, unlisted(root))


def add(root: Path, clones: Sequence[Clone]) -> List[str]:
    """Add each of ``clones`` as a submodule; one line per thing done.

    A clone git refuses to add is said and the others still added; ``workspace.toml`` lists each one added."""
    lines: List[str] = []
    force = ["-f"] if local_workspace.is_local(root) else []
    for clone in clones:
        branch = ["-b", clone.branch] if clone.branch else []
        # A clone when the folder has none yet: no prompt (git's output is captured) and no time limit.
        added = gitcmd.git(
            root,
            "submodule",
            "add",
            "-q",
            *force,
            *branch,
            "--",
            clone.url,
            clone.path,
            network=True,
            timeout=gitcmd.NO_LIMIT,
        )
        if added.returncode != 0:
            lines.append(f"could not add {clone.path}: {(added.stderr.strip().splitlines() or ['failed'])[-1]}")
            continue
        lines.append(f"added {clone.path} as a submodule ({clone.url}, {clone.branch or 'on no branch'})")
        workspace_file.refresh(root)
        project = next((p.name for p in workspace_file.load(root).projects.values() if p.path == clone.path), None)
        if project is None:  # the file lists it under another spelling of its path: tracking follows on setup's run
            continue
        # Tracked as the workspace says: `[workspace] track = false` (or this machine's choice) untracks it.
        lines += [change.line() for change in tracking.apply(root) if change.project == project]
    return lines
