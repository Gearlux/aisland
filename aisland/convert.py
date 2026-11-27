"""``aisland workspace convert``: a workspace of submodules becomes a folder of clones around its root project.

A superproject keeps every project's repository in its own ``.git/modules/<name>`` — the project folder holds only a
``.git`` file pointing there — records which commit of each project goes with which, and holds this machine's choices
in its ``.git/config``. A workspace with a root project (:mod:`aisland.workspace.root_project`) has no root repository:
each project is a clone holding its own repository, the workspace's shared files live in the root project, and the
machine's choices in the root project's ``.git/config``.

Converting, in this order, once the refusals below have passed:

1. each checked-out submodule's repository moves into its folder (``<path>/.git``): ``core.worktree`` goes, and its
   worktrees are repaired (``git worktree repair``). Commits, branches, stashes and uncommitted edits stay as they are;
   a project already holding its own repository (a clone ``aisland setup`` added) is left alone;
2. this machine's ``aisland.*`` settings are copied into the root project's ``.git/config``;
3. the old root repository and every file it tracks move into aisland's backup store
   (``<backups>/.workspace-root/<stamp>/files/``, with a ``manifest.json``), never deleted — with the repositories of
   the projects that are off on this machine, which live only there. What the root does not track (``.env``, the
   ``.venv``, the clones beside its projects, a person's own files) stays.

Refused before anything moves: no root project cloned yet (the workspace's shared files must already live in one);
work the root repository holds that no remote has (a change no commit holds, a commit its upstream lacks); a worktree
of the root repository, whose ``.git`` file would point at nothing; a project holding submodules of its own. A
workspace with no root repository has nothing to convert.

It moves into the backup store like ``aisland clean`` (:mod:`aisland.backups`), so it sits beside it, above setup.
``aisland setup`` afterwards links ``workspace.toml`` and the root project's files into the folder.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from aisland import atomic, gitcmd, locations, machine_config
from aisland.backups import MANIFEST_NAME, PAYLOAD_DIRNAME, STAMP_FORMAT, backup_root, measure
from aisland.errors import AislandError
from aisland.workspace import gitmodules

#: The folder of the backup store the old root goes into: a hidden name, which no project ever has.
STORE_NAME = ".workspace-root"
#: What a workspace without a root repository recording its projects answers — none, or one recording nothing beside
#: the workspace's root project (``aisland workspace init`` makes that one).
NOTHING = "nothing to convert: no root repository records the workspace's projects"
#: This machine's settings that mean nothing once there is no root repository: a local root's marker.
_LEFT_BEHIND = ("aisland.workspace.local",)


@dataclass(frozen=True)
class _Module:
    """A checked-out submodule: its folder relative to the root, and its repository in ``.git/modules``."""

    path: str
    gitdir: Path


def _refuse(why: str) -> AislandError:
    return AislandError(f"not converted: {why}")


def _unsaved_work(root: Path) -> Optional[str]:
    """What the root repository holds that no remote has, in words, or None."""
    changed = gitcmd.git(root, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all").stdout
    paths = [line[3:] for line in changed.splitlines() if line.strip()]
    if paths:
        return f"{', '.join(paths)} {'has' if len(paths) == 1 else 'have'} changes no commit holds"
    upstream = gitcmd.read(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not upstream:
        return None  # a root no remote follows (a local one) tracks only aisland's bookkeeping, kept in the store
    ahead = int(gitcmd.read(root, "rev-list", "--count", f"{upstream}..HEAD") or "0")
    if not ahead:
        return None
    branch = gitcmd.read(root, "branch", "--show-current") or "HEAD"
    remote = upstream.split("/", 1)[0]
    return f"{branch} has {ahead} commit{'' if ahead == 1 else 's'} {remote} lacks"


def _modules(root: Path) -> List[_Module]:
    """Every project folder whose ``.git`` is a file pointing into the root's ``.git/modules``: the direct subfolders
    and the paths ``.gitmodules`` names (a pull may already have taken ``.gitmodules`` away)."""
    store = Path(os.path.realpath(root / ".git" / "modules"))
    candidates = {child.name for child in root.iterdir() if child.is_dir() and not child.name.startswith(".")}
    candidates |= {submodule.path for submodule in gitmodules.read(root)}
    found: List[_Module] = []
    for path in sorted(candidates):
        pointer = root / path / ".git"
        if not pointer.is_file():
            continue
        text = pointer.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            continue
        gitdir = Path(os.path.realpath(root / path / text[len("gitdir:") :].strip()))
        if store in gitdir.parents:
            found.append(_Module(path, gitdir))
    return found


def _tracked(root: Path) -> List[str]:
    """The files the root repository tracks, its gitlinks (the projects) left out."""
    listing = gitcmd.git(root, "ls-files", "--stage", "-z", check=True).stdout
    tracked = []
    for entry in listing.split("\0"):
        if not entry:
            continue
        info, _, path = entry.partition("\t")
        if not info.startswith("160000 "):
            tracked.append(path)
    return tracked


def _refusals(root: Path, modules: List[_Module]) -> Path:
    """Every reason not to convert, raised before anything moves; the root project's folder when there is none."""
    project = locations.root_project(root)
    if project is None:
        raise _refuse(
            "no root project here. Clone it into this folder first: "
            "a clone whose workspace.toml names it as [workspace] root."
        )
    why = _unsaved_work(root)
    if why is not None:
        raise _refuse(
            f"the root repository holds work no remote has ({why}). Push it, or carry it into the root project, first."
        )
    worktrees = [
        line[len("worktree ") :]
        for line in gitcmd.git(root, "worktree", "list", "--porcelain").stdout.splitlines()
        if line.startswith("worktree ")
    ][1:]
    if worktrees:
        raise _refuse(
            f"the root repository has worktrees of its own ({', '.join(worktrees)}): their .git files would point at "
            "nothing. Remove each with git worktree remove <path> first."
        )
    nested = [module.path for module in modules if (module.gitdir / "modules").is_dir()]
    if nested:
        raise _refuse(
            f"{', '.join(nested)} holds submodules of its own, whose repositories would lose their way. "
            "Convert those by hand first."
        )
    return project


def _move_repository(root: Path, module: _Module) -> str:
    """``module``'s repository into its folder; its report line."""
    folder = root / module.path
    relative = module.gitdir.relative_to(Path(os.path.realpath(root / ".git"))).as_posix()
    (folder / ".git").unlink()
    shutil.move(str(module.gitdir), str(folder / ".git"))
    # Its config, edited as a file from the root: run in the folder, git would read the stale `core.worktree` first.
    gitcmd.git(root, "config", "--file", str(folder / ".git" / "config"), "--unset", "core.worktree")
    if (folder / ".git" / "worktrees").is_dir():
        gitcmd.git(folder, "worktree", "repair")
    return f"{module.path}: a clone now, its repository moved from .git/{relative} into {module.path}/.git"


def _copy_settings(root: Path, project: Path) -> int:
    """This machine's ``aisland.*`` settings from the root's ``.git/config`` into the root project's; how many."""
    settings = machine_config.get_all(root, r"^aisland\.")
    copied = 0
    for key, value in settings.items():
        if key in _LEFT_BEHIND:
            continue
        machine_config.put(project, key, value, check=True)
        copied += 1
    return copied


def _store(root: Path, tracked: List[str]) -> Tuple[Path, List[str]]:
    """Move the files the root tracks, then its ``.git``, into a new backup of the store; the payload folder and the
    paths moved. Folders the moves left empty go too."""
    created = datetime.now()
    destination = backup_root() / STORE_NAME / created.strftime(STAMP_FORMAT)
    payload = destination / PAYLOAD_DIRNAME
    moved: List[str] = []
    sizes = []
    for path in [*tracked, ".git"]:
        source = root / path
        if not os.path.lexists(source):
            continue
        sizes.append(measure(source))
        target = payload / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(path)
        parent = source.parent
        while parent != root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    manifest = {
        "project": STORE_NAME,
        "project_dir": str(root),
        "created": created.isoformat(timespec="seconds"),
        "bytes": sum(size for size, _files, _is_dir in sizes),
        "files": sum(files for _size, files, _is_dir in sizes),
        "entries": [
            {"path": path, "bytes": size, "files": files} for path, (size, files, _is_dir) in zip(moved, sizes)
        ],
        "failed": [],
    }
    atomic.write_text(destination / MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
    return payload, moved


def convert_workspace(root: Path) -> List[str]:
    """Convert the workspace of submodules at ``root`` into a folder of clones around its root project (the module
    docstring says what moves where); the report's lines. Raises :class:`AislandError` naming why when it may not."""
    if not (root / ".git").is_dir():
        return [NOTHING]
    modules = _modules(root)
    if locations.rooted(root) and not modules and not gitcmd.git(root, "ls-files").stdout.strip():
        return [NOTHING]  # the root repository beside a root project, recording no project (aisland workspace init)
    project = _refusals(root, modules)
    tracked = _tracked(root)
    lines = [_move_repository(root, module) for module in modules]
    copied = _copy_settings(root, project)
    lines.append(f"this machine's settings: {copied} copied into {project.name}/.git/config")
    payload, moved = _store(root, tracked)
    store = payload / ".git" / "modules"
    off = sorted(child.name for child in store.iterdir() if child.is_dir()) if store.is_dir() else []
    lines += [f"{name}: off on this machine; its repository is kept in {store / name}" for name in off]
    files = len([path for path in moved if path != ".git"])
    lines.append(f"the old root repository and the {files} files it tracked: moved into {payload}")
    return lines
