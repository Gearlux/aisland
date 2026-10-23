"""Whether the root records a project's commits: ``aisland workspace track|untrack [PROJECT…] [--machine-only]``.

A submodule's commits are recorded in the root, one commit per project, and every new commit of a project shows in
the root's ``git status`` (`` M <project>``) until someone records it. An UNTRACKED project carries git's
``ignore = all``: ``git status`` and ``git diff`` in the root never show its new commits, ``git add <project>`` skips
it, and ``git pull-all`` never records it — the root keeps the commit it last recorded.

Two layers, as for every per-project setting (:mod:`aisland.activation`):

* committed — the project's ``track = false`` in ``workspace.toml`` (no key: tracked), which :func:`apply` writes as
  ``submodule.<name>.ignore = all`` in ``.gitmodules``, the file git reads for everyone;
* this machine — ``aisland.track.<project>.track`` in ``.git/config``, which :func:`apply` writes as git's own
  ``submodule.<name>.ignore`` there (``all`` or ``none``), read by git over ``.gitmodules``. aisland's key survives a
  ``git submodule deinit``, which drops git's section; the next ``aisland setup`` writes git's key back.

An ``ignore`` value aisland does not write (``dirty``, ``untracked``) is the user's, and left alone.

An untracked project FOLLOWS ITS BRANCH instead of the commit the root recorded (user request 2026-10-23): one on no
branch — what a clone's ``git submodule update`` leaves — is fetched and put on its branch by :func:`follow_branches`
(``aisland setup``, ``untrack``) and by ``git pull-all``. The branch is ``.gitmodules``' ``branch`` (``.`` is the root's
branch), else origin's default branch; the switch is never forced.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import click

from aisland import local_workspace, workspace_file

TRACK_KEY = "track"

#: The ``ignore`` values aisland writes; any other is the user's own.
_OURS = ("all", "none")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _get(root: Path, *args: str) -> Optional[str]:
    run = _git(root, "config", *args)
    return run.stdout.strip() if run.returncode == 0 else None


def _machine_key(project: str) -> str:
    return f"aisland.track.{project}.{TRACK_KEY}"


def _submodules(root: Path) -> Dict[str, str]:
    """``{project: submodule name}`` for every project of ``workspace.toml`` that is a submodule, in the file's order."""
    names = local_workspace.submodule_names(root)
    return {
        project.name: names[project.path]
        for project in workspace_file.load(root).projects.values()
        if project.path in names
    }


def submodule_projects(root: Path) -> List[str]:
    """The names of the projects of ``workspace.toml`` that are submodules, in the file's order."""
    return list(_submodules(root))


def committed(root: Path, project: str) -> bool:
    """Whether ``project`` is tracked for everyone: ``workspace.toml`` says ``track = false``, or it is."""
    found = workspace_file.load(root).projects.get(project)
    return found is None or bool(found.others.get(TRACK_KEY, True))


def machine_choice(root: Path, project: str) -> Optional[bool]:
    """This machine's choice for ``project``, or ``None`` when it has made none."""
    value = _get(root, "--local", "--type=bool", "--get", _machine_key(project))
    return None if value is None else value == "true"


def _set_machine_choice(root: Path, project: str, track: Optional[bool]) -> None:
    if track is None:
        _git(root, "config", "--local", "--unset", _machine_key(project))
        return
    _git(root, "config", "--local", _machine_key(project), "true" if track else "false")


@dataclass(frozen=True)
class Change:
    """A project whose commits the root now records (``tracked``) or not, by this machine's choice (``here``) or not."""

    project: str
    tracked: bool
    here: bool

    def line(self) -> str:
        where = " on this machine" if self.here else ""
        if self.tracked:
            return f"{self.project}: tracked{where}"
        return f"{self.project}: untracked{where} (the root records none of its commits)"


def _ignored(root: Path, name: str) -> bool:
    """Whether git ignores the submodule ``name``'s commits here: ``.git/config`` first, then ``.gitmodules``."""
    key = f"submodule.{name}.ignore"
    value = _get(root, "--local", "--get", key)
    if value is None:
        value = _get(root, "-f", ".gitmodules", "--get", key)
    return value == "all"


def apply(root: Path) -> List[Change]:
    """Write git's ``ignore`` of every project from the two layers; a :class:`Change` per project whose effect
    changed. ``aisland setup`` runs it (through :func:`aisland.activation.reconcile`)."""
    changes = []
    for project, name in _submodules(root).items():
        key = f"submodule.{name}.ignore"
        before = _ignored(root, name)
        shared = _get(root, "-f", ".gitmodules", "--get", key)
        if not committed(root, project):
            if shared != "all":
                _git(root, "config", "-f", ".gitmodules", key, "all")
        elif shared == "all":
            _git(root, "config", "-f", ".gitmodules", "--unset", key)
        choice = machine_choice(root, project)
        here = _get(root, "--local", "--get", key)
        if choice is not None:
            want = "none" if choice else "all"
            if here != want:
                _git(root, "config", "--local", key, want)
        elif here in _OURS:
            _git(root, "config", "--local", "--unset", key)
        after = _ignored(root, name)
        if after != before:
            changes.append(Change(project, tracked=not after, here=choice is not None))
    return changes


def _chosen(root: Path, projects: Sequence[str]) -> List[str]:
    """``projects``, or every submodule when none is named; refused before anything changes."""
    submodules = _submodules(root)
    if not submodules:
        raise click.ClickException(f"{root} has no submodules: only a submodule's commits are recorded by the root")
    unknown = [name for name in projects if name not in submodules]
    if unknown:
        raise click.ClickException(
            f"'{unknown[0]}' is no submodule of the workspace (the submodules: {', '.join(submodules)})"
        )
    return list(projects) or list(submodules)


def set_committed(root: Path, projects: Sequence[str], track: bool) -> List[Change]:
    """Track or untrack ``projects`` (none: every submodule) for everyone — ``workspace.toml``'s ``track``, and this
    machine follows it again — then write what git reads."""
    chosen = _chosen(root, projects)
    values = {
        name: {TRACK_KEY: False}
        for name in _submodules(root)
        if (name in chosen and not track) or (name not in chosen and not committed(root, name))
    }
    workspace_file.set_settings(root, (TRACK_KEY,), values)
    for project in chosen:
        _set_machine_choice(root, project, None)
    return apply(root)


def set_machine(root: Path, projects: Sequence[str], track: bool) -> List[Change]:
    """Track or untrack ``projects`` (none: every submodule) on this machine only, then write what git reads."""
    for project in _chosen(root, projects):
        _set_machine_choice(root, project, track)
    return apply(root)


def _followed_branch(root: Path, name: str, folder: Path) -> Optional[str]:
    """The branch an untracked submodule follows: ``.gitmodules``' (``.``: the root's), else origin's default."""
    branch = _get(root, "-f", ".gitmodules", "--get", f"submodule.{name}.branch")
    if branch == ".":
        branch = _git(root, "branch", "--show-current").stdout.strip() or None
    if not branch:
        head = _git(folder, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        branch = head[len("origin/") :] if head.startswith("origin/") else None
    return branch


def follow_branches(root: Path) -> List[str]:
    """Put every untracked, checked-out project that is on no branch on the branch it follows, after a fetch: its own
    local branch when it has one, else a new one tracking origin's. Never forced — a switch git refuses (uncommitted
    edits in the way) is said and the project left; an unreachable origin only means the branch it already knows.
    A local branch strictly behind origin's is fast-forwarded to it. One line per project acted on."""
    lines = []
    quiet = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for project, name in _submodules(root).items():
        path = next(p for p, n in local_workspace.submodule_names(root).items() if n == name)
        folder = root / path
        if not (folder / ".git").exists() or not _ignored(root, name):
            continue
        if _git(folder, "branch", "--show-current").stdout.strip():
            continue
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=folder, capture_output=True, env=quiet)
        branch = _followed_branch(root, name, folder)
        if branch is None:
            lines.append(f"{project}: stays on no branch — .gitmodules names none and origin has no default")
            continue
        if _git(folder, "rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode == 0:
            done = _git(folder, "switch", "-q", branch)
        else:
            done = _git(folder, "switch", "-q", "-c", branch, "--track", f"origin/{branch}")
        if done.returncode != 0:
            refusal = (done.stderr.strip().splitlines() or ["failed"])[0]
            lines.append(f"{project}: stays on no branch — git switch {branch} refused: {refusal}")
            continue
        # A local branch behind origin's moves up to it; one with commits of its own is left for `git pull-all`.
        _git(folder, "merge", "-q", "--ff-only", f"origin/{branch}")
        lines.append(f"{project}: on {branch} (it was on no branch)")
    return lines


def track_command(root: Path, projects: Sequence[str], track: bool, machine_only: bool) -> List[str]:
    """``aisland workspace track|untrack``: the change and the report's lines; a local workspace commits it."""
    chosen = _chosen(root, projects)
    word = "tracked" if track else "untracked"
    if machine_only:
        changes = set_machine(root, chosen, track)
        header = f"==> {word} on this machine: {', '.join(chosen)} (nothing committed)"
    else:
        changes = set_committed(root, chosen, track)
        local = local_workspace.is_local(root)
        header = (
            f"==> {word}: {', '.join(chosen)} (in workspace.toml and .gitmodules{'' if local else ' — commit them'})"
        )
    lines = [header, *(f"  - {change.line()}" for change in changes)]
    lines += [f"  - {line}" for line in follow_branches(root)]
    lines += [f"  - {line}" for line in local_workspace.settle(root).lines()]
    return lines
