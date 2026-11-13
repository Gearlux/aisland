"""The git configuration ``aisland setup`` writes, never into a committed file nor the global config: each project's
``pull.rebase false`` (:func:`merge_on_pull`, through :mod:`aisland.machine_config`), so a ``git pull`` typed in a
project merges where git without the setting refuses a divergent one, and the workspace git aliases
(:func:`install_git_aliases`, the packaged ``setup_git.sh``) in the local config of the root and each active submodule.
"""

import subprocess
from pathlib import Path
from typing import Sequence

from aisland import console, failures, machine_config
from aisland.paths import SERVICES_DIR
from aisland.workspace.projects import ProjectMetadata

#: The script that installs the workspace git aliases (`git pull-all`, `git push-all`, ...), shipped in the package.
SETUP_GIT_SCRIPT = SERVICES_DIR / "git" / "setup_git.sh"


def merge_on_pull(projects: Sequence[ProjectMetadata]) -> None:
    """Setup's step: ``pull.rebase false`` in the local config of each of ``projects``."""
    for project in projects:
        machine_config.put(project["path"], "pull.rebase", False)


def install_git_aliases(workspace_root: Path, quiet: bool = False) -> bool:
    """Install the workspace git aliases into the local config of ``workspace_root`` and each active submodule.

    Runs the packaged ``setup_git.sh`` with the root as its argument, so the scripts need not sit in the workspace:
    from a wheel the aliases name them by their path in ``site-packages`` (`aisland setup` runs it every time).
    Returns whether the script succeeded. Its output is passed through, or with ``quiet`` shown only on a failure.
    """
    result = subprocess.run(
        ["bash", str(SETUP_GIT_SCRIPT), str(workspace_root)],
        cwd=workspace_root,
        capture_output=quiet,
        text=True,
    )
    if result.returncode != 0:
        if quiet:
            console.echo((result.stdout or "") + (result.stderr or ""), nl=False)
        failures.report(f"  - Error: installing the git aliases failed (bash {SETUP_GIT_SCRIPT} {workspace_root}).")
    return result.returncode == 0
