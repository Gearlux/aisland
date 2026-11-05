"""The git settings ``aisland setup`` writes into each project's own config (:mod:`aisland.machine_config`, never a
committed file): ``pull.rebase false``, so a ``git pull`` typed in a project merges, where git without the setting
refuses a divergent one. The workspace git aliases are :func:`aisland.setup.manager.install_git_aliases`.
"""

from typing import Sequence

from aisland import machine_config
from aisland.workspace.projects import ProjectMetadata


def merge_on_pull(projects: Sequence[ProjectMetadata]) -> None:
    """Setup's step: ``pull.rebase false`` in the local config of each of ``projects``."""
    for project in projects:
        machine_config.put(project["path"], "pull.rebase", False)
