"""The workspace's projects as ``aisland wheels`` sees them, and what a project needs of them.

A project is a submodule (without ``.gitmodules``: a project folder) that is a Python project, keyed by its canonical
distribution name; an empty folder is kept, so a requirement on it is told apart from one on something outside the
workspace. What a project needs (:func:`dependency_closure`) follows the extras a requirement names and ignores
markers — the wheels serve another machine, whose platform is not this one's — which is where it differs from
:func:`aisland.workspace.projects.install_groups`, the order setup installs this machine's checkouts in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from aisland import pyproject as pyproject_toml
from aisland.errors import AislandError
from aisland.workspace.model import Workspace
from aisland.workspace.sources import read_committed_sources


@dataclass(frozen=True)
class WorkspaceProject:
    """One submodule of the workspace that is a Python project.

    Args:
        name: Its folder (= its ``.gitmodules`` path), the name a user types.
        distribution: Its distribution name (``acme-tools`` for a folder ``acme``).
        path: Its folder on this machine.
        pyproject: Its parsed ``pyproject.toml``; ``None`` when the folder is empty (a detached
            source or an inactive submodule) — then ``distribution`` comes from ``workspace.toml``,
            or is the folder name — or when the file cannot be read.
        problem: Why its ``pyproject.toml`` cannot be read (the file and the line), else ``None``: the run stops on it
            only when it needs the project — the target, or a project of its closure.
    """

    name: str
    distribution: str
    path: Path
    pyproject: Optional[pyproject_toml.Table]
    problem: Optional[str] = None

    @property
    def checked_out(self) -> bool:
        """Whether this machine has its source."""
        return self.pyproject is not None


def workspace_projects(workspace_root: Path) -> Dict[str, WorkspaceProject]:
    """Every submodule that is a Python project (without ``.gitmodules``: every project folder), keyed by canonical
    distribution name — :attr:`aisland.workspace.model.Workspace.folders`, each with its ``pyproject.toml`` read.

    An empty folder is kept (``pyproject=None``): a requirement on it must be told apart from a
    requirement on something outside the workspace. A ``pyproject.toml`` that is no TOML is kept under its folder's
    name with its :attr:`~WorkspaceProject.problem`, raised only by a run that needs it. Two projects of one
    distribution name are refused, naming both folders: a requirement could mean either.
    """
    registry = read_committed_sources(workspace_root)
    projects: Dict[str, WorkspaceProject] = {}
    for _name, relative, _active in Workspace(workspace_root).folders:
        path = workspace_root / relative
        pyproject_file = path / "pyproject.toml"
        if pyproject_file.is_file():
            try:
                data = pyproject_toml.parse(pyproject_file.read_text(encoding="utf-8"), pyproject_file)
            except (OSError, UnicodeDecodeError, pyproject_toml.PyprojectError) as error:
                problem = str(error) if isinstance(error, AislandError) else f"{pyproject_file}: {error}"
                found = WorkspaceProject(relative, relative, path, None, problem)
            else:
                distribution = (data.get("project") or {}).get("name")
                if not distribution:
                    continue
                found = WorkspaceProject(relative, distribution, path, data)
        else:
            source = registry.get(relative)
            distribution = source.distribution if source and source.distribution else relative
            found = WorkspaceProject(relative, distribution, path, None)
        key = canonicalize_name(found.distribution)
        if key in projects:
            raise AislandError(
                f"{projects[key].name}/pyproject.toml and {found.name}/pyproject.toml are both the distribution "
                f"{found.distribution!r}; rename one of them — a requirement on it could mean either"
            )
        projects[key] = found
    return projects


def find_target(projects: Dict[str, WorkspaceProject], workspace_root: Path, name: str) -> WorkspaceProject:
    """The project ``name`` (its folder, else its distribution) names, refused when it has no checkout here."""
    for project in projects.values():
        if project.name == name:
            break
    else:
        found = projects.get(canonicalize_name(name))
        if found is None:
            raise AislandError(f"no project {name!r} in the workspace {workspace_root}")
        project = found
    if project.problem is not None:
        raise AislandError(project.problem)
    if not project.checked_out:
        raise AislandError(
            f"{project.name} is not checked out on this machine (its folder is empty). "
            f"Activate it with: aisland workspace configure active set {project.name} true --machine-only"
        )
    return project


def requirements(project: WorkspaceProject, extra: Optional[str]) -> List[Requirement]:
    """``project``'s base dependencies (``extra=None``) or one extra's, parsed; a project whose ``pyproject.toml``
    cannot be read is refused, naming the file and the line."""
    if project.problem is not None:
        raise AislandError(project.problem)
    table = (project.pyproject or {}).get("project") or {}
    if extra is None:
        raw = table.get("dependencies")
    else:
        raw = (table.get("optional-dependencies") or {}).get(extra)
    parsed = []
    for spec in raw or []:
        try:
            parsed.append(Requirement(spec))
        except InvalidRequirement as error:
            raise AislandError(f"{project.name}/pyproject.toml: cannot read the requirement {spec!r}: {error}")
    return parsed


def dependency_closure(projects: Dict[str, WorkspaceProject], target: WorkspaceProject) -> List[WorkspaceProject]:
    """The workspace projects ``target`` needs, sorted by folder name, ``target`` excluded.

    Base dependencies always; an extra only when a requirement names it (``confluid[pydantic]``);
    markers ignored, so a project needed on one platform only is still in.
    """
    root = canonicalize_name(target.distribution)
    queue: List[Tuple[str, Optional[str]]] = [(root, None)]
    done: Set[Tuple[str, Optional[str]]] = set()
    while queue:
        item = queue.pop()
        if item in done:
            continue
        done.add(item)
        key, extra = item
        for requirement in requirements(projects[key], extra):
            dependency = canonicalize_name(requirement.name)
            if dependency in projects:
                queue.append((dependency, None))
                queue.extend((dependency, e) for e in requirement.extras)
    needed = {key for key, _ in done} - {root}
    return sorted((projects[key] for key in needed), key=lambda p: p.name)
