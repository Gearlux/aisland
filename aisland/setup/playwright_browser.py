"""A dev dependency that needs more than pip: Playwright's browser.

Listing ``playwright`` under a project's ``dev`` extra is the whole declaration: ``aisland setup`` fetches the browser
once the project's editable install is in (:func:`install_browsers`), and the scaffolded CI emits the same step
(:mod:`aisland.ci.ci_scaffold`), so a browser-driven test runs wherever the extra is installed instead of skipping.
Named for the browser, not ``playwright``: that is the package the workspace's venv holds.
"""

import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence

from aisland import console, failures
from aisland import pyproject as pyproject_toml
from aisland.workspace.projects import ProjectMetadata
from aisland.workspace.selection import dev_extra_names

logger = logging.getLogger(__name__)

#: The one browser a workspace test asks for — Playwright's package alone cannot open a page.
PLAYWRIGHT_BROWSER = "chromium"


def browser_install_command(pyproject: Path, venv_bin: Path) -> Optional[List[str]]:
    """The command that fetches the browser a ``playwright`` DEV dependency needs, or ``None``.

    Listing ``playwright`` under ``dev`` is the whole declaration — the shape of the ``slow`` pytest
    marker, never an aisland knob. ``aisland setup`` runs this right after the project's editable
    install, and the scaffolded CI emits its equivalent after ``.[dev]``, so a browser-driven test
    runs everywhere the extra is installed instead of ``skip``-ping into a green build that checks
    less. A project without the dependency gets exactly what it always did.
    """
    if not pyproject.exists():
        return None
    data = pyproject_toml.load(pyproject)
    if "playwright" not in dev_extra_names(data):
        return None
    return [str(venv_bin / "playwright"), "install", PLAYWRIGHT_BROWSER]


def install_browsers(projects: Sequence[ProjectMetadata], venv_bin: Path) -> None:
    """Setup's step: fetch the browser of each of ``projects`` (those whose editable install went in) that declares
    ``playwright`` under ``dev``. A fetch that fails is reported and the others still run."""
    for project in projects:
        project_dir = project["path"]
        browser = browser_install_command(project_dir / "pyproject.toml", venv_bin)
        if browser:
            console.echo(f"==> Installing Playwright's {PLAYWRIGHT_BROWSER} for {project['name']}...")
            try:
                logger.debug("running %s (in %s)", " ".join(browser), project_dir)
                subprocess.check_call(browser, cwd=project_dir)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                failures.report(
                    f"  - Error: Failed to install Playwright's {PLAYWRIGHT_BROWSER} for {project['name']}: {e}"
                )
