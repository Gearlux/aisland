"""The one reader of a workspace's ``.gitmodules``.

Three modules each parsed the file with a ``git config --get-regexp`` process of their own — the projects with
their ``active`` key (:func:`aisland.projects.submodule_active_map`), the ``workspace.toml`` rows with their url and
branch (:func:`aisland.workspace_file.submodules`), a local workspace's ``{path: name}``
(:func:`aisland.local_workspace.submodule_names`) — and each folded git's lines its own way. :func:`read` asks git
once for every key those readers use and folds the lines one way; the three are views over it.

Every ``aisland`` command reads the submodules (discovery lists the projects through them), so it stays ONE
process, never one per submodule: a workspace of 35 submodules spent 13.9 s in 36 processes on a loaded machine.

A key written twice in one submodule's section is read as git reads it: the last value (``git config --get``, and
``git submodule init`` registers the last ``path`` and ``url``); a submodule keeps the place of its first ``path`` line.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from aisland import gitcmd

logger = logging.getLogger(__name__)

#: The file, at the workspace root.
FILE_NAME = ".gitmodules"

#: The keys :func:`read` asks git for.
_KEYS = ("path", "url", "branch", "active")


@dataclass(frozen=True)
class Submodule:
    """One submodule ``.gitmodules`` declares: its name, its folder, its remote (``None`` without a ``url`` line),
    its branch (``None`` without a ``branch`` line) and whether it is active — ``False`` only for an explicit
    ``active = false`` (any case), git's default being active."""

    name: str
    path: str
    url: Optional[str] = None
    branch: Optional[str] = None
    active: bool = True


def read(root: Path) -> List[Submodule]:
    """Every submodule ``<root>/.gitmodules`` declares with a ``path``, in the order of their first ``path`` lines;
    ``[]`` without the file or when git cannot read it."""
    if not (root / FILE_NAME).is_file():
        logger.debug("no %s in %s: no submodules", FILE_NAME, root)
        return []
    pattern = rf"^submodule\..*\.({'|'.join(_KEYS)})$"
    listing = gitcmd.git(root, "config", "-f", FILE_NAME, "--get-regexp", pattern).stdout
    paths: Dict[str, str] = {}  # a dict keeps a key where it was first set: the order of the first path lines
    keys: Dict[str, Dict[str, str]] = {}
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        name, _, variable = key[len("submodule.") :].rpartition(".")
        if variable == "path":
            paths[name] = value
        else:
            keys.setdefault(name, {})[variable] = value
    logger.debug("read %s: %d submodule(s)", root / FILE_NAME, len(paths))
    return [
        Submodule(
            name,
            path,
            keys.get(name, {}).get("url"),
            keys.get(name, {}).get("branch"),
            keys.get(name, {}).get("active", "").strip().lower() != "false",
        )
        for name, path in paths.items()
    ]
