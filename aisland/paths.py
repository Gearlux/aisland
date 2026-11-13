"""Where aisland keeps its state: the folders under :func:`aisland.locations.state_dir` — ``bin/``, ``data/``
and ``tools/`` — and the package's own folder.

Each is a function, asked when it is needed: the state folder follows the workspace, which a long-running process
(the MCP server) or a test may point elsewhere after aisland is imported. A module calls them through this module
(``paths.data_dir()``), so ``locations.state_dir`` is the one place a test moves them all.
"""

from pathlib import Path

from aisland import locations

#: The package's own folder, which holds the files it reads (the workspace-file template, the shell start-up file).
SERVICES_DIR = locations.PACKAGE_DIR


def state_dir() -> Path:
    """The state folder: the aisland checkout (aisland/aisland/paths.py -> aisland/), or ``<workspace>/.aisland`` when
    aisland runs from a wheel (:mod:`aisland.locations`)."""
    return locations.state_dir()


def bin_dir() -> Path:
    """The programs aisland downloads, and the links to the tools it unpacks."""
    return state_dir() / "bin"


def data_dir() -> Path:
    """Each service's data and log, the supervisor's document, the backups, the reports."""
    return state_dir() / "data"


def tools_dir() -> Path:
    """The userspace tools aisland unpacks, each in a folder of its own."""
    return state_dir() / "tools"
