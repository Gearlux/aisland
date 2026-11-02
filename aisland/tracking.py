"""Whether the root records a project's commits: ``aisland workspace track set|unset|status [PROJECT…]``.

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
(``aisland setup``, ``track unset``) and by ``git pull-all``. The branch is ``.gitmodules``' ``branch`` (``.`` is the
root's branch), else origin's default branch; the switch is never forced.

``track set`` records a project's commits, ``track unset`` stops recording them (each writes ``track = true|false``
as above, never removes a setting), and ``track status`` reads the two layers and git back without changing any of
them (:func:`status`).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, Sequence, Tuple

from aisland import gitcmd, local_workspace, workspace_file
from aisland.errors import AislandError

TRACK_KEY = "track"

#: The ``ignore`` values aisland writes; any other is the user's own.
_OURS = ("all", "none")


def _get(root: Path, *args: str) -> Optional[str]:
    run = gitcmd.git(root, "config", *args)
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


#: This machine's choice for every project without one of its own (`track unset --machine-only`, no project named).
_MACHINE_DEFAULT_KEY = "aisland.track.default"


def workspace_default(root: Path) -> bool:
    """Whether a project without a ``track`` of its own is tracked: ``[workspace] track`` in ``workspace.toml``, which
    ``init --untracked`` and ``track unset`` with no project named write, else yes."""
    return bool(workspace_file.load(root).settings.get(TRACK_KEY, True))


def committed(root: Path, project: str) -> bool:
    """Whether ``project`` is tracked for everyone: its own ``track``, else the workspace's (:func:`workspace_default`)."""
    found = workspace_file.load(root).projects.get(project)
    if found is not None and TRACK_KEY in found.others:
        return bool(found.others[TRACK_KEY])
    return workspace_default(root)


def _bool_key(root: Path, key: str) -> Optional[bool]:
    value = _get(root, "--local", "--type=bool", "--get", key)
    return None if value is None else value == "true"


def machine_choice(root: Path, project: str) -> Optional[bool]:
    """This machine's choice for ``project`` — its own, else this machine's for every project — or ``None``."""
    own = _bool_key(root, _machine_key(project))
    return own if own is not None else _bool_key(root, _MACHINE_DEFAULT_KEY)


def _set_machine_choice(root: Path, project: str, track: Optional[bool]) -> None:
    if track is None:
        gitcmd.git(root, "config", "--local", "--unset", _machine_key(project))
        return
    gitcmd.git(root, "config", "--local", _machine_key(project), "true" if track else "false")


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


def _ignore_value(root: Path, name: str) -> Optional[str]:
    """git's ``ignore`` of the submodule ``name`` here: ``.git/config`` first, then ``.gitmodules``; ``None`` when unset."""
    key = f"submodule.{name}.ignore"
    value = _get(root, "--local", "--get", key)
    if value is None:
        value = _get(root, "-f", ".gitmodules", "--get", key)
    return value


def _ignored(root: Path, name: str) -> bool:
    """Whether git ignores the submodule ``name``'s commits here."""
    return _ignore_value(root, name) == "all"


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
                gitcmd.git(root, "config", "-f", ".gitmodules", key, "all")
        elif shared == "all":
            gitcmd.git(root, "config", "-f", ".gitmodules", "--unset", key)
        choice = machine_choice(root, project)
        here = _get(root, "--local", "--get", key)
        if choice is not None:
            want = "none" if choice else "all"
            if here != want:
                gitcmd.git(root, "config", "--local", key, want)
        elif here in _OURS:
            gitcmd.git(root, "config", "--local", "--unset", key)
        after = _ignored(root, name)
        if after != before:
            changes.append(Change(project, tracked=not after, here=choice is not None))
    return changes


def _named(root: Path, projects: Sequence[str]) -> List[str]:
    """``projects``, each a submodule of the workspace; refused before anything changes."""
    submodules = _submodules(root)
    unknown = [name for name in projects if name not in submodules]
    if unknown:
        listed = ", ".join(submodules) or "none"
        raise AislandError(f"'{unknown[0]}' is no submodule of the workspace (the submodules: {listed})")
    return list(projects)


def _write_workspace_default(root: Path, track: bool) -> None:
    """``[workspace] track = false``, or no key when tracked (the default), keeping every other setting."""
    current = workspace_file.load(root)
    settings = dict(current.settings)
    if track:
        settings.pop(TRACK_KEY, None)
    else:
        settings[TRACK_KEY] = False
    workspace_file.write(root, dataclasses.replace(current, settings=settings))


def set_committed(root: Path, projects: Sequence[str], track: bool) -> List[Change]:
    """Track or untrack ``projects`` for everyone — or, with none named, the whole workspace: ``[workspace] track``,
    which every project without a ``track`` of its own follows, those added later included. A project's own ``track``
    is written only where it differs from the workspace's; this machine follows the committed choice again. Then write
    what git reads."""
    named = _named(root, projects)
    explicit = {
        name: bool(project.others[TRACK_KEY])
        for name, project in workspace_file.load(root).projects.items()
        if TRACK_KEY in project.others
    }
    if not named:
        _write_workspace_default(root, track)
        explicit = {}
        gitcmd.git(root, "config", "--local", "--unset", _MACHINE_DEFAULT_KEY)
    default = workspace_default(root)
    for name in named:
        explicit[name] = track
    values = {name: {TRACK_KEY: value} for name, value in explicit.items() if value != default}
    workspace_file.set_settings(root, (TRACK_KEY,), values)
    for project in named or _submodules(root):
        _set_machine_choice(root, project, None)
    return apply(root)


def set_machine(root: Path, projects: Sequence[str], track: bool) -> List[Change]:
    """Track or untrack ``projects`` on this machine only — or, with none named, every project here, those added later
    included (``aisland.track.default``). Then write what git reads."""
    named = _named(root, projects)
    if named:
        for project in named:
            _set_machine_choice(root, project, track)
    else:
        gitcmd.git(root, "config", "--local", _MACHINE_DEFAULT_KEY, "true" if track else "false")
        for project in _submodules(root):
            _set_machine_choice(root, project, None)
    return apply(root)


def _followed_branch(root: Path, name: str, folder: Path) -> Optional[str]:
    """The branch an untracked submodule follows: ``.gitmodules``' (``.``: the root's), else origin's default."""
    branch = _get(root, "-f", ".gitmodules", "--get", f"submodule.{name}.branch")
    if branch == ".":
        branch = gitcmd.git(root, "branch", "--show-current").stdout.strip() or None
    if not branch:
        head = gitcmd.git(folder, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        branch = head[len("origin/") :] if head.startswith("origin/") else None
    return branch


def follow_branches(root: Path) -> List[str]:
    """Put every untracked, checked-out project that is on no branch on the branch it follows, after a fetch: its own
    local branch when it has one, else a new one tracking origin's. Never forced — a switch git refuses (uncommitted
    edits in the way) is said and the project left; an unreachable origin only means the branch it already knows.
    A local branch strictly behind origin's is fast-forwarded to it. One line per project acted on."""
    lines = []
    for project, name in _submodules(root).items():
        path = next(p for p, n in local_workspace.submodule_names(root).items() if n == name)
        folder = root / path
        if not (folder / ".git").exists() or not _ignored(root, name):
            continue
        if gitcmd.git(folder, "branch", "--show-current").stdout.strip():
            continue
        gitcmd.fetch(folder, "-q", "origin")
        branch = _followed_branch(root, name, folder)
        if branch is None:
            lines.append(f"{project}: stays on no branch — .gitmodules names none and origin has no default")
            continue
        if gitcmd.git(folder, "rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode == 0:
            done = gitcmd.git(folder, "switch", "-q", branch)
        else:
            done = gitcmd.git(folder, "switch", "-q", "-c", branch, "--track", f"origin/{branch}")
        if done.returncode != 0:
            refusal = (done.stderr.strip().splitlines() or ["failed"])[0]
            lines.append(f"{project}: stays on no branch — git switch {branch} refused: {refusal}")
            continue
        # A local branch behind origin's moves up to it; one with commits of its own is left for `git pull-all`.
        gitcmd.git(folder, "merge", "-q", "--ff-only", f"origin/{branch}")
        lines.append(f"{project}: on {branch} (it was on no branch)")
    return lines


#: Whose choice a project's is: this machine's own for it, this machine's for every project, the project's own
#: ``track`` in ``workspace.toml``, the workspace's ``[workspace] track``, or none (tracked by default).
Source = Literal["machine", "machine-default", "project", "workspace", "default"]

#: Whether git agrees with the choice: its ``ignore`` is what :func:`apply` writes for it (``applied``), is not
#: (``not applied``: ``aisland setup`` writes it), or is a value aisland does not write and leaves alone (``yours``).
Git = Literal["applied", "not applied", "yours"]


def _toml(value: bool) -> str:
    return "true" if value else "false"


@dataclass(frozen=True)
class Status:
    """One project's choice for ``track status``: is its root-recorded commit followed, whose choice that is, and whether
    git's ``ignore`` (``ignore``, ``None`` when unset) agrees."""

    project: str
    tracked: bool
    source: Source
    reason: str
    git: Git
    ignore: Optional[str]

    def line(self) -> str:
        word = "tracked" if self.tracked else "untracked"
        if self.git == "applied":
            git = "applied"
        elif self.git == "not applied":
            git = "NOT applied — run `aisland setup`"
        else:
            git = f"ignore = {self.ignore}, yours, left alone"
        return f"{self.project}: {word} ({self.reason}; git: {git})"


def _workspace_reason(loaded: workspace_file.WorkspaceFile) -> str:
    """Where the workspace's own choice comes from: its ``[workspace] track``, else the default."""
    if TRACK_KEY in loaded.settings:
        return f"[workspace] track = {_toml(bool(loaded.settings[TRACK_KEY]))} in workspace.toml"
    return "the default: no track in workspace.toml"


def _choice(
    loaded: workspace_file.WorkspaceFile, project: str, own: Optional[bool], machine_default: Optional[bool]
) -> Tuple[bool, Source, str]:
    """``project``'s effective choice, whose it is and in words — in the order :func:`apply` weighs them."""
    if own is not None:
        return own, "machine", f"this machine's {_machine_key(project)} = {_toml(own)}"
    if machine_default is not None:
        return machine_default, "machine-default", f"this machine's {_MACHINE_DEFAULT_KEY} = {_toml(machine_default)}"
    found = loaded.projects.get(project)
    if found is not None and TRACK_KEY in found.others:
        value = bool(found.others[TRACK_KEY])
        return value, "project", f"its own track = {_toml(value)} in workspace.toml"
    source: Source = "workspace" if TRACK_KEY in loaded.settings else "default"
    return bool(loaded.settings.get(TRACK_KEY, True)), source, _workspace_reason(loaded)


def status(root: Path, projects: Sequence[str] = ()) -> List[Status]:
    """A :class:`Status` for each of ``projects`` in the order named — no project: every submodule, in
    ``workspace.toml``'s order. Reads only; an unknown project is refused before anything is read."""
    named = _named(root, projects)
    submodules = _submodules(root)
    loaded = workspace_file.load(root)
    machine_default = _bool_key(root, _MACHINE_DEFAULT_KEY)
    rows = []
    for project in named or list(submodules):
        tracked, source, reason = _choice(loaded, project, _bool_key(root, _machine_key(project)), machine_default)
        ignore = _ignore_value(root, submodules[project])
        git: Git
        if ignore not in (None, *_OURS):
            git = "yours"
        elif (ignore == "all") == (not tracked):
            git = "applied"
        else:
            git = "not applied"
        rows.append(Status(project, tracked, source, reason, git, ignore))
    return rows


def status_lines(root: Path, projects: Sequence[str] = ()) -> List[str]:
    """``aisland workspace track status``: the lines of a named project each, else the workspace's choice and the
    projects that differ from it or that git does not yet agree with — the rest are counted, or, when none differs,
    said once."""
    rows = status(root, projects)
    if projects:
        return [row.line() for row in rows]
    loaded = workspace_file.load(root)
    tracked = bool(loaded.settings.get(TRACK_KEY, True))
    word = "tracked" if tracked else "untracked"
    reason = _workspace_reason(loaded)
    plain = [row for row in rows if row.source in ("workspace", "default") and row.git == "applied"]
    collapsed = plain if len(plain) > 1 else []
    if collapsed and len(collapsed) == len(rows):
        effect = "the root records their commits" if tracked else "the root records none of their commits"
        return [
            f"==> {word}: all {len(rows)} projects ({effect})",
            f"  - from {reason}",
            "  - this machine: no choice of its own",
            f"  - git: {'no ignore' if tracked else 'ignore = all'}, applied for all {len(rows)}",
        ]
    lines = [f"==> the workspace default: {word} ({reason})"]
    lines += [f"  - {row.line()}" for row in rows if row not in collapsed]
    if not rows:
        lines.append("  - no submodules yet")
    elif collapsed:
        lines.append(f"  - {len(collapsed)} more: {word}, as the workspace default says")
    return lines


def status_json(root: Path, projects: Sequence[str] = ()) -> Dict[str, object]:
    """``aisland workspace track status --json``: the workspace's choice and every project's row."""
    return {
        "default_tracked": workspace_default(root),
        "projects": [dataclasses.asdict(row) for row in status(root, projects)],
    }


def track_command(root: Path, projects: Sequence[str], track: bool, machine_only: bool) -> List[str]:
    """``aisland workspace track set|unset``: the change and the report's lines; a local workspace commits it."""
    _named(root, projects)
    word = "tracked" if track else "untracked"
    which = ", ".join(projects) if projects else "every project, and every one added later"
    if machine_only:
        changes = set_machine(root, projects, track)
        header = f"==> {word} on this machine: {which} (nothing committed)"
    else:
        changes = set_committed(root, projects, track)
        local = local_workspace.is_local(root)
        header = f"==> {word}: {which} (in workspace.toml and .gitmodules{'' if local else ' — commit them'})"
    lines = [header, *(f"  - {change.line()}" for change in changes)]
    lines += [f"  - {line}" for line in follow_branches(root)]
    lines += [f"  - {line}" for line in local_workspace.settle(root).lines()]
    return lines
