"""Where aisland finds its workspace and keeps its state.

aisland is installed one of two ways, and each has its own rule:

* **From a checkout** — editable, the package at ``<workspace>/aisland/aisland/``, ``aisland/pyproject.toml``
  beside it, and a workspace above that lists the checkout as one of its projects (:func:`listing_workspace`).
  Every path follows the package's own location: the workspace is the one that lists it and the state lives in
  ``<workspace>/aisland/`` (``data/``, ``bin/``, ``tools/``). The folder you run aisland in and ``AISLAND_WORKSPACE``
  change nothing — this is how aisland worked before it could be installed from a wheel.
* **From a wheel** — the package in ``site-packages``, where the package's location says nothing about any
  workspace; and so is a clone of aisland no workspace lists (a contributor's, CI's), installed editable or not. The workspace is ``AISLAND_WORKSPACE`` when set, else the one the folder you run aisland in belongs to
  (:func:`find_workspace`: a superproject, a folder of projects, a project's parent, a fresh repository); the
  state lives in ``<workspace>/.aisland/``.

Standard library only: the pytest plugin (``aisland.pytest_stats``) imports this in every pytest start.
Nothing here raises: outside a workspace the root falls back to the folder you run in, and the CLI refuses the
command (:func:`workspace_found`, :func:`missing_workspace_message`) before anything is written there.
"""

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Dict, List, Literal, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: The installed package's folder: ``<workspace>/aisland/aisland`` in a checkout, ``site-packages/aisland`` from a wheel.
PACKAGE_DIR = Path(__file__).parent.absolute()
#: The folder holding the package: the aisland checkout, or ``site-packages``.
CHECKOUT_DIR = PACKAGE_DIR.parent
#: How far above a checkout the workspace listing it is looked for.
_LISTING_DEPTH = 5
_GITMODULES_PATH = re.compile(r"^\s*path\s*=\s*(.+?)\s*$", re.MULTILINE)


def _lists(folder: Path, relative: str) -> bool:
    """Whether ``folder``'s ``.gitmodules`` has a submodule at ``relative``, or its ``workspace.toml`` a project in it.
    A file that cannot be read lists nothing."""
    try:
        gitmodules = (folder / ".gitmodules").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        gitmodules = ""
    if relative in (match.strip().strip('"') for match in _GITMODULES_PATH.findall(gitmodules)):
        return True
    try:
        projects = tomllib.loads((folder / "workspace.toml").read_text(encoding="utf-8")).get("project", {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return isinstance(projects, dict) and any(
        isinstance(table, dict) and table.get("path", name) == relative for name, table in projects.items()
    )


def listing_workspace(checkout: Path) -> Optional[Path]:
    """The workspace that lists ``checkout`` as one of its projects — the nearest folder above it whose
    ``.gitmodules`` has a submodule at its path, or whose ``workspace.toml`` has a project in it — or None for a
    clone of its own. A ``TASKS.md`` or a repository above it lists nothing: a contributor's clone in ``~/code`` is no
    workspace's project."""
    for folder in checkout.parents[:_LISTING_DEPTH]:
        if _lists(folder, checkout.relative_to(folder).as_posix()):
            return folder
    return None


def runs_as_checkout(checkout: Path) -> bool:
    """Whether aisland at ``checkout`` runs as a checkout: its ``pyproject.toml`` beside the package (no wheel carries
    one) and a workspace that lists it. Any other clone works like a wheel, on the workspace it is run in."""
    try:
        beside = (checkout / "pyproject.toml").is_file()
    except OSError:
        return False
    return beside and listing_workspace(checkout) is not None


#: True when aisland runs from its checkout in the workspace that lists it (:func:`runs_as_checkout`).
FROM_CHECKOUT = runs_as_checkout(CHECKOUT_DIR)
#: Names the workspace when aisland runs from a wheel; ignored from a checkout.
WORKSPACE_ENV = "AISLAND_WORKSPACE"
#: The workspace folder that holds the state when aisland runs from a wheel.
STATE_FOLDER = ".aisland"
#: The files that make a folder an aisland workspace (:func:`is_workspace`): the one aisland writes it down in, and
#: git's list of its submodules.
WORKSPACE_MARKERS = ("workspace.toml", ".gitmodules")


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
    # 0. The workspace that lists the checkout (the one that makes it run as a checkout at all).
    listed = listing_workspace(start.parent.parent)
    if listed is not None:
        return _said(listed, f"from the checkout: {listed} lists it as a project")
    # start itself is a file; its parents are the candidate directories.
    chain: List[Path] = list(start.parents)[:6]

    # 1. The superproject: the one directory with .gitmodules (+ a TASKS.md, so a
    #    stray .gitmodules elsewhere in the tree can't hijack the result).
    for cand in chain:
        if (cand / ".gitmodules").is_file() and (cand / "TASKS.md").exists():
            return _said(cand, "from the checkout: it holds .gitmodules and TASKS.md")

    # 2. No submodules in this checkout: OUTERMOST TASKS.md wins over a project's.
    with_tasks = [cand for cand in chain if (cand / "TASKS.md").exists()]
    if with_tasks:
        return _said(with_tasks[-1], "from the checkout: no .gitmodules above it, the outermost TASKS.md")

    # 3. Outermost .git (the superproject's directory, not a submodule's file).
    with_git = [cand for cand in chain if (cand / ".git").exists()]
    if with_git:
        return _said(with_git[-1], "from the checkout: no .gitmodules or TASKS.md above it, the outermost .git")

    # 4. Absolute fallback: <root>/aisland/aisland/manager.py -> <root>.
    return _said(start.parents[2], "from the checkout: no marker above it, two folders above the package")


#: The last answer :func:`_said` logged: every command asks for the root many times, the log says it once.
_last_said: Optional[Tuple[Path, str]] = None


def _said(root: Path, why: str) -> Path:
    """``root``, logged at debug with ``why`` when it differs from the last answer logged."""
    global _last_said
    if _last_said != (root, why):
        _last_said = (root, why)
        logger.debug("workspace root %s (%s)", root, why)
    return root


#: What :func:`_is` asks of a path.
PathKind = Literal["file", "folder", "link"]


def _is(path: Path, kind: PathKind) -> bool:
    """Whether ``path`` is a file, a folder or a symbolic link — False when it cannot be looked at. pathlib answers
    False for a missing path but raises ``PermissionError`` for one inside a folder you may not enter
    (``/tmp/snap-private-tmp`` on Linux), and from a wheel every command looks for its workspace on import."""
    try:
        if kind == "file":
            return path.is_file()
        if kind == "folder":
            return path.is_dir()
        return path.is_symlink()
    except OSError:
        return False


def is_alias(folder: Path, workspace: Path) -> bool:
    """Whether ``folder`` is a symbolic link to a folder inside ``workspace`` — a second name for a folder the
    workspace already holds (``ml -> machine_learning``), never a project of its own: every reader of the workspace's
    projects skips it, and the folder it names is found under its own name. A link to a folder OUTSIDE the workspace
    is no alias: it is how a checkout kept elsewhere joins the workspace. Both sides are resolved, so a workspace
    entered through a link of its own (``~/acme -> /build/acme``) tells its aliases all the same."""
    if not _is(folder, "link"):
        return False
    target = folder.resolve()
    inside = workspace.resolve()
    return target == inside or inside in target.parents


def alias_names(workspace: Path) -> Dict[Path, str]:
    """The short name each folder of ``workspace`` goes by: for every non-hidden link in the workspace's root that is
    an alias (:func:`is_alias`) of a folder, that folder's resolved path -> the link's name. Of two links to one
    folder the first by name gives it. The generated VS Code workspace file shows a project under this name."""
    names: Dict[Path, str] = {}
    try:
        children = sorted(workspace.iterdir(), key=lambda c: c.name)
    except OSError:
        return names
    for child in children:
        if not child.name.startswith(".") and _is(child, "folder") and is_alias(child, workspace):
            names.setdefault(child.resolve(), child.name)
    return names


def holds_projects(folder: Path) -> bool:
    """Whether a direct, non-hidden subfolder of ``folder`` holds a ``pyproject.toml`` (a workspace's projects). A
    subfolder that cannot be looked into holds none."""
    try:
        children = list(folder.iterdir())
    except OSError:
        return False
    return any(
        not child.name.startswith(".") and _is(child, "folder") and _is(child / "pyproject.toml", "file")
        for child in children
    )


def find_workspace(start: Path) -> Optional[Path]:
    """The workspace ``start`` is in, or None. From ``start`` upwards, the first folder that

    1. holds ``.gitmodules`` — a superproject (from inside a submodule too);
    2. is ``start`` itself and holds projects (:func:`holds_projects`) — a folder of projects, run from its root;
    3. holds a ``pyproject.toml`` — a project, so the workspace is its PARENT (a project that is its own git clone
       is never the workspace);
    4. holds a ``.git`` folder — a repository with no project yet (what ``aisland workspace init`` makes).

    Rule 2 looks at ``start`` only: applied above it, a home folder holding one project would become the workspace
    of every folder under it. A folder that cannot be looked into holds none of these, and the search goes on above
    it.
    """
    found = _find_workspace(start)
    return found[0] if found else None


def _find_workspace(start: Path) -> Optional[Tuple[Path, str]]:
    """:func:`find_workspace`'s answer and the rule that gave it, in words for the debug log."""
    for candidate in [start, *start.parents]:
        if _is(candidate / ".gitmodules", "file"):
            return candidate, f"{candidate} holds .gitmodules"
        if candidate == start and holds_projects(candidate):
            return candidate, f"{candidate}, the folder aisland runs in, holds projects"
        if _is(candidate / "pyproject.toml", "file"):
            return candidate.parent, f"{candidate} is a project, the workspace its parent"
        if _is(candidate / ".git", "folder"):
            return candidate, f"{candidate} is a repository"
    return None


def wheel_workspace_root(cwd: Path, environ: Mapping[str, str]) -> Optional[Path]:
    """The workspace of aisland installed from a wheel: ``AISLAND_WORKSPACE`` (an empty value counts as unset), else
    the git repository ``cwd`` is in; None when neither names one. The named folder is not checked here."""
    named = environ.get(WORKSPACE_ENV, "")
    if named:
        return _said(Path(named).expanduser().resolve(), f"from a wheel: {WORKSPACE_ENV} names it")
    found = _find_workspace(cwd.resolve())
    return _said(found[0], f"from a wheel: {found[1]}") if found else None


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


def is_workspace(folder: Path, environ: Mapping[str, str] = os.environ) -> bool:
    """Whether ``folder`` is an aisland workspace: from a checkout always; from a wheel, a folder holding one of
    :data:`WORKSPACE_MARKERS` (what ``aisland workspace init`` and ``aisland setup`` write) or the one
    ``AISLAND_WORKSPACE`` names.

    :func:`find_workspace` also answers for a lone repository (a project's parent, a fresh ``git init``), so that
    ``workspace init`` and ``setup`` can start there; what writes into a workspace uninvited — the pytest plugin's
    record — asks this first, so a repository of its own stays untouched.
    """
    if FROM_CHECKOUT:
        return True
    named = environ.get(WORKSPACE_ENV, "")
    if named and Path(named).expanduser().resolve() == folder.resolve():
        return True
    return any(_is(folder / marker, "file") for marker in WORKSPACE_MARKERS)


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
        f"No workspace here: {Path.cwd().resolve()} is in no git repository and holds no project. Run aisland in "
        f"your workspace or one of its projects, set {WORKSPACE_ENV} to it, or create one with "
        "`aisland workspace init <folder>`."
    )
