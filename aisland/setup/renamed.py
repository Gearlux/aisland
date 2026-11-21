"""What a renamed project left installed, uninstalled before ``aisland setup`` installs it (``manager._run_installs``).

``uv pip install -e <folder>`` installs the distribution the folder's ``pyproject.toml`` names NOW, beside whatever was
installed from that folder before. After a rename in place two editable distributions own one package and one script,
and uninstalling the old one afterwards deletes the shared script; after a rename of the folder too, the old
distribution stays, installed from a folder that is gone. Setup uninstalls both first, one line each saying why:

- a distribution installed from a project's folder under another name than the folder's ``pyproject.toml`` has;
- a distribution installed from a folder directly in the workspace root that no longer exists.

A distribution installed from anywhere else is never touched: outside the workspace, deeper in it, or from a folder
still there (a deactivated project's, which ``git submodule deinit`` leaves empty).
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, List

from packaging.utils import canonicalize_name

from aisland import console
from aisland.workspace.projects import ProjectMetadata


def editable_installs(workspace_root: Path, uv_cmd: str) -> Dict[str, Path]:
    """Each editable distribution of the venv setup installs into, and the folder it was installed from.

    Asked of the same uv from the same folder as the installs, so it reads the same venv. A uv that cannot list them
    gives none: the install that follows runs the same uv and reports the failure itself."""
    listing = subprocess.run(
        [uv_cmd, "pip", "list", "--editable", "--format", "json"], cwd=workspace_root, capture_output=True, text=True
    )
    if listing.returncode != 0:
        return {}
    return {entry["name"]: Path(entry["editable_project_location"]) for entry in json.loads(listing.stdout)}


def renamed_in_place(installed: Dict[str, Path], projects: List[ProjectMetadata]) -> Dict[str, Dict[str, str]]:
    """Per project, each distribution installed from its folder under another name, with why it goes."""
    found: Dict[str, Dict[str, str]] = {}
    for project in projects:
        folder, name = Path(project["path"]), project["name"]
        for old, location in installed.items():
            if location.resolve() == folder.resolve() and canonicalize_name(old) != canonicalize_name(name):
                found.setdefault(name, {})[old] = f"installed from {folder}, whose project is now {name}"
    return found


def gone_folders(installed: Dict[str, Path], workspace_root: Path) -> Dict[str, str]:
    """Each distribution installed from a folder directly in the workspace root that no longer exists, with why it
    goes."""
    root = workspace_root.resolve()
    return {
        old: f"installed from {location}, which no longer exists"
        for old, location in installed.items()
        if location.resolve().parent == root and not location.exists()
    }


def uninstall(workspace_root: Path, uv_cmd: str, reasons: Dict[str, str]) -> None:
    """Say why each distribution of ``reasons`` goes, then uninstall them all with one uv command (nothing when there
    are none); a failure raises ``subprocess.CalledProcessError``."""
    if not reasons:
        return
    for old, reason in reasons.items():
        console.echo(f"==> Uninstalling {old}: {reason}.")
    subprocess.check_call([uv_cmd, "pip", "uninstall", *reasons], cwd=workspace_root)
