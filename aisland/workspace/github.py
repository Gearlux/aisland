"""A project's GitHub repository, as its remote names it.

A project's GitHub owner is what makes the project, and its PyPI release (:mod:`aisland.pypi`), the workspace's own:
no organisation is written into aisland.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, NamedTuple, Optional

from aisland.workspace import file as workspace_file

#: A GitHub remote in each spelling git accepts: ``git@github.com:owner/repo``, ``ssh://git@github.com/owner/repo``
#: and ``https://github.com/owner/repo`` (a user before the host too: ``https://user@github.com/owner/repo``),
#: ``.git`` optional. A deeper path is no repository.
_GITHUB_URL_RE = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^@/]+@)?github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$"
)


class GitHubRepository(NamedTuple):
    """A repository on github.com, as a project's remote names it."""

    owner: str
    name: str

    @property
    def url(self) -> str:
        """The address a runner clones it from (``https://github.com/<owner>/<name>.git``)."""
        return f"https://github.com/{self.owner}/{self.name}.git"


def github_repository(url: str) -> Optional[GitHubRepository]:
    """The GitHub repository ``url`` names, or None when it is no GitHub repository (another host, a deeper path)."""
    match = _GITHUB_URL_RE.match(url.strip())
    return GitHubRepository(match.group(1), match.group(2)) if match else None


def _folder_key(workspace_root: Path, path: str) -> str:
    """A project's folder relative to the root, as one spelling (``./app/`` → ``app``); a link that leads outside the
    workspace keeps the path it is listed under."""
    folder = (workspace_root / path).resolve()
    try:
        return folder.relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return Path(os.path.normpath(path)).as_posix()


def project_repositories(workspace_root: Path) -> Dict[str, GitHubRepository]:
    """Each project folder whose remote is on GitHub, and that repository.

    The remote is the submodule's in ``.gitmodules``, or a clone's ``origin`` in a workspace without one
    (:func:`aisland.workspace.file.workspace_projects`). A project's GitHub owner is what makes it, and its PyPI
    release, the workspace's own: no organisation is written into aisland.
    """
    repositories: Dict[str, GitHubRepository] = {}
    for found in workspace_file.workspace_projects(workspace_root):
        repository = github_repository(found.url)
        if repository is None:
            continue
        repositories[_folder_key(workspace_root, found.path)] = repository
    return repositories
