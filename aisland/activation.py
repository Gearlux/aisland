"""Which projects this machine has: the ``active`` plugin of ``aisland workspace configure``.

A project whose table in ``workspace.toml`` has an ``active`` key is OPTIONAL and the value is its default; a project
without the key is always part of the workspace. This machine's choice is ``aisland.active.<project>.active`` in
``.git/config`` — aisland's own key, so git's ``submodule deinit`` (which drops git's section) never erases it.

``aisland setup`` asks about each optional project (:func:`resolve`), then brings the folders in line
(:func:`reconcile`): an active project is checked out — added from its url and branch when the workspace does not
have it yet (a machine made by ``init --from``, which clones only the projects without ``active``) — an inactive one
emptied, never forced. It also writes what git reads: ``.gitmodules`` ``active = false`` for a project whose default
is off, and this machine's ``submodule.<name>.active``, so a plain ``git submodule update`` agrees.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import click

from aisland import gitcmd, local_workspace, machine_config, tracking, workspace_file
from aisland.projects import submodule_active_map
from aisland.sources import detached_projects

#: The project's setting in ``workspace.toml`` and the last part of this machine's ``aisland.active.<project>.active``.
ACTIVE_KEY = "active"


# --- what is optional, and what is in effect here -----------------------------------------------------------------


def optional(root: Path) -> Dict[str, bool]:
    """``{project: default}`` for every project of ``workspace.toml`` with an ``active`` key, in the file's order."""
    return {
        name: bool(project.others[ACTIVE_KEY])
        for name, project in workspace_file.load(root).projects.items()
        if ACTIVE_KEY in project.others
    }


def _machine_key(project: str) -> str:
    return f"aisland.active.{project}.{ACTIVE_KEY}"


def machine_choice(root: Path, project: str) -> Optional[bool]:
    """This machine's choice for ``project``, or ``None`` when it has made none."""
    return machine_config.get_bool(root, _machine_key(project))


def set_machine_choice(root: Path, project: str, active: Optional[bool]) -> None:
    """Record (or, with ``None``, forget) this machine's choice. Never committed."""
    machine_config.put(root, _machine_key(project), active)


def effective(root: Path, project: str) -> bool:
    """Whether ``project`` is part of the workspace on this machine: its choice, else its default, else yes."""
    choice = machine_choice(root, project)
    if choice is not None:
        return choice
    return optional(root).get(project, True)


# --- setup asks ----------------------------------------------------------------------------------------------------


def resolve(root: Path, non_interactive: bool) -> None:
    """Ask about each optional project, its saved answer or else its default offered; record the answer.

    Under ``non_interactive`` (``aisland setup -y``) a project this machine has answered is not asked again; one it
    never answered still is — a machine consciously opts into an optional project."""
    defaults = optional(root)
    if not defaults:
        return
    click.echo("==> Resolving optional project activation...")
    for project, default in defaults.items():
        choice = machine_choice(root, project)
        if non_interactive and choice is not None:
            continue
        answer = click.confirm(
            f"  Activate '{project}'? (clone + install)", default=default if choice is None else choice
        )
        set_machine_choice(root, project, answer)


# --- setup brings the folders in line ------------------------------------------------------------------------------


def _submodule_names(root: Path) -> Dict[str, str]:
    """``{path: submodule name}`` from ``.gitmodules``."""
    return {path: name for name, path, _active in _gitmodules(root)}


def _gitmodules(root: Path) -> List[tuple[str, str, bool]]:

    return submodule_active_map(root)


def reconcile(root: Path) -> List[str]:
    """Check each project out or empty it as this machine has it, and tell git. Returns the projects whose folder
    could not follow (a deinit refused, an add or init that failed), each said on the way.

    Then each project's ``ignore`` is written from its tracking (:mod:`aisland.tracking`) — git's own key in
    ``.git/config`` is gone after a deinit —, each untracked project on no branch is put on its branch, and a local
    workspace (:mod:`aisland.local_workspace`) commits
    ``.gitmodules`` and the projects added."""
    stuck = _reconcile(root)
    for change in tracking.apply(root):
        click.echo(f"  - {change.line()}")
    for line in tracking.follow_branches(root):
        click.echo(f"  - {line}")
    for line in local_workspace.settle(root).lines():
        click.echo(f"  - {line}")
    return stuck


def _reconcile(root: Path) -> List[str]:

    listed = workspace_file.load(root)
    if not listed.projects:
        return []
    defaults = optional(root)
    detached = detached_projects(root)
    names = _submodule_names(root)
    stuck: List[str] = []
    click.echo("==> Bringing the project folders in line with their activation...")
    for project in listed.projects.values():
        want = effective(root, project.name) and project.name not in detached
        name = names.get(project.path)
        checked_out = (root / project.path / ".git").exists()
        if want and not checked_out:
            if name is not None:
                click.echo(f"  - init {project.path} (active)")
                done = gitcmd.git(root, "submodule", "update", "--init", project.path)
            elif project.url is not None:
                click.echo(f"  - add {project.path} (active)")
                branch = ["-b", project.branch] if project.branch else []
                # A local workspace's `.gitignore` ignores everything: git adds the path only with `-f`.
                force = ["-f"] if local_workspace.is_local(root) else []
                done = gitcmd.git(root, "submodule", "add", "-q", *force, *branch, project.url, project.path)
                name = _submodule_names(root).get(project.path)
            else:
                click.echo(f"    skip {project.path}: active, but workspace.toml gives it no url to clone from.")
                stuck.append(project.name)
                continue
            if done.returncode != 0:
                click.echo(
                    f"    skip {project.path}: {done.stderr.strip().splitlines()[-1] if done.stderr else 'failed'}"
                )
                stuck.append(project.name)
        elif not want and checked_out and name is not None:
            click.echo(f"  - deinit {project.path} (inactive)")
            # No ``-f``: a deinit that would discard uncommitted work refuses, protecting it.
            if gitcmd.git(root, "submodule", "deinit", project.path).returncode != 0:
                click.echo(f"    skip {project.name}: deinit refused; resolve local changes and re-run.")
                stuck.append(project.name)
        if name is None:
            continue
        machine_config.put(root, f"submodule.{name}.active", want)
        if defaults.get(project.name) is False:
            gitcmd.git(root, "config", "-f", ".gitmodules", f"submodule.{name}.active", "false")
        else:
            gitcmd.git(root, "config", "-f", ".gitmodules", "--unset", f"submodule.{name}.active")
    return stuck


# --- `aisland workspace configure active` --------------------------------------------------------------------------


def rows(root: Path) -> List[Dict[str, str]]:
    """One row per optional project or project this machine has a choice for."""
    defaults = optional(root)
    found = []
    for name, project in workspace_file.load(root).projects.items():
        choice = machine_choice(root, name)
        if name not in defaults and choice is None:
            continue
        found.append(
            {
                "project": name,
                "committed": str(defaults[name]).lower() if name in defaults else "-",
                "this machine": "-" if choice is None else str(choice).lower(),
                "folder": "checked out" if (root / project.path / ".git").exists() else "empty",
            }
        )
    return found


def _known(root: Path, project: str) -> bool:
    projects = workspace_file.load(root).projects
    if project in projects:
        return True
    click.echo(
        f"Error: '{project}' is no project of {workspace_file.FILE_NAME} (the projects: {', '.join(projects)})",
        err=True,
    )
    return False


def _write_default(root: Path, project: str, default: Optional[bool]) -> None:
    """Set (or, with ``None``, remove) ``project``'s committed default, keeping every other project's."""
    values = {name: {ACTIVE_KEY: value} for name, value in optional(root).items()}
    values.pop(project, None)
    if default is not None:
        values[project] = {ACTIVE_KEY: default}
    workspace_file.set_settings(root, (ACTIVE_KEY,), values)


def set_active(root: Path, project: str, active: bool, machine_only: bool) -> int:
    """Record ``project``'s choice for this machine, or its committed default, and bring the folders in line."""
    if not _known(root, project):
        return 1
    if machine_only:
        set_machine_choice(root, project, active)
        click.echo(f"==> {project}: {'active' if active else 'inactive'} on this machine (nothing committed)")
    else:
        _write_default(root, project, active)
        click.echo(f"==> {project}: optional, {'on' if active else 'off'} by default (in {workspace_file.FILE_NAME})")
    return 1 if project in reconcile(root) else 0


def unset_active(root: Path, project: str, machine_only: bool) -> int:
    """Forget this machine's choice, or make ``project`` always part of the workspace, and bring the folders in line."""
    if not _known(root, project):
        return 1
    if machine_only:
        set_machine_choice(root, project, None)
        click.echo(f"==> {project}: this machine follows the committed default again")
    else:
        _write_default(root, project, None)
        click.echo(f"==> {project}: always part of the workspace")
    return 1 if project in reconcile(root) else 0
