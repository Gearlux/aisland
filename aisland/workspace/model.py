"""The workspace as one record: its projects, each with everything aisland knows of it, read from disk when first asked.

A :class:`Project` joins what used to be read separately — the folder it lives in and whether this checkout carries it
(:func:`aisland.workspace.projects.workspace_folders`), its submodule (:mod:`aisland.workspace.gitmodules`), its row
of ``workspace.toml`` (:mod:`aisland.workspace.file`) and its ``pyproject.toml``'s metadata — so a caller asks one
record instead of zipping four lists by name or folder.

A :class:`Workspace` is a SNAPSHOT of the disk as it was first read: each part is read once, when first asked, and
never again. Build one where it is used, never keep one across a step that changes the workspace (``aisland setup``
writes ``workspace.toml`` and adds submodules between its steps) — reading again costs a few milliseconds, a stale
answer a wrong command.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from aisland import locations
from aisland.workspace import file as workspace_file
from aisland.workspace import gitmodules
from aisland.workspace.projects import (
    ProjectFolder,
    ProjectMetadata,
    dependency_order,
    discover_projects,
    get_project_metadata,
    get_workspace_name,
    workspace_folders,
)


@dataclass(frozen=True)
class Project:
    """One project of the workspace: its ``name``, its ``folder`` relative to the root and the absolute ``path``,
    whether this checkout carries it (``active``), its ``submodule`` (``None`` in a workspace of plain folders), its row
    of ``workspace.toml`` (``listed``, ``None`` when the file has none) and its ``pyproject.toml``'s ``metadata``
    (``None`` for a folder without one: a detached source, a project not cloned yet)."""

    name: str
    folder: str
    path: Path
    active: bool
    submodule: Optional[gitmodules.Submodule]
    listed: Optional[workspace_file.Project]
    metadata: Optional[ProjectMetadata]

    @property
    def checked_out(self) -> bool:
        """Whether the folder holds a clone (a deinit'd submodule's is empty)."""
        return (self.path / ".git").exists()


class Workspace:
    """The workspace at ``root``, each part read when first asked (see the module's note on snapshots)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @functools.cached_property
    def name(self) -> str:
        """``[workspace] name``, else the origin's repository, else the folder (:func:`get_workspace_name`)."""
        return get_workspace_name(self.root)

    @functools.cached_property
    def file(self) -> workspace_file.WorkspaceFile:
        """``workspace.toml``, checked; an empty one when the workspace has none."""
        return workspace_file.load(self.root)

    @functools.cached_property
    def submodules(self) -> List[gitmodules.Submodule]:
        """Every submodule ``.gitmodules`` declares (none without the file)."""
        return gitmodules.read(self.root)

    @functools.cached_property
    def folders(self) -> List[ProjectFolder]:
        """The projects by their folders: the submodules, or without ``.gitmodules`` every folder of the root
        holding a ``pyproject.toml`` (:func:`workspace_folders`)."""
        return workspace_folders(self.root)

    @functools.cached_property
    def top_level_folders(self) -> List[Path]:
        """Every direct, non-hidden folder of the root holding a ``pyproject.toml``, whatever ``.gitmodules`` says (a
        link to another folder of the workspace is none): the projects with something on disk to read."""
        return sorted(
            child
            for child in self.root.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and (child / "pyproject.toml").is_file()
            and not locations.is_alias(child, self.root)
        )

    @functools.cached_property
    def metadata(self) -> List[ProjectMetadata]:
        """The metadata of every ``pyproject.toml`` of the workspace's projects, nested ones included
        (:func:`discover_projects`), by folder name: the order a folder listing gives differs between file systems,
        and every order made from this one (:attr:`install_order`, the editor's folders) must not."""
        found = [get_project_metadata(pyproject) for pyproject in discover_projects(self.root)]
        return sorted(found, key=lambda entry: entry["path"].name)

    @functools.cached_property
    def install_order(self) -> List[ProjectMetadata]:
        """:attr:`metadata`, dependencies first."""
        return dependency_order(self.metadata)

    @functools.cached_property
    def projects(self) -> List[Project]:
        """Every project of :attr:`folders`, in its order, with its submodule, its ``workspace.toml`` row and its
        metadata joined in (a row is matched by its path, as ``aisland setup`` matches it)."""
        submodules = {submodule.name: submodule for submodule in self.submodules}
        listed = {row.path: row for row in self.file.projects.values()}
        metadata = {entry["path"].resolve(): entry for entry in self.metadata}
        return [
            Project(
                name=name,
                folder=folder,
                path=self.root / folder,
                active=active,
                submodule=submodules.get(name),
                listed=listed.get(folder),
                metadata=metadata.get((self.root / folder).resolve()),
            )
            for name, folder, active in self.folders
        ]

    def project(self, name: str) -> Optional[Project]:
        """The project called ``name``, or ``None``."""
        return self.by_name.get(name)

    @functools.cached_property
    def by_name(self) -> Dict[str, Project]:
        """:attr:`projects` by name."""
        return {project.name: project for project in self.projects}

    @functools.cached_property
    def known_names(self) -> List[str]:
        """Every name a project command takes: each submodule's, and each top-level folder's that holds a
        ``pyproject.toml`` (a sibling no submodule lists)."""
        return sorted({submodule.name for submodule in self.submodules} | {f.name for f in self.top_level_folders})
