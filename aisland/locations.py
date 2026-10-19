"""Where aisland finds its workspace and keeps its state.

aisland is installed one of two ways, and each has its own rule:

* **From a checkout** — editable, the package at ``<workspace>/aisland/aisland/``, ``aisland/pyproject.toml``
  beside it. Every path follows the package's own location: the workspace is found from it and the state lives in
  ``<workspace>/aisland/`` (``data/``, ``bin/``, ``tools/``). The folder you run aisland in and ``AISLAND_WORKSPACE``
  change nothing — this is how aisland worked before it could be installed from a wheel.
* **From a wheel** — the package in ``site-packages``, where the package's location says nothing about any
  workspace. The workspace is ``AISLAND_WORKSPACE`` when set, else the git repository you run aisland in (the
  nearest folder holding a ``.git`` FOLDER — a submodule holds a ``.git`` file, so its superproject is found); the
  state lives in ``<workspace>/.aisland/``.

Standard library only: the pytest plugin (``aisland.pytest_stats``) imports this in every pytest start.
Nothing here raises: outside a workspace the root falls back to the folder you run in, and the CLI refuses the
command (:func:`workspace_found`, :func:`missing_workspace_message`) before anything is written there.
"""

import os
from pathlib import Path
from typing import List, Mapping, Optional

#: The installed package's folder: ``<workspace>/aisland/aisland`` in a checkout, ``site-packages/aisland`` from a wheel.
PACKAGE_DIR = Path(__file__).parent.absolute()
#: The folder holding the package: the aisland checkout, or ``site-packages``.
CHECKOUT_DIR = PACKAGE_DIR.parent
#: True when aisland runs from its checkout: its ``pyproject.toml`` sits beside the package (no wheel carries one).
FROM_CHECKOUT = (CHECKOUT_DIR / "pyproject.toml").is_file()
#: Names the workspace when aisland runs from a wheel; ignored from a checkout.
WORKSPACE_ENV = "AISLAND_WORKSPACE"
#: The workspace folder that holds the state when aisland runs from a wheel.
STATE_FOLDER = ".aisland"


def checkout_workspace_root(package_file: Optional[Path] = None) -> Path:
    """The workspace root of a checkout, found from the package's own location by searching upwards for a marker.

    ``package_file`` stands for a module of the package (default: ``manager.py``, as it always was).

    Marker choice is load-bearing. `TASKS.md` alone is NOT a workspace marker: the
    2026-07-26 backlog restructuring made backlogs PROJECT-OWNED, so every project
    dir carries one and an innermost-match walk stops at a PROJECT. Concretely,
    `aisland/TASKS.md` made this return ``<root>/aisland``, which then named the
    Jenkins orchestrator job "aisland" instead of "acme", set the global
    ``WORKSPACE_ROOT`` env to the aisland dir (so every job's `customWorkspace`
    pointed at ``<root>/aisland/<project>``), and looked for the workspace `.env`
    one level too deep ("No workspace .env found; skipping injection").

    The workspace root is the SUPERPROJECT — the single directory carrying
    ``.gitmodules`` (a submodule has a `.git` *file* and no `.gitmodules`). Prefer
    that; when a checkout genuinely has no submodules, fall back to the OUTERMOST
    `TASKS.md` — never the innermost — so a nested project can't win.
    """
    start = (package_file or PACKAGE_DIR / "manager.py").resolve()
    # start itself is a file; its parents are the candidate directories.
    chain: List[Path] = list(start.parents)[:6]

    # 1. The superproject: the one directory with .gitmodules (+ a TASKS.md, so a
    #    stray .gitmodules elsewhere in the tree can't hijack the result).
    for cand in chain:
        if (cand / ".gitmodules").is_file() and (cand / "TASKS.md").exists():
            return cand

    # 2. No submodules in this checkout: OUTERMOST TASKS.md wins over a project's.
    with_tasks = [cand for cand in chain if (cand / "TASKS.md").exists()]
    if with_tasks:
        return with_tasks[-1]

    # 3. Outermost .git (the superproject's directory, not a submodule's file).
    with_git = [cand for cand in chain if (cand / ".git").exists()]
    if with_git:
        return with_git[-1]

    # 4. Absolute fallback: <root>/aisland/aisland/manager.py -> <root>.
    return start.parents[2]


def find_workspace(start: Path) -> Optional[Path]:
    """The nearest of ``start`` and its parents holding a ``.git`` folder, or None outside any git repository."""
    for candidate in [start, *start.parents]:
        if (candidate / ".git").is_dir():
            return candidate
    return None


def wheel_workspace_root(cwd: Path, environ: Mapping[str, str]) -> Optional[Path]:
    """The workspace of aisland installed from a wheel: ``AISLAND_WORKSPACE`` (an empty value counts as unset), else
    the git repository ``cwd`` is in; None when neither names one. The named folder is not checked here."""
    named = environ.get(WORKSPACE_ENV, "")
    if named:
        return Path(named).expanduser().resolve()
    return find_workspace(cwd.resolve())


def workspace_root() -> Path:
    """The workspace aisland works on (see the module docstring); never raises."""
    if FROM_CHECKOUT:
        return checkout_workspace_root()
    return wheel_workspace_root(Path.cwd(), os.environ) or Path.cwd().resolve()


def state_dir() -> Path:
    """The folder holding aisland's ``data/``, ``bin/`` and ``tools/``."""
    if FROM_CHECKOUT:
        return CHECKOUT_DIR
    return workspace_root() / STATE_FOLDER


def workspace_found() -> bool:
    """Whether there is a workspace to work on: always from a checkout; from a wheel, a folder named or found."""
    if FROM_CHECKOUT:
        return True
    root = wheel_workspace_root(Path.cwd(), os.environ)
    return root is not None and root.is_dir()


def missing_workspace_message() -> str:
    """What to tell the user when :func:`workspace_found` is False — the variable's value, or how to get one."""
    named = os.environ.get(WORKSPACE_ENV, "")
    if named:
        return (
            f"{WORKSPACE_ENV}={named} is not a folder. Set it to your workspace, or unset it to use the git "
            "repository you run aisland in."
        )
    return (
        f"No workspace here: {Path.cwd().resolve()} is not inside a git repository. Run aisland inside your "
        f"workspace, set {WORKSPACE_ENV} to it, or create one with `aisland workspace init <folder>`."
    )
