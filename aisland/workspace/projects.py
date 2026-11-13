"""The workspace and its projects: the root, the name, the project folders, and the order they install in."""

from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, TypedDict

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import NormalizedName, canonicalize_name

from aisland import gitcmd, locations, machine_config
from aisland import pyproject as pyproject_toml
from aisland.workspace import file as workspace_file
from aisland.workspace import gitmodules


def get_workspace_root() -> Path:
    """The workspace root: found from the package's location in a checkout, from where aisland runs when it comes
    from a wheel (:mod:`aisland.locations`, which holds the marker rule)."""
    return locations.workspace_root()


def get_workspace_name(root: Optional[Path] = None) -> str:
    """The workspace's name: ``[workspace] name`` in its ``workspace.toml``, else its origin's repository name, else
    its folder's name. It names the editor file, the Jenkins jobs and the how-to's ``--prompt``."""
    root = root or get_workspace_root()
    named = workspace_file.name_of(root)
    if named:
        return named
    url = gitcmd.remote_url(root)  # no origin, or no git: the folder names it
    return (workspace_file.repository_name(url) if url else None) or root.name


def discover_projects(root: Path, workspace_root: Optional[Path] = None) -> List[Path]:
    """Recursively discover pyproject.toml files, but only in directories that are identified as projects.

    A hidden folder (``.venv``, ``.git``, a tool's cache) is never a project, nor is a link to a folder inside the
    workspace (``workspace_root``, default ``root``): it is a second name for a folder found under its own
    (:func:`aisland.locations.is_alias`). Only the folders' own names are read, never the path above the workspace."""
    workspace_root = workspace_root or root
    projects = []
    for item in locations.project_folders(root, workspace_root):
        projects.append(item / "pyproject.toml")
        projects.extend(discover_projects(item, workspace_root))  # a project's own sub-projects (submodules)
    return projects


class ProjectMetadata(TypedDict):
    """What :func:`get_project_metadata` reads of a project's ``pyproject.toml``: its distribution name, its folder,
    every requirement (its extras' included) and whether it has a ``dev`` extra."""

    name: str
    path: Path
    dependencies: List[str]
    has_dev: bool


def get_project_metadata(pyproject_path: Path) -> ProjectMetadata:
    """The distribution name, folder and every requirement (its extras' included) of one ``pyproject.toml``. A file
    that is no valid TOML raises :class:`aisland.pyproject.PyprojectError` naming it — a sub-project's too (a
    template's placeholders), which tomllib's own error never named."""
    data = pyproject_toml.parse(pyproject_path.read_text(), pyproject_path)
    project_info = data.get("project", {})
    optional_deps = project_info.get("optional-dependencies", {})
    deps = list(project_info.get("dependencies", []))  # a copy: the extras below never reach the loaded table
    for extra_deps in optional_deps.values():
        deps.extend(extra_deps)
    return {
        "name": project_info.get("name") or "",
        "path": pyproject_path.parent,
        "dependencies": deps,
        "has_dev": "dev" in optional_deps,
    }


def requirement_name(spec: str) -> Optional[NormalizedName]:
    """The normalized distribution name a PEP 508 requirement names (``Foo_Bar[x]~=1.0; python_version>'3'`` →
    ``foo-bar``), or ``None`` for a string that is no requirement."""
    try:
        return canonicalize_name(Requirement(spec).name)
    except InvalidRequirement:
        return None


def internal_dependencies(projects: List[ProjectMetadata]) -> Dict[str, List[str]]:
    """Map each project name to the names of the workspace projects it depends on, each once, in the order its
    requirements name them. Names match as PEP 503 normalizes them, so ``Foo_Bar`` requires ``foo-bar``."""
    by_canonical = {canonicalize_name(p["name"]): p["name"] for p in projects if p["name"]}
    adj: Dict[str, List[str]] = {p["name"]: [] for p in projects}
    for p in projects:
        for spec in p["dependencies"]:
            name = requirement_name(spec)
            dependency = by_canonical.get(name) if name else None
            if dependency and dependency != p["name"] and dependency not in adj[p["name"]]:
                adj[p["name"]].append(dependency)
    return adj


def install_groups(projects: List[ProjectMetadata]) -> List[List[ProjectMetadata]]:
    """Group projects into install units, dependencies first; a dependency CYCLE is one unit.

    Tarjan's strongly-connected-components: a cycle (``a[dev]`` requires ``b`` and ``b`` requires
    ``a``) cannot be installed one project at a time — uv
    resolves the missing half from the package index and fails — so its members must go through
    ONE ``uv pip install -e A -e B``. Every acyclic project is a singleton unit. Tarjan emits a
    component only after everything it depends on, which is exactly install order.
    """
    adj = internal_dependencies(projects)
    by_name = {p["name"]: p for p in projects}
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: set[str] = set()
    groups: List[List[ProjectMetadata]] = []

    def visit(name: str) -> None:
        index[name] = low[name] = len(index)
        stack.append(name)
        on_stack.add(name)
        for dep in adj[name]:
            if dep not in index:
                visit(dep)
                low[name] = min(low[name], low[dep])
            elif dep in on_stack:
                low[name] = min(low[name], index[dep])
        if low[name] == index[name]:
            members: List[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                members.append(member)
                if member == name:
                    break
            groups.append([by_name[n] for n in reversed(members)])

    for p in projects:
        if p["name"] not in index:
            visit(p["name"])
    return groups


def dependency_order(projects: List[ProjectMetadata]) -> List[ProjectMetadata]:
    """``projects`` with every workspace dependency before its dependents; otherwise in the order given. A dependency
    cycle's members stand together (:func:`install_groups`, which setup installs as one unit)."""
    return [project for group in install_groups(projects) for project in group]


class ProjectFolder(NamedTuple):
    """A project of the workspace by its folder: its ``name``, its ``path`` relative to the root, and whether this
    checkout carries it (``active``). A tuple, so ``for name, path, active in …`` reads it as before."""

    name: str
    path: str
    active: bool


def submodule_active_map(workspace_root: Path) -> List[ProjectFolder]:
    """Return a :class:`ProjectFolder` for every submodule declared in ``.gitmodules``.

    ``active`` is the declared intent: ``False`` only when the entry has an explicit
    ``active = false`` key; otherwise ``True`` (git's default — a submodule with no ``active``
    key is active). This is the shared, committed source of truth for which submodules a
    checkout should carry; an ``active = false`` marks one this workspace deliberately skips
    (e.g. an optional or access-gated remote).
    """
    return [
        ProjectFolder(submodule.name, submodule.path, submodule.active) for submodule in gitmodules.read(workspace_root)
    ]


def workspace_folders(workspace_root: Path) -> List[ProjectFolder]:
    """Return a :class:`ProjectFolder` for every project of the workspace.

    With ``.gitmodules``, its submodules (:func:`submodule_active_map`). Without one, every project folder of the
    root (:func:`aisland.locations.project_folders`), active and named by its folder — the workspace is read as if
    each were a submodule.
    """
    if (workspace_root / ".gitmodules").is_file():
        return submodule_active_map(workspace_root)
    return [ProjectFolder(child.name, child.name, True) for child in locations.project_folders(workspace_root)]


def set_submodule_active(workspace_root: Path, name: str, active: bool) -> None:
    """Record the per-machine activation decision in ``.git/config`` (never committed)."""
    machine_config.put(workspace_root, f"submodule.{name}.active", active)


#: What emptying a project folder may delete because something makes it again: the caches a run writes, a venv or
#: ``node_modules`` an install recreates, and what ``aisland setup`` puts in every project (:mod:`aisland.git_exclude`:
#: ``pyrightconfig.json``, ``*.egg-info/``, the harness's ``.claude/commands``). Only names no data folder carries: a
#: ``build/`` or ``dist/`` may hold what a person keeps. Anything else git ignores there is kept (:func:`ignored_content`).
_REMADE_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coverage",
        ".DS_Store",
        ".venv",
        "node_modules",
        "pyrightconfig.json",
    }
)
_REMADE_PATHS = frozenset({".claude/commands"})


def ignored_content(folder: Path) -> List[str]:
    """The git-ignored paths of the checkout ``folder`` that emptying it would delete for good, sorted.

    ``git submodule deinit`` without ``-f`` refuses a checkout with uncommitted changes, yet deletes what git ignores
    with the rest of the folder: a run's output, a checkpoint, a project's own ``.env``. A path something makes again
    is left out: one inside a folder :data:`_REMADE_NAMES` names or a ``*.egg-info`` folder, or one of
    :data:`_REMADE_PATHS`. A folder git cannot read lists nothing."""
    listed = gitcmd.read(folder, "status", "--porcelain", "--ignored=matching", "-z") or ""
    kept = []
    for entry in listed.split("\0"):
        if not entry.startswith("!! "):
            continue
        path = entry[3:].rstrip("/")
        parts = path.split("/")
        if path in _REMADE_PATHS or any(part in _REMADE_NAMES or part.endswith(".egg-info") for part in parts):
            continue
        kept.append(path)
    return sorted(kept)


def ignored_content_refusal(project: str, folder: str, paths: Sequence[str]) -> str:
    """Why ``project``'s folder is not emptied: the ignored ``paths`` it holds (:func:`ignored_content`), and the
    command that moves them into a backup first."""
    shown = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
    return (
        f"{project}: {folder} holds git-ignored files emptying it would delete for good ({shown}); move them into "
        f"a backup with `aisland clean {project}`, then run this again"
    )
