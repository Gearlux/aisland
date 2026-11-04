"""Wheels for a project installed OUTSIDE the workspace (``aisland wheels <project>``).

A project that is not on PyPI (navigaitor) depends on workspace projects of which some are on PyPI
and some are not. A machine without the workspace can install the first kind from PyPI only;
this module builds the second kind as wheels into ``<project>/wheels/``, where
``pip install --find-links wheels -e .`` finds them, and then answers whether the PyPI half is
recent enough — the wheels are built from this checkout, so they may use code no release holds.

The run, in order:

1. **What the project needs** (:func:`dependency_closure`): the workspace projects its
   ``[project] dependencies`` reach, their own dependencies and the extras a requirement names
   followed, every environment marker INCLUDED — the wheels serve another machine, whose
   platform is not this one's.
2. **On PyPI or not** (:func:`pypi_state`): published = a version under the name whose metadata
   points at ``github.com/<owner>/`` — the owner of the project's own remote — and has a file that is not yanked. Pre-releases COUNT
   (recordstream 0.1.0a2: pip takes it when no final release exists), which is where this
   question differs from :func:`aisland.pypi.released_on_pypi`, the CI scaffolder's. A
   stranger's package under the same name is not ours — and is reported, because pip takes the
   higher version of the two.
3. **The wheels** (:func:`export_head`, :func:`build_wheel`): each from an export of the
   project's last COMMIT, never its folder — a setuptools ``build/lib`` left in a checkout puts
   files the project deleted long ago into the wheel (measured 2026-10-08). A project with
   uncommitted changes is refused, so no one ships a change believing it is in — unless the run is
   told ``--force`` (:func:`export_worktree`): such a project is then built from its files as they
   are, those git ignores left out, its commit written ``<sha>+uncommitted`` and its changed files
   listed in the report; ``--force`` never goes with ``--commit``, since no commit reproduces those
   wheels. The folder's old ``*.whl`` go; anything else in it stays. Unchanged sources rebuild byte
   for byte, so git sees no change.
4. **The PyPI half, twice** (:func:`wheel_differences`, :func:`install_and_import`): each
   published dependency's PyPI wheel against the one its checkout builds, file by file ("behind");
   then the project installed in an EMPTY venv from its wheels plus PyPI, and every module of
   the project and of its wheels imported — not only the packages: a module no package imports
   is checked too, because ``import <package>`` never reaches it. A failed import names the project whose module
   is missing — the one to release first (measured 2026-10-08: a module needed
   ``recordstream.draws``, which recordstream 0.1.0a2 lacks). ``--no-check`` skips both and changes
   nothing else the run writes; the report then says ``checked: false``, and ``--commit`` is refused.

``--include-project`` builds the project's OWN wheel too, under the same rules (its last commit; refused when
dirty, unless ``--force``). ``wheels.yaml`` lists it last; while its ``install:`` is true, ``install.py`` installs
that wheel in place of the project's folder, editable, and the empty-venv check installs and imports the wheel.
Nothing then needs the project's source, so its zip installs from any folder.

What is built is the run's SCOPE (:data:`WheelsScope`): by default the projects above that PyPI
lacks; ``--include-published`` every project the target needs that has a checkout here, those on
PyPI too (the wheel holds the commit, not the release); ``--all`` every workspace project that has a
checkout here, needed or not. Beside the wheels go ``wheels.yaml`` — each wheel, its commit, whether
the target needs it, and ``install:``, the user's choice, kept across reruns — and ``install.py``,
the one generic installer (:mod:`aisland.wheels.install`, copied under a generated header) that
reads it and names each chosen wheel by its FILE: pip takes a wheel it finds only through
``--find-links`` from PyPI instead when both have the version (measured 2026-10-09), and takes a
wheel named by its file over PyPI's. The empty-venv check runs that script.

The folder is the machine's, not the repository's: each run writes ``wheels/.gitignore`` holding ``*``,
so git never sees it — unless the run is told ``--commit``, which then commits ``wheels/`` itself (the zip
excepted) once the empty-venv check passed. ``aisland wheels zip`` packs the folder into
``<project>-wheels.zip`` with a ``__main__.py`` (:mod:`aisland.wheels.unzip`), so the other machine — which has
no aisland — unpacks and installs it with ``python <project>-wheels.zip``; ``aisland wheels install`` runs the
same zip into a named interpreter.

The report is JSON-serializable (:meth:`WheelsReport.to_dict`, ``aisland wheels --json``): the
shape an MCP tool returns.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Sequence, Set, Tuple

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from aisland import console, downloads
from aisland import pyproject as pyproject_toml
from aisland.errors import AislandError
from aisland.github import GitHubRepository, project_repositories
from aisland.installers import ensure_uv_in_venv
from aisland.projects import get_workspace_name, workspace_folders
from aisland.pypi import PypiFile, PypiInfo, PypiProject, points_at_owner, pypi_json
from aisland.sources import read_committed_sources
from aisland.wheels import bundle as wheels_bundle
from aisland.wheels import check as wheels_check
from aisland.wheels import extra as wheels_extra
from aisland.wheels import scrub as wheels_scrub
from aisland.wheels.check import (
    attribute_failure,
    install_and_import,
    install_bundle_and_import,
    names_only_here,
    owner_of,
    wheel_differences,
    wheel_modules,
)
from aisland.wheels.files import (
    GITIGNORE,
    IGNORE_ALL,
    IGNORE_ZIP,
    INSTALL_SCRIPT,
    README_NAME,
    WHEELS_DIR,
    WHEELS_YAML,
    ZIP_TIME,
    installs_own_wheel,
    read_install_choices,
    render_install_script,
    render_readme,
    render_wheels_yaml,
    write_wheels,
    write_zip,
)
from aisland.wheels.report import (
    BuiltWheel,
    BundleWheel,
    ImportFailure,
    ModuleFailure,
    NotBuilt,
    PublishedDependency,
    WheelsReport,
    WheelsScope,
    bundle_command,
    provenance_header,
    regenerate_command,
)

#: PyPI's answer for a project: this workspace's release, none, or a stranger's package under the name.
PypiState = Literal["published", "absent", "foreign"]


@dataclass(frozen=True)
class WorkspaceProject:
    """One submodule of the workspace that is a Python project.

    Args:
        name: Its folder (= its ``.gitmodules`` path), the name a user types.
        distribution: Its distribution name (``acme-tools`` for a folder ``acme``).
        path: Its folder on this machine.
        pyproject: Its parsed ``pyproject.toml``; ``None`` when the folder is empty (a detached
            source or an inactive submodule) — then ``distribution`` comes from ``workspace.toml``,
            or is the folder name.
    """

    name: str
    distribution: str
    path: Path
    pyproject: Optional[pyproject_toml.Table]

    @property
    def checked_out(self) -> bool:
        """Whether this machine has its source."""
        return self.pyproject is not None


# --- the workspace's projects and what a project needs -----------------------------------------


def workspace_projects(workspace_root: Path) -> Dict[str, WorkspaceProject]:
    """Every submodule that is a Python project (without ``.gitmodules``: every project folder), keyed by canonical
    distribution name.

    An empty folder is kept (``pyproject=None``): a requirement on it must be told apart from a
    requirement on something outside the workspace.
    """
    registry = read_committed_sources(workspace_root)
    projects: Dict[str, WorkspaceProject] = {}
    for _name, relative, _active in workspace_folders(workspace_root):
        path = workspace_root / relative
        pyproject_file = path / "pyproject.toml"
        if pyproject_file.is_file():
            data = pyproject_toml.load(pyproject_file)
            distribution = (data.get("project") or {}).get("name")
            if not distribution:
                continue
            projects[canonicalize_name(distribution)] = WorkspaceProject(relative, distribution, path, data)
        else:
            source = registry.get(relative)
            distribution = source.distribution if source and source.distribution else relative
            projects[canonicalize_name(distribution)] = WorkspaceProject(relative, distribution, path, None)
    return projects


def _requirements(project: WorkspaceProject, extra: Optional[str]) -> List[Requirement]:
    """``project``'s base dependencies (``extra=None``) or one extra's, parsed."""
    table = (project.pyproject or {}).get("project") or {}
    if extra is None:
        raw = table.get("dependencies")
    else:
        raw = (table.get("optional-dependencies") or {}).get(extra)
    requirements = []
    for spec in raw or []:
        try:
            requirements.append(Requirement(spec))
        except InvalidRequirement as error:
            raise AislandError(f"{project.name}/pyproject.toml: cannot read the requirement {spec!r}: {error}")
    return requirements


def dependency_closure(projects: Dict[str, WorkspaceProject], target: WorkspaceProject) -> List[WorkspaceProject]:
    """The workspace projects ``target`` needs, sorted by folder name, ``target`` excluded.

    Base dependencies always; an extra only when a requirement names it (``confluid[pydantic]``);
    markers ignored, so a project needed on one platform only is still in.
    """
    root = canonicalize_name(target.distribution)
    queue: List[Tuple[str, Optional[str]]] = [(root, None)]
    done: Set[Tuple[str, Optional[str]]] = set()
    while queue:
        item = queue.pop()
        if item in done:
            continue
        done.add(item)
        key, extra = item
        for requirement in _requirements(projects[key], extra):
            dependency = canonicalize_name(requirement.name)
            if dependency in projects:
                queue.append((dependency, None))
                queue.extend((dependency, e) for e in requirement.extras)
    needed = {key for key, _ in done} - {root}
    return sorted((projects[key] for key in needed), key=lambda p: p.name)


def pypi_state(distribution: str, owner: Optional[str]) -> Tuple[PypiState, Optional[PypiProject]]:
    """Whether PyPI has ``distribution`` as THIS workspace's project, with PyPI's JSON when it answered.

    The workspace's project is the release whose metadata names ``github.com/<owner>/``, the owner of the project's
    own remote; a project whose remote is not on GitHub (``owner`` None) has no release aisland can tell apart from a
    stranger's (``foreign``).

    Raises ``click.ClickException`` when PyPI cannot be reached: a guess would either ship a
    wheel PyPI already serves or leave out one it does not.
    """
    status, payload = pypi_json(distribution)
    if status == "missing":
        return "absent", None
    if status == "unreachable":
        raise AislandError(f"cannot tell whether {distribution} is on PyPI: {payload}. Retry when it answers.")
    project: PypiProject = payload
    info: PypiInfo = project.get("info") or {}
    if not points_at_owner(info, owner):
        return "foreign", project
    for files in (project.get("releases") or {}).values():
        if any(not f.get("yanked", False) for f in files or []):
            return "published", project
    return "absent", None


# --- building and reading wheels -------------------------------------------------------------


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(project_dir), *args], capture_output=True, text=True)


def export_head(project_dir: Path, dest: Path) -> int:
    """Write ``project_dir``'s last commit into ``dest`` — what a wheel is built from.

    Returns the commit's time (seconds since the epoch), the timestamp its wheel carries.
    """
    archive = subprocess.run(["git", "-C", str(project_dir), "archive", "--format=tar", "HEAD"], capture_output=True)
    if archive.returncode != 0:
        reason = archive.stderr.decode(errors="replace").strip()
        raise AislandError(f"{project_dir.name}: cannot export its last commit: {reason}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return int(_git(project_dir, "log", "-1", "--format=%ct", "HEAD").stdout.strip())


def export_worktree(project_dir: Path, dest: Path) -> int:
    """Write ``project_dir``'s files as they are into ``dest`` — what ``--force`` builds a dirty project from.

    The files are the ones git tracks and the untracked ones it does not ignore: a deleted file is gone,
    a new one is in, and an ignored setuptools ``build/lib`` stays out, as it does from a commit. Returns the
    last commit's time, so a rerun over unchanged files builds the same wheel.
    """
    listed = _git(project_dir, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise AislandError(f"{project_dir.name}: cannot list its files: {listed.stderr.strip()}")
    for relative in sorted(set(filter(None, listed.stdout.split("\0")))):
        source = project_dir / relative
        # A deleted file is still listed (it is in the index), and a nested repository is a folder: neither is
        # copied, as `git archive` carries neither.
        if not source.is_file() and not source.is_symlink():
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    return int(_git(project_dir, "log", "-1", "--format=%ct", "HEAD").stdout.strip())


def build_wheel(uv: str, source_dir: Path, out_dir: Path, timestamp: Optional[int] = None) -> Path:
    """Build ``source_dir``'s wheel with uv into ``out_dir`` (replacing one of the same name).

    ``timestamp`` becomes ``SOURCE_DATE_EPOCH``: setuptools stamps the dist-info files with the
    build's clock otherwise, so an unchanged project would rebuild into a different file
    (measured 2026-10-08) and git would see every wheel change on every run.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ) if timestamp is None else {**os.environ, "SOURCE_DATE_EPOCH": str(timestamp)}
    with tempfile.TemporaryDirectory() as tmp:
        command = [uv, "build", "--wheel", "--quiet", "--python", sys.executable, *wheels_check.PYPI_INDEX_ARGS]
        proc = subprocess.run([*command, "--out-dir", tmp, str(source_dir)], capture_output=True, text=True, env=env)
        built = sorted(Path(tmp).glob("*.whl"))
        if proc.returncode != 0 or len(built) != 1:
            raise AislandError(f"building the wheel of {source_dir.name} failed:\n{proc.stderr.strip()}")
        wheel = out_dir / built[0].name
        os.replace(built[0], wheel)
    return wheel


def _wheel_version(wheel: Path) -> str:
    """The version a wheel's file name carries (``name-<version>-py3-none-any.whl``)."""
    return wheel.name.split("-")[1]


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloads.download(url, dest)
    return dest


def _pure_wheel(payload: PypiProject) -> Optional[PypiFile]:
    """The pure-Python wheel among PyPI's files of its latest version."""
    entries: List[PypiFile] = payload.get("urls") or []
    for entry in entries:
        if entry.get("packagetype") == "bdist_wheel" and str(entry.get("filename", "")).endswith("-none-any.whl"):
            return entry
    return None


# --- the run ---------------------------------------------------------------------------------


def _uv(workspace_root: Path) -> str:
    return ensure_uv_in_venv(workspace_root / ".venv" / "bin")


def find_target(projects: Dict[str, WorkspaceProject], workspace_root: Path, name: str) -> WorkspaceProject:
    for project in projects.values():
        if project.name == name:
            break
    else:
        found = projects.get(canonicalize_name(name))
        if found is None:
            raise AislandError(f"no project {name!r} in the workspace {workspace_root}")
        project = found
    if not project.checked_out:
        raise AislandError(
            f"{project.name} is not checked out on this machine (its folder is empty). "
            f"Activate it with: aisland workspace configure active set {project.name} true --machine-only"
        )
    return project


def _package_folders(path: Path) -> List[str]:
    """The project's top-level package folders (an ``__init__.py`` inside), its wheels folder left out."""
    return sorted(
        child.name
        for child in path.iterdir()
        if child.is_dir() and child.name != WHEELS_DIR and (child / "__init__.py").is_file()
    )


def wheels_folder_problem(project: WorkspaceProject) -> Optional[str]:
    """Why ``project``'s build would take the ``wheels/`` folder this command writes for a package, or None.

    setuptools discovers packages when a project lists none, and takes every folder with a Python name in a flat
    layout — ``wheels`` among them: two top-level packages stop the build, and a ``[tool.setuptools.packages.find]``
    without ``include`` ships the folder inside the project's wheel. setuptools' own advice is to list the packages,
    so that is the fix this names. Left alone: another build backend, a ``setup.py`` (its arguments are code, not
    read), a ``src`` layout, a package list or ``py-modules``, and a ``find`` that cannot reach the folder.
    """
    data = project.pyproject or {}
    backend = (data.get("build-system") or {}).get("build-backend")
    if (backend and not str(backend).startswith("setuptools")) or (project.path / "setup.py").exists():
        return None
    settings = (data.get("tool") or {}).get("setuptools") or {}
    packages = settings.get("packages")
    if isinstance(packages, list) or "py-modules" in settings:
        return None
    folders = _package_folders(project.path) or ["your_package"]
    fix = f"{project.name}/pyproject.toml:\n    [tool.setuptools.packages.find]\n    include = " + json.dumps(
        [f"{folder}*" for folder in folders]
    ).replace('","', '", "')
    find = packages.get("find") if isinstance(packages, dict) else None
    if not isinstance(find, dict):
        if (project.path / "src").is_dir():
            return None
        return (
            f"{project.name} finds its packages automatically, so the {WHEELS_DIR}/ folder this command writes would "
            f"become a second package and stop {project.name} from building.\nList its packages in {fix}"
        )
    where = [str(entry).rstrip("/") or "." for entry in find.get("where", ["."])]
    takes = "." in where or "./" in where
    include, exclude = find.get("include"), find.get("exclude") or []
    if include:
        takes = takes and any(fnmatch.fnmatchcase(WHEELS_DIR, pattern) for pattern in include)
    takes = takes and find.get("namespaces", True) is not False
    takes = takes and not any(fnmatch.fnmatchcase(WHEELS_DIR, pattern) for pattern in exclude)
    if not takes:
        return None
    return (
        f"{project.name}'s [tool.setuptools.packages.find] takes every folder, so the {WHEELS_DIR}/ folder this "
        f"command writes would be shipped inside {project.name}'s wheel.\nName its packages in {fix}"
    )


def _uncommitted(project: WorkspaceProject) -> List[str]:
    """``git status --porcelain`` of the project: its changes no commit holds."""
    if _git(project.path, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise AislandError(
            f"{project.name} is in no git repository; a wheel is built from a project's last commit. "
            "Put it under git (git init, then commit) and run this again."
        )
    return [line for line in _git(project.path, "status", "--porcelain").stdout.splitlines() if line]


def _uncommitted_file(project: WorkspaceProject, name: str) -> List[str]:
    """``git status --porcelain`` of one file of the project."""
    return [line for line in _git(project.path, "status", "--porcelain", "--", name).stdout.splitlines() if line]


def _outside_wheels_dir(line: str) -> bool:
    """Whether a ``git status --porcelain`` line names a path outside the project's ``wheels/``."""
    path = line[3:].split(" -> ")[-1].strip('"')
    return path != WHEELS_DIR and not path.startswith(f"{WHEELS_DIR}/")


def _refuse_uncommitted(
    projects: List[WorkspaceProject], force: bool = False, target: Optional[WorkspaceProject] = None
) -> Dict[str, List[str]]:
    """Stop before any build when a project to ship has changes no commit holds — unless ``force``, which
    returns each such project's changes instead (it is then built from its working tree). ``target``, whose
    own wheel is shipped too, is checked without its ``wheels/``: the folder this command writes is no change
    of its code."""
    dirty = {project.name: lines for project in projects if (lines := _uncommitted(project))}
    if target is not None and (lines := [line for line in _uncommitted(target) if _outside_wheels_dir(line)]):
        dirty[target.name] = lines
    if dirty and not force:
        problems = []
        for name, lines in dirty.items():
            problems.append(f"{name} has changes no commit holds; its wheel would not contain them:")
            problems.extend(f"    {line}" for line in lines)
        problems.append("Commit or stash them, then run this again — or pass --force to build them with these changes.")
        raise AislandError("\n".join(problems))
    return dirty


def _pypi_version(payload: Optional[PypiProject]) -> str:
    info: PypiInfo = (payload or {}).get("info") or {}
    return str(info.get("version") or "")


@dataclass(frozen=True)
class _Options:
    """The flags of one ``aisland wheels build`` run (see :func:`vendor_wheels`)."""

    scope: WheelsScope
    commit: bool
    make_zip: bool
    force: bool
    check: bool
    include_project: bool
    bundle: bool

    @property
    def builds_own_wheel(self) -> bool:
        """The target's own wheel is built: shipped (``--include-project``), or merged into the bundle."""
        return self.include_project or self.bundle


@dataclass(frozen=True)
class WheelsPlan:
    """What a run builds and what it takes from PyPI, worked out before anything is built.

    ``needed`` is the target's closure (:func:`dependency_closure`); ``states`` PyPI's answer for every project the
    run may build or take from it; ``to_build`` the projects whose wheels the scope builds; ``not_built`` the ones the
    scope covers that have no checkout here; ``published`` the needed projects taken from PyPI, checked against it.
    ``on_github`` is whether the target's own remote is on GitHub; for one that is not, ``hidden_names`` (the
    workspace's name and its projects' GitHub owners) are what its wheels' metadata must not carry
    (:mod:`aisland.wheels.scrub`).
    """

    projects: Dict[str, WorkspaceProject]
    target: WorkspaceProject
    needed: List[WorkspaceProject]
    states: Dict[str, Tuple[PypiState, Optional[PypiProject]]]
    to_build: List[WorkspaceProject]
    not_built: List[NotBuilt]
    published: List[WorkspaceProject]
    on_github: bool = True
    hidden_names: Tuple[str, ...] = ()

    @property
    def needed_names(self) -> Set[str]:
        return {p.name for p in self.needed}


@dataclass(frozen=True)
class _Extra:
    """The extra a run writes into the target's ``pyproject.toml`` (:mod:`aisland.wheels.extra`): ``--extra NAME``, or
    the one the file's generated block names (``remembered``); ``left`` is that block's extra when the run leaves it
    as it is, ``command`` the extra the regenerate command names."""

    name: Optional[str]
    remembered: bool
    left: Optional[str]
    command: Optional[str]


@dataclass
class _Built:
    """The wheels a run built in its scratch folder, in the order of the plan's ``to_build``, the distribution each
    module of them belongs to, the PyPI names another project holds, and the target's own wheel when it is built."""

    wheels: List[Path] = field(default_factory=list)
    entries: List[BuiltWheel] = field(default_factory=list)
    owners: Dict[str, str] = field(default_factory=dict)
    name_clashes: List[str] = field(default_factory=list)
    own_wheel: Optional[Path] = None
    own_entry: Optional[BuiltWheel] = None


@dataclass
class _Checked:
    """What a run's checks found: each published dependency against PyPI's wheel, the names only its checkout's
    changed files use, and the empty venvs' installs and imports — the target's and the bundle's. A run under
    ``--no-check`` has only the published dependencies, each "not compared"."""

    compared: List[PublishedDependency]
    new_names: Dict[str, Set[str]] = field(default_factory=dict)
    install_error: Optional[str] = None
    failures: Dict[str, ModuleFailure] = field(default_factory=dict)
    imported: List[str] = field(default_factory=list)
    packages: List[str] = field(default_factory=list)
    bundle_install_error: Optional[str] = None
    bundle_failures: Dict[str, ModuleFailure] = field(default_factory=dict)
    bundle_imported: List[str] = field(default_factory=list)


def vendor_wheels(
    workspace_root: Path,
    project_name: str,
    scope: WheelsScope = "unpublished",
    commit: bool = False,
    make_zip: bool = False,
    force: bool = False,
    check: bool = True,
    include_project: bool = False,
    bundle: bool = False,
    extra: Optional[str] = None,
) -> WheelsReport:
    """``aisland wheels build <project_name>``: build, write, compare, install, import; then, when asked, commit
    the folder (only if the check passed) and zip it. ``force`` builds a project with uncommitted changes from
    its working tree instead of refusing it; ``check=False`` (``--no-check``) skips the comparison with PyPI and
    the empty-venv install; ``include_project`` (``--include-project``) builds the project's own wheel too;
    ``bundle`` (``--bundle``) merges the project's own wheel and the needed wheels into one, written into
    ``wheels/bundle/`` and checked alone in an empty venv of its own; ``extra`` (``--extra NAME``) writes the extra
    NAME, which installs every wheel built, into the target's ``pyproject.toml`` (:mod:`aisland.wheels.extra`). See
    the module docstring.

    Every refusal comes before the first build, in this order: the flags, the plan (what the target needs and PyPI
    has), uncommitted changes, the extra, an unreadable ``wheels.yaml``."""
    options = _Options(scope, commit, make_zip, force, check, include_project, bundle)
    _refuse_flags(options)
    plan = _plan(workspace_root, project_name, scope)
    target = plan.target
    # The bundle holds the project's own code, so its project is held to the rules of its own wheel.
    dirty = _refuse_uncommitted(plan.to_build, force, target if options.builds_own_wheel else None)
    pyproject = target.path / "pyproject.toml"
    chosen = _choose_extra(plan, pyproject, extra, options)
    folder = target.path / WHEELS_DIR
    choices = read_install_choices(folder / WHEELS_YAML)
    uv = _uv(workspace_root)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        built = _build_all(uv, plan, dirty, scratch, options.builds_own_wheel)
        extra_changed, bundle_path = _write_folder(plan, options, chosen, built, folder, choices, pyproject)
        checked = _check(uv, plan, options, built, folder, scratch, bundle_path) if check else _unchecked(plan)

    behind = [p for p in checked.compared if p.differing_files]
    import_failures, import_warnings = _classify(
        checked.failures, built.owners, behind, checked.new_names, checked.packages
    )
    bundle_import_failures, _ = _classify(
        checked.bundle_failures, built.owners, behind, checked.new_names, checked.packages
    )
    passed = (
        checked.install_error is None
        and not import_failures
        and checked.bundle_install_error is None
        and not bundle_import_failures
    )
    wheels_dir = str(folder.relative_to(workspace_root))
    committed, commit_note, tracked_warning = _commit_or_note(target, options, chosen, passed, wheels_dir, pyproject)
    archive = write_zip(target.name, folder) if make_zip else None
    project_wheel = built.own_entry if include_project else None
    return WheelsReport(
        target=target.name,
        wheels_dir=wheels_dir,
        wheels_bytes=sum(
            (folder / b.wheel).stat().st_size for b in built.entries + ([project_wheel] if project_wheel else [])
        ),
        built=built.entries,
        published=checked.compared,
        name_clashes=built.name_clashes,
        install_error=checked.install_error,
        import_failures=import_failures,
        imported=checked.imported,
        packages=checked.packages,
        import_warnings=import_warnings,
        scope=scope,
        not_built=plan.not_built,
        needed_projects=sorted(plan.needed_names),
        committed=committed,
        commit_note=commit_note,
        zip_file=str(archive.relative_to(workspace_root)) if archive else None,
        zip_bytes=archive.stat().st_size if archive else 0,
        zip_standalone=archive is not None and installs_own_wheel(folder),
        tracked_warning=tracked_warning,
        checked=check,
        project_wheel=project_wheel,
        bundle=_bundle_entry(bundle_path, workspace_root, target, built.entries),
        bundle_install_error=checked.bundle_install_error,
        bundle_import_failures=bundle_import_failures,
        bundle_imported=checked.bundle_imported,
        extra=chosen.name,
        extra_changed=extra_changed,
        extra_remembered=chosen.remembered,
        extra_left=chosen.left,
    )


def _refuse_flags(options: _Options) -> None:
    """Stop on two flags that cannot go together, before anything is read."""
    if options.force and options.commit:
        raise AislandError(
            "--force cannot be combined with --commit: a wheel built with uncommitted changes is one no commit "
            "reproduces. Commit the changes and run with --commit, or drop --commit."
        )
    if not options.check and options.commit:
        raise AislandError(
            "--no-check cannot be combined with --commit: the folder is committed only once the install check "
            "passed. Drop one of them."
        )


def _plan(workspace_root: Path, project_name: str, scope: WheelsScope) -> WheelsPlan:
    """What the run builds: the target, what it needs, what PyPI has of it and what the scope covers. Refuses a target
    whose setuptools discovery would take ``wheels/`` for a package, and a needed project neither PyPI nor a checkout
    here has."""
    projects = workspace_projects(workspace_root)
    target = find_target(projects, workspace_root, project_name)
    problem = wheels_folder_problem(target)
    if problem:
        raise AislandError(problem)
    needed = dependency_closure(projects, target)
    needed_names = {p.name for p in needed}
    if scope == "all":
        covered = sorted((p for p in projects.values() if p.name != target.name), key=lambda p: p.name)
    else:
        covered = needed

    repositories = project_repositories(workspace_root)
    states = {
        p.name: pypi_state(p.distribution, _remote_owner(repositories, p))
        for p in covered
        if p.checked_out or p.name in needed_names
    }
    missing = [p for p in needed if not _published(states, p) and not p.checked_out]
    if missing:
        raise AislandError(
            "\n".join(
                f"{p.name} is not on PyPI and not checked out here, so its wheel cannot be built. "
                f"Check it out with: aisland workspace configure source set {p.name} local"
                for p in missing
            )
        )
    if scope == "unpublished":
        to_build = [p for p in needed if not _published(states, p)]
        not_built: List[NotBuilt] = []
    else:
        to_build = [p for p in covered if p.checked_out]
        not_built = [
            NotBuilt(
                p.name,
                (
                    f"no checkout here — taken from PyPI {_pypi_version(states[p.name][1])}"
                    if p.name in needed_names
                    else "no checkout here"
                ),
            )
            for p in covered
            if not p.checked_out
        ]
    published = [p for p in needed if _published(states, p) and p not in to_build]
    hidden = (get_workspace_name(workspace_root), *sorted({r.owner for r in repositories.values()}))
    return WheelsPlan(
        projects, target, needed, states, to_build, not_built, published, target.name in repositories, hidden
    )


def _remote_owner(repositories: Dict[str, GitHubRepository], project: WorkspaceProject) -> Optional[str]:
    """The GitHub owner of the project's own remote; None for a project whose remote is not on GitHub."""
    repository = repositories.get(project.name)
    return repository.owner if repository else None


def _published(states: Dict[str, Tuple[PypiState, Optional[PypiProject]]], project: WorkspaceProject) -> bool:
    return project.name in states and states[project.name][0] == "published"


def _choose_extra(plan: WheelsPlan, pyproject: Path, extra: Optional[str], options: _Options) -> _Extra:
    """The extra the run writes, and the refusals that keep it from writing the wrong one."""
    remembered = wheels_extra.remembered_extra(pyproject) if pyproject.is_file() else None
    if remembered is not None and extra is not None and extra != remembered[0]:
        raise AislandError(
            f"{pyproject}:{remembered[1]}: this file's generated block writes the extra {remembered[0]!r}; --extra "
            f"{extra} would write a second one — remove the block to rename it, or drop --extra"
        )
    # A block a build wrote is kept current by every later build without --extra — but not by one that builds the
    # project's own wheel (--include-project), which installs no folder: there --extra still decides.
    from_block = extra is None and remembered is not None and not options.include_project
    left = remembered[0] if extra is None and remembered is not None and options.include_project else None
    if from_block and remembered is not None:
        extra = remembered[0]
    if extra is not None:
        wheels_extra.check_extra(pyproject, extra, [p.distribution for p in plan.to_build])
        if options.commit and (lines := _uncommitted_file(plan.target, pyproject.name)):
            raise AislandError(
                f"{plan.target.name}/{pyproject.name} has changes no commit holds; --commit would commit them with the "
                f"extra {extra}:\n" + "\n".join(f"    {line}" for line in lines) + "\nCommit or stash them first."
            )
    # The regenerate command names --extra only where a rerun without it would not write the extra again: once the
    # block exists a build without --include-project keeps it current by itself.
    return _Extra(extra, from_block, left, extra if options.include_project else None)


def _build_all(uv: str, plan: WheelsPlan, dirty: Dict[str, List[str]], scratch: Path, own: bool) -> _Built:
    """Every wheel of the plan's ``to_build`` — and the target's own when ``own`` — into ``scratch/wheels``; for a
    target not on GitHub, each scrubbed of what names GitHub or the workspace (:func:`wheels_scrub.scrub_wheel`)."""
    built = _Built()
    for project in plan.to_build:
        wheel, entry = _build_one(uv, project, plan, dirty, scratch)
        if not plan.on_github:
            wheels_scrub.scrub_wheel(wheel, plan.hidden_names)
        built.wheels.append(wheel)
        built.entries.append(entry)
        built.owners.update({module: project.distribution for module in wheel_modules(wheel)})
        state, payload = plan.states.get(project.name, ("absent", None))
        if state == "foreign" and payload is not None:
            built.name_clashes.extend(_clash(project, payload, entry.version))
    if own:
        built.own_wheel, built.own_entry = _build_one(uv, plan.target, plan, dirty, scratch)
        if not plan.on_github:
            wheels_scrub.scrub_wheel(built.own_wheel, plan.hidden_names)
    return built


def _build_one(
    uv: str, project: WorkspaceProject, plan: WheelsPlan, dirty: Dict[str, List[str]], scratch: Path
) -> Tuple[Path, BuiltWheel]:
    """One project's wheel, from its last commit — or, when ``dirty`` lists its changes (``--force``), from its
    working tree, its commit then written ``<sha>+uncommitted``."""
    branch = _git(project.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    sha = _git(project.path, "rev-parse", "--short", "HEAD").stdout.strip()
    changes = dirty.get(project.name, [])
    if changes:
        sha += "+uncommitted"
    console.echo(f"==> Building {project.name} ({branch} {sha})", err=True)
    export = export_worktree if changes else export_head
    timestamp = export(project.path, scratch / "src" / project.name)
    wheel = build_wheel(uv, scratch / "src" / project.name, scratch / "wheels", timestamp)
    state, payload = plan.states.get(project.name, ("absent", None))
    entry = BuiltWheel(
        project.name,
        project.distribution,
        _wheel_version(wheel),
        branch,
        sha,
        wheel.name,
        needed=project.name in plan.needed_names or project is plan.target,
        on_pypi=_pypi_version(payload) if state == "published" else None,
        uncommitted=changes,
    )
    return wheel, entry


def _write_folder(
    plan: WheelsPlan,
    options: _Options,
    chosen: _Extra,
    built: _Built,
    folder: Path,
    choices: Dict[str, bool],
    pyproject: Path,
) -> Tuple[bool, Optional[Path]]:
    """The wheels and the files beside them into ``folder``, the extra into ``pyproject``, and the bundle when asked;
    returns whether the extra changed the file, and the bundle's path."""
    target, scope, include_project = plan.target, options.scope, options.include_project
    project_wheel = built.own_entry if include_project else None
    files = {
        README_NAME: render_readme(
            target.name, scope, built.entries, project_wheel, chosen.name, chosen.command, commits=plan.on_github
        ),
        WHEELS_YAML: render_wheels_yaml(
            target.name,
            scope,
            built.entries,
            plan.not_built,
            choices,
            project_wheel,
            chosen.command,
            commits=plan.on_github,
        ),
        INSTALL_SCRIPT: render_install_script(target.name, scope, include_project, chosen.command),
        GITIGNORE: IGNORE_ZIP if options.commit else IGNORE_ALL,
    }
    shipped_own = [built.own_wheel] if built.own_wheel and include_project else []
    write_wheels(folder, built.wheels + shipped_own, files)
    extra_changed = chosen.name is not None and wheels_extra.write_extra(
        pyproject,
        chosen.name,
        [wheels_extra.ExtraWheel(b.distribution, b.wheel) for b in built.entries],
        regenerate_command(target.name, scope, include_project, chosen.command),
    )
    bundle_path: Optional[Path] = None
    if options.bundle and built.own_wheel is not None and built.own_entry is not None:
        command = bundle_command(target.name, scope, include_project, chosen.command)
        bundle_path = _write_bundle(
            plan.projects, target, built.own_wheel, built.own_entry, built.wheels, built.entries, folder, command
        )
    return extra_changed, bundle_path


def _check(
    uv: str,
    plan: WheelsPlan,
    options: _Options,
    built: _Built,
    folder: Path,
    scratch: Path,
    bundle_path: Optional[Path],
) -> _Checked:
    """Both checks: each published dependency against PyPI's wheel, then the target in an empty venv with every
    module imported — and the bundle alone in one of its own."""
    target = plan.target
    new_names: Dict[str, Set[str]] = {}
    compared = [
        _compare(uv, project, plan.states[project.name][1] or {}, scratch, built.owners, new_names)
        for project in plan.published
    ]

    console.echo(f"==> Installing {target.name} in an empty venv and importing every module", err=True)
    if built.own_wheel is not None:
        target_wheel = built.own_wheel
    else:
        # Under --force the target's new modules are checked too: it is installed editable, from its folder.
        target_export = export_worktree if options.force and _uncommitted(target) else export_head
        target_export(target.path, scratch / "src" / target.name)
        target_wheel = build_wheel(uv, scratch / "src" / target.name, scratch / "target")
    own = wheel_modules(target_wheel)
    built.owners.update({module: target.distribution for module in own})
    installed = [wheel for wheel, b in zip(built.wheels, built.entries) if b.needed]
    packages = sorted(set(own) | {m for wheel in installed for m in wheel_modules(wheel)})
    install_error, failures, imported = install_and_import(uv, target.path, folder, packages, scratch)
    checked = _Checked(compared, new_names, install_error, failures, imported, packages)
    if bundle_path is not None:
        console.echo(f"==> Installing the bundle {bundle_path.name} alone in an empty venv", err=True)
        checked.bundle_install_error, checked.bundle_failures, checked.bundle_imported = install_bundle_and_import(
            uv, target.path, bundle_path, packages, scratch / "bundle"
        )
    return checked


def _unchecked(plan: WheelsPlan) -> _Checked:
    """What ``--no-check`` reports: every published dependency, not compared."""
    return _Checked(
        [
            PublishedDependency(
                p.name, p.distribution, _pypi_version(plan.states[p.name][1]), None, [], "not compared (--no-check)"
            )
            for p in plan.published
        ]
    )


def _commit_or_note(
    target: WorkspaceProject, options: _Options, chosen: _Extra, passed: bool, wheels_dir: str, pyproject: Path
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """``(commit, note, tracked warning)``: under ``--commit`` the folder is committed once the checks ``passed``;
    without it, a folder git tracks all the same is named with the command that stops tracking it."""
    if options.commit:
        if not passed:
            return None, "not committed: the install check failed", None
        committed = _commit_wheels(
            target,
            regenerate_command(target.name, options.scope, options.include_project, chosen.command),
            [pyproject.name] if chosen.name is not None else [],
        )
        return committed, None if committed else f"nothing new to commit in {wheels_dir}", None
    tracked = [line for line in _git(target.path, "ls-files", "--", WHEELS_DIR).stdout.splitlines() if line]
    if not tracked:
        return None, None, None
    return (
        None,
        None,
        f"{WHEELS_DIR}/ is tracked in {target.name} ({len(tracked)} files); stop tracking it with: "
        f"git -C {target.name} rm -r --cached {WHEELS_DIR}",
    )


def _classify(
    failures: Dict[str, ModuleFailure],
    owners: Dict[str, str],
    behind: List[PublishedDependency],
    new_names: Dict[str, Set[str]],
    packages: List[str],
) -> Tuple[List[ImportFailure], List[ImportFailure]]:
    """The import run's failures as ``(breaks, warnings)``.

    A package that fails, a module that misses a workspace project's module, or one that fails on a project behind
    PyPI is a break. A deeper module that misses something no workspace project ships is most often an optional
    extra's (the empty venv installs none), so it is reported without failing the run.
    """
    every_failure = []
    for module, f in sorted(failures.items()):
        named = f.get("missing")
        owner = owner_of(named, owners)
        culprits = [] if owner else attribute_failure(f, owners, behind, new_names)
        every_failure.append(ImportFailure(module, str(f.get("error")), named, owner, culprits))
    breaks = [f for f in every_failure if f.module in packages or f.blamed]
    return breaks, [f for f in every_failure if f not in breaks]


def _write_bundle(
    projects: Dict[str, WorkspaceProject],
    target: WorkspaceProject,
    own_wheel: Path,
    own_entry: BuiltWheel,
    staged: List[Path],
    built: List[BuiltWheel],
    folder: Path,
    command: str,
) -> Path:
    """Merge the target's wheel and the wheels it needs into ``<folder>/bundle/`` (:mod:`aisland.wheels.bundle`).

    A wheel ``--all`` built that the target does not need stays out. What the bundle requires is read from the
    ``pyproject.toml`` of each project inside it.
    """
    inside = [(wheel, b) for wheel, b in zip(staged, built) if b.needed]
    members = [(target, own_wheel, own_entry)] + [
        (projects[canonicalize_name(b.distribution)], wheel, b) for wheel, b in inside
    ]
    for project, _, _ in members:
        if "dependencies" in ((project.pyproject or {}).get("project") or {}).get("dynamic", []):
            raise AislandError(
                f"{project.name}/pyproject.toml declares its dependencies dynamic; a bundle reads them from "
                "[project] dependencies"
            )
    tables = [((p.pyproject or {}).get("project") or {}) for p, _, _ in members]
    requires = wheels_bundle.bundle_requirements(
        lambda name, extra: _requirements(projects[name], extra),
        canonicalize_name(target.distribution),
        list((tables[0].get("optional-dependencies") or {}).keys()),
        {canonicalize_name(b.distribution): b.version for _, _, b in members},
        [str(t["requires-python"]) for t in tables if "requires-python" in t],
    )
    distribution = wheels_bundle.BUNDLE_NAME.format(distribution=target.distribution)
    rows = "\n".join(f"| {b.project} | {b.distribution} | {b.version} | {b.branch} {b.commit} |" for _, _, b in members)
    header = "\n".join(provenance_header("", command, "aisland/aisland/wheels/build.py (_write_bundle)"))
    description = f"""\
<!--
{header}
-->
# {distribution}

{target.name} and the workspace projects it needs, in one wheel. Inside it pip knows only the name
`{distribution}`: none of the projects below is installed under its own name.

| Project | Distribution | Version | Commit |
| --- | --- | --- | --- |
{rows}
"""
    metadata = wheels_bundle.render_metadata(
        distribution,
        own_entry.version,
        f"{target.name} and the workspace projects it needs, in one wheel",
        requires,
        description,
    )
    parts = [wheels_bundle.BundlePart(b.project, b.distribution, wheel) for _, wheel, b in members]
    return wheels_bundle.merge_wheels(
        parts, distribution, own_entry.version, metadata, folder / wheels_bundle.BUNDLE_DIR, ZIP_TIME
    )


def _bundle_entry(
    bundle: Optional[Path], workspace_root: Path, target: WorkspaceProject, built: List[BuiltWheel]
) -> Optional[BundleWheel]:
    """The report's line for the bundle ``--bundle`` wrote (``None`` without one)."""
    if bundle is None:
        return None
    return BundleWheel(
        wheel=str(bundle.relative_to(workspace_root)),
        distribution=wheels_bundle.BUNDLE_NAME.format(distribution=target.distribution),
        version=bundle.name.split("-")[1],
        projects=[target.name] + [b.project for b in built if b.needed],
        bytes=bundle.stat().st_size,
    )


def _commit_wheels(target: WorkspaceProject, command: str, also: Sequence[str] = ()) -> Optional[str]:
    """Commit ``wheels/`` in the target — that path and ``also`` (``--extra``'s ``pyproject.toml``) only, whatever
    else is staged or changed — and return the commit's short SHA, or ``None`` when they hold nothing new."""
    paths = [WHEELS_DIR, *also]
    added = _git(target.path, "add", "-A", "--", *paths)
    if added.returncode != 0:
        raise AislandError(f"{target.name}: git add {' '.join(paths)} failed: {added.stderr.strip()}")
    if _git(target.path, "diff", "--cached", "--quiet", "--", *paths).returncode == 0:
        return None
    message = f"chore: wheels built with {command}"
    done = _git(target.path, "commit", "-q", "-m", message, "--", *paths)
    if done.returncode != 0:
        raise AislandError(f"{target.name}: committing {WHEELS_DIR} failed: {done.stderr.strip()}")
    return _git(target.path, "rev-parse", "--short", "HEAD").stdout.strip()


def zip_wheels(workspace_root: Path, project_name: str) -> Path:
    """``aisland wheels zip <project_name>``: the folder as it is, packed into ``<project>-wheels.zip``."""
    target = find_target(workspace_projects(workspace_root), workspace_root, project_name)
    folder = target.path / WHEELS_DIR
    if not (folder / WHEELS_YAML).is_file():
        raise AislandError(
            f"{target.name}/{WHEELS_DIR} has no {WHEELS_YAML}; build it first: aisland wheels build {target.name}"
        )
    return write_zip(target.name, folder)


def _clash(project: WorkspaceProject, payload: PypiProject, version: str) -> List[str]:
    """The warning for a name PyPI gives to another project, when pip would take theirs."""
    theirs = _pypi_version(payload)
    try:
        if Version(theirs) < Version(version):
            return []
    except InvalidVersion:
        pass
    return [
        f"{project.name}: PyPI's {project.distribution} {theirs} is another project's "
        f"— pip takes it over this wheel ({version})"
    ]


def _compare(
    uv: str,
    project: WorkspaceProject,
    payload: PypiProject,
    scratch: Path,
    owners: Dict[str, str],
    new_names: Dict[str, Set[str]],
) -> PublishedDependency:
    """``project``'s PyPI wheel against the one its checkout builds; records whose modules are whose and
    the names only the checkout's changed files use (``new_names``, for :func:`attribute_failure`)."""
    pypi_version = _pypi_version(payload)
    entry = _pure_wheel(payload)
    if entry is None:
        owners.setdefault(project.distribution.replace("-", "_"), project.distribution)
        note = f"PyPI has no pure-Python wheel of {pypi_version} to compare"
        return PublishedDependency(project.name, project.distribution, pypi_version, None, [], note)
    console.echo(f"==> Comparing {project.name} with PyPI {pypi_version}", err=True)
    remote = _download(str(entry["url"]), scratch / "pypi" / str(entry["filename"]))
    owners.update({module: project.distribution for module in wheel_modules(remote)})
    if not project.checked_out:
        return PublishedDependency(project.name, project.distribution, pypi_version, None, [], "no checkout here")
    export_head(project.path, scratch / "src" / project.name)
    local = build_wheel(uv, scratch / "src" / project.name, scratch / "local")
    differing = wheel_differences(remote, local)
    if differing:
        new_names[project.distribution] = names_only_here(remote, local, differing)
    return PublishedDependency(project.name, project.distribution, pypi_version, _wheel_version(local), differing)
