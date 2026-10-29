"""The workspace and its projects: the root, the name, the project folders, and the order they install in."""

import re
import subprocess
import tomllib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aisland import locations


def get_workspace_root() -> Path:
    """The workspace root: found from the package's location in a checkout, from where aisland runs when it comes
    from a wheel (:mod:`aisland.locations`, which holds the marker rule)."""
    return locations.workspace_root()


def get_workspace_name(root: Optional[Path] = None) -> str:
    """The workspace's name: ``[workspace] name`` in its ``workspace.toml``, else its origin's repository name, else
    its folder's name. It names the editor file, the Jenkins jobs and the how-to's ``--prompt``."""
    from aisland import workspace_file  # local: workspace_file imports discovery, which imports this module

    root = root or get_workspace_root()
    named = workspace_file.name_of(root)
    if named:
        return named
    try:  # no origin, or no git: the folder names it
        output = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        output = ""
    # The repository's name: its URL's last part, `.git` dropped from its end only (https, ssh and scp-style:
    # https://github.com/acme/models.git, git@github.com:acme/my.github-tools.git, git@host:models.git).
    name = re.split(r"[/:]", output.rstrip("/"))[-1].removesuffix(".git") if output else ""
    if name:
        return name
    return root.name


def discover_projects(root: Path, workspace_root: Optional[Path] = None) -> List[Path]:
    """Recursively discover pyproject.toml files, but only in directories that are identified as projects.

    A link to a folder inside the workspace (``workspace_root``, default ``root``) is skipped: it is a second name for
    a folder found under its own (:func:`aisland.locations.is_alias`)."""
    workspace_root = workspace_root or root
    projects = []
    # Skip standard ignored directories
    if any(p in str(root) for p in [".venv", "aisland/data"]):
        return []

    for item in root.iterdir():
        if item.is_dir() and not locations.is_alias(item, workspace_root):
            pyproject = item / "pyproject.toml"
            if pyproject.exists():
                projects.append(pyproject)
                # If it's a project, also check for sub-projects (submodules)
                projects.extend(discover_projects(item, workspace_root))
    return projects


def get_project_metadata(pyproject_path: Path) -> dict:
    """Extract name and dependencies from pyproject.toml."""
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
        project_info = data.get("project", {})
        name = project_info.get("name")
        deps = project_info.get("dependencies", [])
        # Also include optional-dependencies if they are internal
        optional_deps = project_info.get("optional-dependencies", {})
        for extra_deps in optional_deps.values():
            deps.extend(extra_deps)

        return {
            "name": name,
            "path": pyproject_path.parent,
            "dependencies": deps,
            "has_dev": "dev" in optional_deps,
        }


def internal_dependencies(projects: List[dict]) -> Dict[str, List[str]]:
    """Map each project name to the names of the workspace projects it depends on."""
    project_names = {p["name"] for p in projects}
    adj: Dict[str, List[str]] = {p["name"]: [] for p in projects}
    for p in projects:
        for dep in p["dependencies"]:
            # Extract package name from dependency string (e.g., 'loggair>=0.1.0' -> 'loggair')
            dep_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
            if dep_name in project_names and dep_name != p["name"]:
                adj[p["name"]].append(dep_name)
    return adj


def install_groups(projects: List[dict]) -> List[List[dict]]:
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
    groups: List[List[dict]] = []

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


def get_install_order(workspace_root: Path) -> List[dict]:
    """Determine the correct installation order using topological sort based on workspace dependencies."""
    pyprojects = discover_projects(workspace_root)
    return dependency_order([get_project_metadata(p) for p in pyprojects])


def dependency_order(projects: List[dict]) -> List[dict]:
    """``projects`` with every workspace dependency before its dependents; otherwise in the order given."""
    adj = internal_dependencies(projects)

    # Topological sort (Kahn's algorithm or DFS)
    visited = set()
    temp_visited = set()
    order = []

    def visit(name: str) -> None:
        if name in temp_visited:
            # Circular dependency detected - just break and continue for now
            return
        if name not in visited:
            temp_visited.add(name)
            for neighbor in adj.get(name, []):
                visit(neighbor)
            temp_visited.remove(name)
            visited.add(name)
            order.append(name)

    for p in projects:
        visit(p["name"])

    # Map names back to metadata
    name_to_meta = {p["name"]: p for p in projects}
    return [name_to_meta[name] for name in order]


def submodule_active_map(workspace_root: Path) -> List[Tuple[str, str, bool]]:
    """Return ``(name, path, active)`` for every submodule declared in ``.gitmodules``.

    ``active`` is the declared intent: ``False`` only when the entry has an explicit
    ``active = false`` key; otherwise ``True`` (git's default — a submodule with no ``active``
    key is active). This is the shared, committed source of truth for which submodules a
    checkout should carry; an ``active = false`` marks one this workspace deliberately skips
    (e.g. an optional or access-gated remote).
    """
    gitmodules = workspace_root / ".gitmodules"
    if not gitmodules.exists():
        return []
    # ONE git process for every submodule's path and `active` key: every aisland command reads this map, and a
    # process per submodule cost 36 of them (13.9 s on a loaded machine) in a workspace of 35.
    listing = subprocess.run(
        ["git", "config", "-f", str(gitmodules), "--get-regexp", r"^submodule\..*\.(path|active)$"],
        cwd=workspace_root,
        text=True,
        capture_output=True,
    ).stdout
    paths: Dict[str, str] = {}
    active: Dict[str, str] = {}
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        name, _, variable = key[len("submodule.") :].rpartition(".")
        if variable == "path":
            paths.setdefault(name, value)
        else:
            active[name] = value.strip()  # git's `--get` answers the last value written; so does this
    return [(name, path, active.get(name, "").lower() != "false") for name, path in paths.items()]


def workspace_folders(workspace_root: Path) -> List[Tuple[str, str, bool]]:
    """Return ``(name, path, active)`` for every project of the workspace.

    With ``.gitmodules``, its submodules (:func:`submodule_active_map`). Without one, every direct, non-hidden
    subfolder holding a ``pyproject.toml``, active and named by its folder — the workspace is read as if each were
    a submodule (`aisland.locations.holds_projects` is the same test). A link to another folder of the workspace is
    none of them (:func:`aisland.locations.is_alias`).
    """
    if (workspace_root / ".gitmodules").is_file():
        return submodule_active_map(workspace_root)
    return [
        (child.name, child.name, True)
        for child in sorted(workspace_root.iterdir(), key=lambda c: c.name)
        if child.is_dir()
        and not child.name.startswith(".")
        and (child / "pyproject.toml").is_file()
        and not locations.is_alias(child, workspace_root)
    ]


def set_submodule_active(workspace_root: Path, name: str, active: bool) -> None:
    """Record the per-machine activation decision in ``.git/config`` (never committed)."""
    subprocess.run(
        ["git", "config", "--local", f"submodule.{name}.active", "true" if active else "false"],
        cwd=workspace_root,
        check=False,
    )
