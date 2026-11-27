"""A workspace's root project: the clone that holds the workspace's own files.

A workspace with a root project has no root repository. Its folder holds plain clones, side by side, and one of them is
the root project: its ``workspace.toml`` names it (``[workspace] root``) and lists every project, itself included.
Nothing records which commit of each project goes with which: each clone follows its branch.

aisland reads a workspace's ``workspace.toml`` at the workspace folder, as in every workspace. Here that file is a link
to the root project's, which :func:`link` makes on every ``aisland setup`` — so every write of it (setup keeping it
current, ``aisland workspace configure … set``) lands in the root project, to be committed there. The rules and the
files the root project brings reach the folder as a harness set (:mod:`aisland.setup.harness`), this machine's
settings live in the root project's ``.git/config`` (:mod:`aisland.machine_config`), and the root project itself is a
project for git only: it is cloned, pulled and pushed like the others, never installed.

Which folder is the root project is :func:`aisland.locations.root_project`'s answer, read with the standard library.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from aisland import locations
from aisland.errors import AislandError


def find(root: Path) -> Optional[Path]:
    """The folder of the root project of the workspace at ``root``, or None for a workspace without one."""
    return locations.root_project(root)


def rooted(root: Path) -> bool:
    """Whether the workspace at ``root`` is one with a root project: :func:`aisland.locations.rooted`."""
    return locations.rooted(root)


def name(root: Path) -> Optional[str]:
    """The root project's folder name — the name its harness set and its row go by — or None."""
    project = find(root)
    return project.name if project is not None else None


def link(root: Path) -> List[str]:
    """Make ``<root>/workspace.toml`` a link to the root project's file, and say so; nothing in a workspace without a
    root project, or once the link is there.

    Refused, and left as it is: a ``workspace.toml`` of the folder's own, or a link to any other file — the folder
    would be written down twice, and setup's edits would never reach the root project.

    A workspace of submodules holding a root project is one still to convert: its own file stays as it is, and the one
    line names both ways on — :mod:`aisland.convert` for a workspace of submodules, or taking the ``.gitmodules`` away
    from a root repository that should record no project —, so setup goes on working while the machine waits."""
    project = find(root)
    if project is None:
        return []
    if not rooted(root):
        return [
            f"{project.name} is a root project beside a .gitmodules: aisland workspace convert makes this workspace one "
            "around it, or remove .gitmodules (and git rm --cached the submodules it lists) to keep a root repository "
            "that records no project"
        ]
    own = root / locations.WORKSPACE_FILE
    target = f"{project.name}/{locations.WORKSPACE_FILE}"
    if own.is_symlink():
        if os.path.realpath(own) == os.path.realpath(project / locations.WORKSPACE_FILE):
            return []
        what = f"a link to {os.readlink(own)}"
    elif own.exists():
        what = "a file of its own"
    else:
        own.symlink_to(target)
        return [f"linked workspace.toml to {target}, the root project's"]
    raise AislandError(
        f"workspace.toml is {what}, not the root project's {target}. "
        "Remove it and run aisland setup again: setup links it."
    )
