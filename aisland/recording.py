"""Whether the pytest plugin records a project's runs: ``aisland workspace test-stats on|off [PROJECT…] [--machine-only]``.

The plugin (:mod:`aisland.pytest_stats`) records every run of a workspace project for ``aisland tests report``; this
switch turns that off. Two layers, as ``aisland workspace track|untrack`` (:mod:`aisland.tracking`):

* committed — the project's ``test-stats = false`` in ``workspace.toml``, and with no project named the workspace's
  ``[workspace] test-stats = false``, which every project without a ``test-stats`` of its own follows, those added
  later included; a project's own value is written only where it differs from the workspace's;
* this machine — ``aisland.test-stats.<project>.record`` in ``.git/config``, and ``aisland.test-stats.default`` for
  every project; this machine's choice wins over the committed one. A committed switch drops this machine's choice
  for the projects it names (with none named: every project's, and this machine's default).

The plugin reads both with the standard library alone (:func:`aisland.pytest_stats.recording`); this module writes them.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from aisland import locations, pytest_stats, workspace_file
from aisland.errors import AislandError

KEY = pytest_stats.RECORD_KEY


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _named(root: Path, projects: Sequence[str]) -> List[str]:
    """``projects``, each a project of ``workspace.toml``; refused before anything changes."""
    known = list(workspace_file.load(root).projects)
    unknown = [name for name in projects if name not in known]
    if unknown:
        raise AislandError(
            f"'{unknown[0]}' is no project of the workspace (its projects: {', '.join(known) or 'none'})"
        )
    return list(projects)


def workspace_default(root: Path) -> bool:
    """Whether a project without a ``test-stats`` of its own is recorded: ``[workspace] test-stats``, else yes."""
    return bool(workspace_file.load(root).settings.get(KEY, True))


def _set_machine_choice(root: Path, project: str, on: Optional[bool]) -> None:
    key = pytest_stats.machine_key(project)
    if on is None:
        _git(root, "config", "--local", "--unset", key)
    else:
        _git(root, "config", "--local", key, "true" if on else "false")


def set_committed(root: Path, projects: Sequence[str], on: bool) -> None:
    """Record ``projects``' runs or not, for everyone — with none named, the whole workspace's, those added later
    included; this machine follows the committed choice again for the projects it names."""
    named = _named(root, projects)
    current = workspace_file.load(root)
    explicit = {name: bool(project.others[KEY]) for name, project in current.projects.items() if KEY in project.others}
    if not named:
        settings = dict(current.settings)
        if on:
            settings.pop(KEY, None)
        else:
            settings[KEY] = False
        workspace_file.write(root, dataclasses.replace(current, settings=settings))
        explicit = {}
        _git(root, "config", "--local", "--unset", pytest_stats.MACHINE_DEFAULT_KEY)
    default = workspace_default(root)
    for name in named:
        explicit[name] = on
    values = {name: {KEY: value} for name, value in explicit.items() if value != default}
    workspace_file.set_settings(root, (KEY,), values)
    for project in named or list(workspace_file.load(root).projects):
        _set_machine_choice(root, project, None)


def set_machine(root: Path, projects: Sequence[str], on: bool) -> None:
    """Record ``projects``' runs or not on this machine only — with none named, every project's here, those added
    later included (``aisland.test-stats.default``)."""
    named = _named(root, projects)
    if named:
        for project in named:
            _set_machine_choice(root, project, on)
        return
    _git(root, "config", "--local", pytest_stats.MACHINE_DEFAULT_KEY, "true" if on else "false")
    for project in workspace_file.load(root).projects:
        _set_machine_choice(root, project, None)


def states(root: Path) -> List[str]:
    """One line per project of ``workspace.toml``: whether its runs are recorded, and when this machine decided it."""
    lines = []
    for project in workspace_file.load(root).projects.values():
        switch = pytest_stats.recording(root, project.path)
        state = "recorded" if switch.on else "not recorded"
        lines.append(f"{project.name}: {state}{' (this machine)' if switch.by == 'this machine' else ''}")
    return lines


def command(root: Path, projects: Sequence[str], on: bool, machine_only: bool) -> List[str]:
    """``aisland workspace test-stats on|off``: the change, then every project's state.

    Refused, before anything is written, outside an aisland workspace — the plugin records nothing there, so there is
    nothing to switch — and with ``--machine-only`` where the workspace is no git repository (this machine's switch
    lives in its ``.git/config``)."""
    if not locations.is_workspace(root):
        raise AislandError(
            f"{root} is no aisland workspace (it holds no workspace.toml or .gitmodules): no test run is recorded "
            "there, so there is nothing to turn on or off"
        )
    if machine_only and not (root / ".git").exists():
        raise AislandError(
            f"{root} is no git repository: this machine's switch lives in its .git/config — leave out --machine-only "
            "to write the choice into workspace.toml"
        )
    _named(root, projects)
    word = "recorded" if on else "not recorded"
    which = ", ".join(projects) if projects else "every project, and every one added later"
    if machine_only:
        set_machine(root, projects, on)
        header = f"==> test runs {word} on this machine: {which} (nothing committed)"
    else:
        set_committed(root, projects, on)
        header = f"==> test runs {word}: {which} (in workspace.toml — commit it)"
    return [header, *(f"  - {line}" for line in states(root))]
