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
   question differs from :func:`aisland.sources.released_on_pypi`, the CI scaffolder's. A
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
the one generic installer (:mod:`aisland.wheels_install`, copied under a generated header) that
reads it and names each chosen wheel by its FILE: pip takes a wheel it finds only through
``--find-links`` from PyPI instead when both have the version (measured 2026-10-09), and takes a
wheel named by its file over PyPI's. The empty-venv check runs that script.

The folder is the machine's, not the repository's: each run writes ``wheels/.gitignore`` holding ``*``,
so git never sees it — unless the run is told ``--commit``, which then commits ``wheels/`` itself (the zip
excepted) once the empty-venv check passed. ``aisland wheels zip`` packs the folder into
``<project>-wheels.zip`` with a ``__main__.py`` (:mod:`aisland.wheels_unzip`), so the other machine — which has
no aisland — unpacks and installs it with ``python <project>-wheels.zip``; ``aisland wheels install`` runs the
same zip into a named interpreter.

The report is JSON-serializable (:meth:`WheelsReport.to_dict`, ``aisland wheels --json``): the
shape an MCP tool returns.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Set, Tuple, TypedDict

import click
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from aisland import pyproject as pyproject_toml
from aisland import wheels_bundle, wheels_extra, wheels_install, wheels_unzip
from aisland.errors import AislandError
from aisland.installers import ensure_uv_in_venv
from aisland.projects import workspace_folders
from aisland.sources import (
    PypiFile,
    PypiInfo,
    PypiProject,
    points_at_owner,
    project_repositories,
    pypi_json,
    read_committed_sources,
)
from aisland.wheels_install import WheelsFileError, read_wheels

#: The folder inside the project that receives the wheels; the install line names it.
WHEELS_DIR = "wheels"

#: The file written beside the wheels: what each one was built from and how to install.
README_NAME = "README.md"

#: The list of the wheels install.py reads; its ``install:`` values are the user's.
WHEELS_YAML = wheels_install.WHEELS_FILE

#: The generic installer written beside the wheels (a copy of :mod:`aisland.wheels_install`).
INSTALL_SCRIPT = "install.py"

#: The ignore file written into the folder: everything (the default) or only the zip (``--commit``).
GITIGNORE = ".gitignore"
_IGNORE_ALL = "*\n"
_IGNORE_ZIP = "*.zip\n"

#: The zip ``aisland wheels zip`` writes into the folder; ``python <it>`` unpacks and installs it.
ZIP_NAME = "{project}-wheels.zip"

#: Every zip member's time, so a zip of an unchanged folder is the same file (the zip format's epoch).
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

#: What a run builds: the needed projects PyPI lacks (the default), every needed project with a
#: checkout (``--include-published``), or every workspace project with a checkout (``--all``).
WheelsScope = Literal["unpublished", "needed", "all"]

#: Each scope's flag, as the command line spells it.
_SCOPE_FLAGS: Dict[str, str] = {"unpublished": "", "needed": " --include-published", "all": " --all"}

#: The flag that builds the project's own wheel too, as the command line spells it.
_INCLUDE_PROJECT_FLAG = " --include-project"

#: The line that frames a generated header (and the template's own) in the files written.
_HEADER_RULE = "# =========================================================================\n"

#: Where uv finds PyPI, for the builds and the empty-venv install. Empty = uv's own default
#: (pypi.org, or the index the user's uv configuration names). A test points it at a local folder.
_PYPI_INDEX_ARGS: Tuple[str, ...] = ()

#: The labels :func:`wheel_differences` uses for what is not a file of the wheel.
_DEPENDENCIES = "(its dependencies)"
_ENTRY_POINTS = "(its entry points)"

#: Run inside the empty venv: imports each package named after the output path and, when it
#: imports, every module under it (``__main__`` excepted: importing it runs the program), then
#: writes ``{"failures": {module: {"error", "missing", "frames"}}, "imported": [module, ...]}``.
#: ``missing`` is the module an ``ImportError`` names (``recordstream.draws``), which says whose release
#: lacks it; ``frames`` the files of the traceback, outermost first, which say whose code raised it. A package that fails is reported alone; the modules under it cannot be reached.
_IMPORT_SCRIPT = """\
import importlib, json, pkgutil, sys, traceback
failures, imported, seen = {}, [], set()
def attempt(name):
    seen.add(name)
    try:
        module = importlib.import_module(name)
    except BaseException as error:  # an import may raise anything, SystemExit included
        frames = [frame.filename for frame in traceback.extract_tb(error.__traceback__)]
        failures[name] = {
            "error": f"{type(error).__name__}: {error}",
            "missing": getattr(error, "name", None),
            "frames": frames,
        }
        return None
    imported.append(name)
    return module
for root in sys.argv[2:]:
    if root in seen:
        continue
    package = attempt(root)
    if package is None or not hasattr(package, "__path__"):
        continue
    found = [m.name for m in pkgutil.walk_packages(package.__path__, root + ".", onerror=lambda name: None)]
    for name in sorted(set(found)):
        if name not in seen and name.rsplit(".", 1)[-1] != "__main__":
            attempt(name)
with open(sys.argv[1], "w") as handle:
    json.dump({"failures": failures, "imported": sorted(imported)}, handle)
"""

PypiState = Literal["published", "absent", "foreign"]


class ModuleFailure(TypedDict, total=False):
    """One module the import run (:data:`_IMPORT_SCRIPT`) could not import, as it writes it: the error, the module an
    ``ImportError`` names (``None`` for another error) and the traceback's files, outermost first."""

    error: str
    missing: Optional[str]
    frames: List[str]


class ImportRun(TypedDict):
    """The file the import run writes: every module that failed, by name, and every module that imported."""

    failures: Dict[str, ModuleFailure]
    imported: List[str]


#: What an empty venv's check returns: the install's error (``None`` when it installed), the modules that failed to
#: import, the modules that imported.
CheckResult = Tuple[Optional[str], Dict[str, ModuleFailure], List[str]]


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


@dataclass
class BuiltWheel:
    """A wheel written into the project's ``wheels/`` folder, and the commit it came from."""

    project: str
    distribution: str
    version: str
    branch: str
    commit: str
    wheel: str
    #: Whether the target depends on it (``--all`` also builds what it does not).
    needed: bool = True
    #: The version PyPI has, when the project is published there.
    on_pypi: Optional[str] = None
    #: ``git status --porcelain`` of a project ``--force`` built from its working tree (its ``commit`` then
    #: ends in ``+uncommitted``); empty for a wheel built from its commit.
    uncommitted: List[str] = field(default_factory=list)


@dataclass
class NotBuilt:
    """A workspace project the scope covers that this machine cannot build (no checkout here)."""

    project: str
    reason: str


@dataclass
class PublishedDependency:
    """A workspace project the target takes from PyPI, compared with its checkout here.

    ``differing_files`` is empty when the wheel this checkout builds holds what PyPI's does;
    ``note`` says why no comparison was made (no checkout here, no pure-Python wheel on PyPI).
    """

    project: str
    distribution: str
    pypi_version: str
    checkout_version: Optional[str]
    differing_files: List[str]
    note: Optional[str] = None


@dataclass
class ImportFailure:
    """A module that did not import in the empty venv.

    ``missing`` is the module the error names; ``distribution`` the project that owns it. ``culprits``
    are the distributions an error that names no module (a ``TypeError`` from an outdated release) is
    attributed to: see :func:`attribute_failure`.
    """

    module: str
    error: str
    missing: Optional[str]
    distribution: Optional[str]
    culprits: List[str] = field(default_factory=list)

    @property
    def blamed(self) -> List[str]:
        """The distributions the failure points at: the one owning the module it misses, else its culprits."""
        return [self.distribution] if self.distribution else self.culprits


@dataclass
class BundleWheel:
    """The one wheel ``--bundle`` merged: the project and the wheels it needs (:mod:`aisland.wheels_bundle`)."""

    #: The file, relative to the workspace.
    wheel: str
    distribution: str
    version: str
    #: The projects inside it, the target first.
    projects: List[str]
    bytes: int


@dataclass
class WheelsReport:
    """What ``aisland wheels <target>`` built and found."""

    target: str
    wheels_dir: str
    wheels_bytes: int
    built: List[BuiltWheel]
    published: List[PublishedDependency]
    name_clashes: List[str]
    install_error: Optional[str]
    import_failures: List[ImportFailure]
    imported: List[str] = field(default_factory=list)
    #: The packages whose modules were imported: the target's and its wheels' outermost ones.
    packages: List[str] = field(default_factory=list)
    #: Modules below those packages that miss something no workspace project ships — most often an
    #: optional extra's dependency, which the empty venv does not install. Reported; never an exit 1.
    import_warnings: List[ImportFailure] = field(default_factory=list)
    scope: WheelsScope = "unpublished"
    #: The projects the scope covers that were not built, and why.
    not_built: List[NotBuilt] = field(default_factory=list)
    #: Every workspace project the target needs, built or not.
    needed_projects: List[str] = field(default_factory=list)
    #: The commit ``--commit`` made in the target (short SHA), or why it made none (``commit_note``).
    committed: Optional[str] = None
    commit_note: Optional[str] = None
    #: The zip ``--zip`` wrote, relative to the workspace.
    zip_file: Optional[str] = None
    zip_bytes: int = 0
    #: The zip installs the project's own wheel, so it runs from any folder (else only from the project's).
    zip_standalone: bool = False
    #: Said when the folder is ignored but files of it are still tracked, with the command that untracks them.
    tracked_warning: Optional[str] = None
    #: False under ``--no-check``: nothing was compared with PyPI and nothing installed, so ``ok`` found no failure
    #: because it looked for none.
    checked: bool = True
    #: The target's own wheel (``--include-project``); never in ``built``, which holds what the target needs.
    project_wheel: Optional[BuiltWheel] = None
    #: The one wheel ``--bundle`` wrote, and its own check: installed alone in an empty venv, every module imported.
    bundle: Optional[BundleWheel] = None
    bundle_install_error: Optional[str] = None
    bundle_import_failures: List[ImportFailure] = field(default_factory=list)
    bundle_imported: List[str] = field(default_factory=list)
    #: The extra ``--extra`` wrote into the target's ``pyproject.toml``, and whether that file changed.
    extra: Optional[str] = None
    extra_changed: bool = False
    #: True when the extra was written without ``--extra``: the target's ``pyproject.toml`` holds its generated block.
    extra_remembered: bool = False
    #: The extra a generated block names that this run left as it is (``--include-project``, no ``--extra``).
    extra_left: Optional[str] = None

    @property
    def ok(self) -> bool:
        """The target installed in an empty venv, every package imported, and no module missed a workspace project
        — and so did the bundle, when there is one (an unchecked run: no failure, since nothing was checked)."""
        bundle_ok = self.bundle_install_error is None and not self.bundle_import_failures
        return self.install_error is None and not self.import_failures and bundle_ok

    @property
    def release_first(self) -> List[str]:
        """The published projects a failed import points at: PyPI lacks what the wheels use."""
        owners = {d for f in self.import_failures for d in f.blamed}
        return sorted(p.project for p in self.published if p.distribution in owners)

    @property
    def behind(self) -> List[str]:
        """The published projects whose checkout builds a different wheel than PyPI's."""
        return sorted(p.project for p in self.published if p.differing_files)

    @property
    def behind_only(self) -> List[str]:
        """Behind, yet no failed import points at them."""
        first = set(self.release_first)
        return [p for p in self.behind if p not in first]

    def to_dict(self) -> Dict[str, Any]:  # Any: asdict's JSON-ready nesting, which a reader indexes into
        """The report as JSON-ready data, verdicts included."""
        data = asdict(self)
        data.update(ok=self.ok, release_first=self.release_first, behind=self.behind)
        return data


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
        command = [uv, "build", "--wheel", "--quiet", "--python", sys.executable, *_PYPI_INDEX_ARGS]
        proc = subprocess.run([*command, "--out-dir", tmp, str(source_dir)], capture_output=True, text=True, env=env)
        built = sorted(Path(tmp).glob("*.whl"))
        if proc.returncode != 0 or len(built) != 1:
            raise AislandError(f"building the wheel of {source_dir.name} failed:\n{proc.stderr.strip()}")
        wheel = out_dir / built[0].name
        os.replace(built[0], wheel)
    return wheel


def _is_metadata(member: str) -> bool:
    return member.split("/", 1)[0].endswith((".dist-info", ".data"))


def _is_shim(source: str) -> bool:
    """An ``__init__.py`` that only extends its ``__path__`` — the parent package lives elsewhere."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return False
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    return bool(body) and all("extend_path" in ast.unparse(statement) for statement in body)


def wheel_modules(wheel: Path) -> List[str]:
    """The outermost packages (and top-level modules) ``wheel`` brings — what to import.

    A namespace shim's package is not one of them (``raidar/__init__.py`` in a child
    project's wheel); its child is, by its dotted name (``raidar.ultralytics``).
    """
    real: Set[str] = set()
    modules: Set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if _is_metadata(member):
                continue
            parts = member.split("/")
            if parts[-1] == "__init__.py":
                if not _is_shim(archive.read(member).decode("utf-8", errors="replace")):
                    real.add(".".join(parts[:-1]))
            elif len(parts) == 1 and member.endswith(".py"):
                modules.add(member[: -len(".py")])

    def _ancestors(package: str) -> List[str]:
        parts = package.split(".")
        return [".".join(parts[:i]) for i in range(1, len(parts))]

    outermost = {p for p in real if not any(a in real for a in _ancestors(p))}
    return sorted(outermost | modules)


def _wheel_content(wheel: Path) -> Dict[str, str]:
    """``{file: sha256}``, plus the dependencies and entry points under their labels.

    The rest of the dist-info (RECORD, WHEEL, the version) says nothing about the code.
    """
    content: Dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            data = archive.read(member)
            top, _, leaf = member.partition("/")
            if top.endswith(".dist-info"):
                if leaf == "METADATA":
                    lines = data.decode("utf-8", errors="replace").splitlines()
                    kept = sorted(line for line in lines if line.startswith(("Requires-Dist:", "Requires-Python:")))
                    content[_DEPENDENCIES] = hashlib.sha256("\n".join(kept).encode()).hexdigest()
                elif leaf == "entry_points.txt":
                    content[_ENTRY_POINTS] = hashlib.sha256(data).hexdigest()
                continue
            content[member] = hashlib.sha256(data).hexdigest()
    return content


def wheel_differences(published: Path, local: Path) -> List[str]:
    """The files that differ between two wheels (changed, added or removed), then the labels."""
    a, b = _wheel_content(published), _wheel_content(local)
    differing = [key for key in a.keys() | b.keys() if a.get(key) != b.get(key)]
    labels = [label for label in (_DEPENDENCIES, _ENTRY_POINTS) if label in differing]
    return sorted(key for key in differing if key not in labels) + labels


def _wheel_version(wheel: Path) -> str:
    """The version a wheel's file name carries (``name-<version>-py3-none-any.whl``)."""
    return wheel.name.split("-")[1]


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        dest.write_bytes(response.read())
    return dest


def _pure_wheel(payload: PypiProject) -> Optional[PypiFile]:
    """The pure-Python wheel among PyPI's files of its latest version."""
    entries: List[PypiFile] = payload.get("urls") or []
    for entry in entries:
        if entry.get("packagetype") == "bdist_wheel" and str(entry.get("filename", "")).endswith("-none-any.whl"):
            return entry
    return None


# --- the empty venv --------------------------------------------------------------------------


def install_and_import(uv: str, target_dir: Path, wheels_dir: Path, packages: List[str], scratch: Path) -> CheckResult:
    """Install ``target_dir`` in an empty venv with the shipped ``install.py`` (the wheels it needs, through
    uv), and import every module of ``packages``.

    Returns ``(install_error, failures, imported)``. The venv is made in ``scratch``, and uv runs there so
    the workspace's own ``uv.toml`` (its overrides) is not found; the target's own ``uv.toml``
    (a private index it may need) is passed through the script when it has one. ``PYTHONPATH`` is dropped,
    so nothing of the workspace leaks into the imports; ``uv``'s folder leads ``PATH``, where the script
    looks for it.
    """
    script = [str(wheels_dir / INSTALL_SCRIPT), "--installer", "uv", "--needed", "--", "--quiet"]
    return _install_and_import(
        uv, target_dir, packages, scratch, lambda python, config: [str(python), *script, *config]
    )


def install_bundle_and_import(
    uv: str, target_dir: Path, bundle: Path, packages: List[str], scratch: Path
) -> CheckResult:
    """Install ``bundle`` ALONE in an empty venv (``uv pip install <bundle>``: the rest from PyPI) and import every
    module of ``packages``; the venv, the configuration and the result as :func:`install_and_import`'s."""

    def _command(python: Path, config: List[str]) -> List[str]:
        return [uv, "pip", "install", "--python", str(python), "--quiet", *config, str(bundle)]

    return _install_and_import(uv, target_dir, packages, scratch, _command)


def _install_and_import(
    uv: str,
    target_dir: Path,
    packages: List[str],
    scratch: Path,
    install: Callable[[Path, List[str]], List[str]],
) -> CheckResult:
    """Make an empty venv in ``scratch``, run ``install(python, configuration)`` there, import every module of
    ``packages``; ``configuration`` is the target's ``uv.toml`` (when it has one) and the index arguments."""
    scratch.mkdir(parents=True, exist_ok=True)
    venv = scratch / "venv"
    python = venv / "bin" / "python"
    created = subprocess.run(
        [uv, "venv", "--quiet", "--python", sys.executable, str(venv)], cwd=scratch, capture_output=True, text=True
    )
    if created.returncode != 0:
        return f"creating the venv failed:\n{created.stderr.strip()}", {}, []
    config = ["--config-file", str(target_dir / "uv.toml")] if (target_dir / "uv.toml").is_file() else []
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PATH"] = os.pathsep.join([str(Path(uv).parent), env.get("PATH", "")])
    installed = subprocess.run(
        install(python, [*config, *_PYPI_INDEX_ARGS]), cwd=scratch, env=env, capture_output=True, text=True
    )
    if installed.returncode != 0:
        return (installed.stderr.strip() or installed.stdout.strip()), {}, []
    result = scratch / "imports.json"
    imported = subprocess.run(
        [str(python), "-c", _IMPORT_SCRIPT, str(result), *packages],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
    )
    if not result.is_file():
        return f"the import run ended without a result (exit {imported.returncode}):\n{imported.stderr.strip()}", {}, []
    outcome: ImportRun = json.loads(result.read_text())
    return None, outcome["failures"], list(outcome["imported"])


def _owner(module: Optional[str], owners: Dict[str, str]) -> Optional[str]:
    """The distribution whose package is the longest prefix of ``module``."""
    if not module:
        return None
    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        prefix = ".".join(parts[:end])
        if prefix in owners:
            return owners[prefix]
    return None


_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_CALLED = re.compile(r"^\w+: ([\w.]+)\(")


def _identifiers(wheel: Path, only: Optional[Set[str]] = None) -> Set[str]:
    """The identifiers in the ``.py`` files of ``wheel`` (only the members named in ``only``, when given)."""
    names: Set[str] = set()
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.namelist():
            if member.endswith(".py") and (only is None or member in only):
                names.update(_IDENTIFIER.findall(archive.read(member).decode("utf-8", errors="replace")))
    return names


def _names_only_here(published: Path, local: Path, differing: List[str]) -> Set[str]:
    """The identifiers the checkout's changed files use that no file of PyPI's wheel does."""
    return _identifiers(local, set(differing)) - _identifiers(published)


def _error_names(error: str) -> Set[str]:
    """The names an error message points at: what it quotes and the callable it opens with."""
    names: Set[str] = set()
    for quoted in _QUOTED.findall(error):
        names.update(_IDENTIFIER.findall(quoted))
    called = _CALLED.match(error)
    if called:
        names.update(called.group(1).split("."))
    return names


def _frame_owner(filename: str, owners: Dict[str, str]) -> Optional[str]:
    """The distribution whose installed package holds ``filename`` (``.../site-packages/liquifai/app.py``)."""
    parts = Path(filename).parts
    if "site-packages" not in parts:
        return None
    inside = parts[parts.index("site-packages") + 1 :]
    if not inside:
        return None
    return _owner(inside[0].removesuffix(".py"), owners)


def attribute_failure(
    failure: ModuleFailure,
    owners: Dict[str, str],
    behind: List[PublishedDependency],
    new_names: Dict[str, Set[str]],
) -> List[str]:
    """The published projects, behind PyPI, whose release a failure that names no module is blamed on.

    Only a ``ModuleNotFoundError`` of a module no workspace project ships is an optional extra's. Any
    other error is the outdated release's when a project is behind, found in this order:

    1. the innermost frame of the traceback that lies in a behind project's code;
    2. the behind projects whose checkout-only code uses a name the message points at — a quoted name
       or the callable it opens with (``'config_files'``): a bad keyword is raised at the call, so no
       frame of the callee exists;
    3. else every behind project.

    No project behind, or a ``ModuleNotFoundError``: nobody.
    """
    if not behind or str(failure.get("error", "")).startswith("ModuleNotFoundError:"):
        return []
    distributions = {p.distribution for p in behind}
    for filename in reversed(failure.get("frames") or []):
        owner = _frame_owner(str(filename), owners)
        if owner in distributions:
            return [str(owner)]
    names = _error_names(str(failure.get("error", "")))
    named = [d for d in distributions if names & new_names.get(d, set())]
    return sorted(named or distributions)


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


def _write_wheels(folder: Path, staged: List[Path], files: Dict[str, str]) -> None:
    """Replace the folder's ``*.whl`` with ``staged`` and write ``files`` (name: text); other files stay. A bundle an
    earlier ``--bundle`` wrote goes too: built from older commits, it would sit beside the new wheels unchanged."""
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.whl"):
        old.unlink()
    bundle_dir = folder / wheels_bundle.BUNDLE_DIR
    for old in bundle_dir.glob("*.whl"):
        old.unlink()
    if bundle_dir.is_dir() and not any(bundle_dir.iterdir()):
        bundle_dir.rmdir()
    for wheel in staged:
        shutil.copy2(wheel, folder / wheel.name)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")


def read_install_choices(path: Path) -> Dict[str, bool]:
    """The ``install:`` value of each project in an existing ``wheels.yaml`` (none when there is no file).

    A file that cannot be read stops the run at its line, before anything is built: rewriting it would
    silently drop the choices it holds.
    """
    if not path.is_file():
        return {}
    try:
        _project, entries = read_wheels(path)
    except WheelsFileError as error:
        raise AislandError(str(error)) from error
    return {entry.project: entry.install for entry in entries}


def _pypi_version(payload: Optional[PypiProject]) -> str:
    info: PypiInfo = (payload or {}).get("info") or {}
    return str(info.get("version") or "")


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
    NAME, which installs every wheel built, into the target's ``pyproject.toml`` (:mod:`aisland.wheels_extra`). See
    the module docstring."""
    if force and commit:
        raise AislandError(
            "--force cannot be combined with --commit: a wheel built with uncommitted changes is one no commit "
            "reproduces. Commit the changes and run with --commit, or drop --commit."
        )
    if not check and commit:
        raise AislandError(
            "--no-check cannot be combined with --commit: the folder is committed only once the install check "
            "passed. Drop one of them."
        )
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

    def _owner(project: WorkspaceProject) -> Optional[str]:
        repository = repositories.get(project.name)
        return repository.owner if repository else None

    states = {p.name: pypi_state(p.distribution, _owner(p)) for p in covered if p.checked_out or p.name in needed_names}

    def _published(project: WorkspaceProject) -> bool:
        return project.name in states and states[project.name][0] == "published"

    missing = [p for p in needed if not _published(p) and not p.checked_out]
    if missing:
        raise AislandError(
            "\n".join(
                f"{p.name} is not on PyPI and not checked out here, so its wheel cannot be built. "
                f"Check it out with: aisland workspace configure source set {p.name} local"
                for p in missing
            )
        )
    if scope == "unpublished":
        to_build = [p for p in needed if not _published(p)]
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
    published = [p for p in needed if _published(p) and p not in to_build]
    # The bundle holds the project's own code, so its project is held to the rules of its own wheel.
    dirty = _refuse_uncommitted(to_build, force, target if include_project or bundle else None)
    pyproject = target.path / "pyproject.toml"
    remembered = wheels_extra.remembered_extra(pyproject) if pyproject.is_file() else None
    if remembered is not None and extra is not None and extra != remembered[0]:
        raise AislandError(
            f"{pyproject}:{remembered[1]}: this file's generated block writes the extra {remembered[0]!r}; --extra "
            f"{extra} would write a second one — remove the block to rename it, or drop --extra"
        )
    # A block a build wrote is kept current by every later build without --extra — but not by one that builds the
    # project's own wheel (--include-project), which installs no folder: there --extra still decides.
    extra_remembered = extra is None and remembered is not None and not include_project
    extra_left = remembered[0] if extra is None and remembered is not None and include_project else None
    if extra_remembered and remembered is not None:
        extra = remembered[0]
    # The regenerate command names --extra only where a rerun without it would not write the extra again: once the
    # block exists a build without --include-project keeps it current by itself.
    command_extra = extra if include_project else None
    if extra is not None:
        wheels_extra.check_extra(pyproject, extra, [p.distribution for p in to_build])
        if commit and (lines := _uncommitted_file(target, pyproject.name)):
            raise AislandError(
                f"{target.name}/{pyproject.name} has changes no commit holds; --commit would commit them with the "
                f"extra {extra}:\n" + "\n".join(f"    {line}" for line in lines) + "\nCommit or stash them first."
            )

    folder = target.path / WHEELS_DIR
    choices = read_install_choices(folder / WHEELS_YAML)
    uv = _uv(workspace_root)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        built: List[BuiltWheel] = []
        staged: List[Path] = []
        owners: Dict[str, str] = {}
        name_clashes: List[str] = []

        def _build(project: WorkspaceProject) -> Tuple[Path, BuiltWheel]:
            branch = _git(project.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            sha = _git(project.path, "rev-parse", "--short", "HEAD").stdout.strip()
            changes = dirty.get(project.name, [])
            if changes:
                sha += "+uncommitted"
            click.echo(f"==> Building {project.name} ({branch} {sha})", err=True)
            export = export_worktree if changes else export_head
            timestamp = export(project.path, scratch / "src" / project.name)
            wheel = build_wheel(uv, scratch / "src" / project.name, scratch / "wheels", timestamp)
            state, payload = states.get(project.name, ("absent", None))
            entry = BuiltWheel(
                project.name,
                project.distribution,
                _wheel_version(wheel),
                branch,
                sha,
                wheel.name,
                needed=project.name in needed_names or project is target,
                on_pypi=_pypi_version(payload) if state == "published" else None,
                uncommitted=changes,
            )
            return wheel, entry

        for project in to_build:
            wheel, entry = _build(project)
            staged.append(wheel)
            built.append(entry)
            owners.update({module: project.distribution for module in wheel_modules(wheel)})
            state, payload = states.get(project.name, ("absent", None))
            if state == "foreign" and payload is not None:
                name_clashes.extend(_clash(project, payload, entry.version))
        own_wheel: Optional[Path] = None
        own_entry: Optional[BuiltWheel] = None
        if include_project or bundle:
            own_wheel, own_entry = _build(target)
        project_wheel = own_entry if include_project else None

        files = {
            README_NAME: render_readme(target.name, scope, built, project_wheel, extra, command_extra),
            WHEELS_YAML: render_wheels_yaml(
                target.name, scope, built, not_built, choices, project_wheel, command_extra
            ),
            INSTALL_SCRIPT: render_install_script(target.name, scope, include_project, command_extra),
            GITIGNORE: _IGNORE_ZIP if commit else _IGNORE_ALL,
        }
        _write_wheels(folder, staged + ([own_wheel] if own_wheel and include_project else []), files)
        extra_changed = extra is not None and wheels_extra.write_extra(
            pyproject,
            extra,
            [wheels_extra.ExtraWheel(b.distribution, b.wheel) for b in built],
            regenerate_command(target.name, scope, include_project, command_extra),
        )
        bundle_path: Optional[Path] = None
        if bundle and own_wheel is not None and own_entry is not None:
            command = bundle_command(target.name, scope, include_project, command_extra)
            bundle_path = _write_bundle(projects, target, own_wheel, own_entry, staged, built, folder, command)

        new_names: Dict[str, Set[str]] = {}
        install_error: Optional[str] = None
        failures: Dict[str, ModuleFailure] = {}
        imported: List[str] = []
        bundle_install_error: Optional[str] = None
        bundle_failures: Dict[str, ModuleFailure] = {}
        bundle_imported: List[str] = []
        packages: List[str] = []
        if not check:
            compared = [
                PublishedDependency(
                    p.name, p.distribution, _pypi_version(states[p.name][1]), None, [], "not compared (--no-check)"
                )
                for p in published
            ]
        else:
            compared = [
                _compare(uv, project, states[project.name][1] or {}, scratch, owners, new_names)
                for project in published
            ]

            click.echo(f"==> Installing {target.name} in an empty venv and importing every module", err=True)
            if own_wheel is not None:
                target_wheel = own_wheel
            else:
                # Under --force the target's new modules are checked too: it is installed editable, from its folder.
                target_export = export_worktree if force and _uncommitted(target) else export_head
                target_export(target.path, scratch / "src" / target.name)
                target_wheel = build_wheel(uv, scratch / "src" / target.name, scratch / "target")
            own = wheel_modules(target_wheel)
            owners.update({module: target.distribution for module in own})
            installed = [wheel for wheel, b in zip(staged, built) if b.needed]
            packages = sorted(set(own) | {m for wheel in installed for m in wheel_modules(wheel)})
            install_error, failures, imported = install_and_import(uv, target.path, folder, packages, scratch)
            if bundle_path is not None:
                click.echo(f"==> Installing the bundle {bundle_path.name} alone in an empty venv", err=True)
                bundle_install_error, bundle_failures, bundle_imported = install_bundle_and_import(
                    uv, target.path, bundle_path, packages, scratch / "bundle"
                )

    behind = [p for p in compared if p.differing_files]
    import_failures, import_warnings = _classify(failures, owners, behind, new_names, packages)
    bundle_import_failures, _ = _classify(bundle_failures, owners, behind, new_names, packages)
    wheels_dir = str(folder.relative_to(workspace_root))
    committed: Optional[str] = None
    commit_note: Optional[str] = None
    tracked_warning: Optional[str] = None
    if commit:
        if (
            install_error is None
            and not import_failures
            and bundle_install_error is None
            and not bundle_import_failures
        ):
            committed = _commit_wheels(
                target,
                regenerate_command(target.name, scope, include_project, command_extra),
                [pyproject.name] if extra is not None else [],
            )
            commit_note = None if committed else f"nothing new to commit in {wheels_dir}"
        else:
            commit_note = "not committed: the install check failed"
    else:
        tracked = [line for line in _git(target.path, "ls-files", "--", WHEELS_DIR).stdout.splitlines() if line]
        if tracked:
            tracked_warning = (
                f"{WHEELS_DIR}/ is tracked in {target.name} ({len(tracked)} files); stop tracking it with: "
                f"git -C {target.name} rm -r --cached {WHEELS_DIR}"
            )
    archive = _write_zip(target.name, folder) if make_zip else None
    zip_file = str(archive.relative_to(workspace_root)) if archive else None
    return WheelsReport(
        target=target.name,
        wheels_dir=wheels_dir,
        wheels_bytes=sum((folder / b.wheel).stat().st_size for b in built + ([project_wheel] if project_wheel else [])),
        built=built,
        published=compared,
        name_clashes=name_clashes,
        install_error=install_error,
        import_failures=import_failures,
        imported=imported,
        packages=packages,
        import_warnings=import_warnings,
        scope=scope,
        not_built=not_built,
        needed_projects=sorted(needed_names),
        committed=committed,
        commit_note=commit_note,
        zip_file=zip_file,
        zip_bytes=archive.stat().st_size if archive else 0,
        zip_standalone=archive is not None and installs_own_wheel(folder),
        tracked_warning=tracked_warning,
        checked=check,
        project_wheel=project_wheel,
        bundle=_bundle_entry(bundle_path, workspace_root, target, built),
        bundle_install_error=bundle_install_error,
        bundle_import_failures=bundle_import_failures,
        bundle_imported=bundle_imported,
        extra=extra,
        extra_changed=extra_changed,
        extra_remembered=extra_remembered,
        extra_left=extra_left,
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
        owner = _owner(named, owners)
        culprits = [] if owner else attribute_failure(f, owners, behind, new_names)
        every_failure.append(ImportFailure(module, str(f.get("error")), named, owner, culprits))
    breaks = [f for f in every_failure if f.module in packages or f.blamed]
    return breaks, [f for f in every_failure if f not in breaks]


def bundle_command(target: str, scope: WheelsScope, include_project: bool = False, extra: Optional[str] = None) -> str:
    """The command that writes a bundle — its METADATA's ``Generated by:`` and ``To regenerate:``."""
    return f"{regenerate_command(target, scope, include_project, extra)} --bundle"


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
    """Merge the target's wheel and the wheels it needs into ``<folder>/bundle/`` (:mod:`aisland.wheels_bundle`).

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
    header = "\n".join(_header("", command, "aisland/aisland/wheels.py (_write_bundle)"))
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
        parts, distribution, own_entry.version, metadata, folder / wheels_bundle.BUNDLE_DIR, _ZIP_TIME
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


def installs_own_wheel(folder: Path) -> bool:
    """Whether ``folder``'s ``wheels.yaml`` lists the project's own wheel with ``install: true``, and the wheel is
    there: ``install.py`` then installs it in place of the project's folder, so nothing needs the project's source."""
    if not (folder / WHEELS_YAML).is_file():
        return False
    project, entries = read_wheels(folder / WHEELS_YAML)
    return any(e.project == project and e.install and (folder / e.wheel).is_file() for e in entries)


def _write_zip(target_name: str, folder: Path) -> Path:
    """``<folder>/<target>-wheels.zip``: the generated ``__main__.py``, then every file of the folder but a zip.

    Members are written in name order with one fixed time, so a zip of an unchanged folder is the same file. A
    folder that installs the project's own wheel (:func:`installs_own_wheel`) makes a zip that installs from any
    folder.
    """
    archive = folder / ZIP_NAME.format(project=target_name)
    try:
        standalone = installs_own_wheel(folder)
    except WheelsFileError as error:
        raise AislandError(str(error)) from error
    members = [("__main__.py", render_unzip_script(target_name, standalone).encode("utf-8"))]
    members += [
        (f"{WHEELS_DIR}/{path.name}", path.read_bytes())
        for path in sorted(folder.iterdir(), key=lambda p: p.name)
        if path.is_file() and path.suffix != ".zip"
    ]
    partial = archive.with_suffix(".zip.partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, data)
    os.replace(partial, archive)
    return archive


def zip_wheels(workspace_root: Path, project_name: str) -> Path:
    """``aisland wheels zip <project_name>``: the folder as it is, packed into ``<project>-wheels.zip``."""
    target = find_target(workspace_projects(workspace_root), workspace_root, project_name)
    folder = target.path / WHEELS_DIR
    if not (folder / WHEELS_YAML).is_file():
        raise AislandError(
            f"{target.name}/{WHEELS_DIR} has no {WHEELS_YAML}; build it first: aisland wheels build {target.name}"
        )
    return _write_zip(target.name, folder)


def install_zip(
    zip_path: Path, python: Path, into: Path, args: List[str], workspace_root: Optional[Path] = None
) -> int:
    """``aisland wheels install <zip>``: run the zip (unpack into ``into/wheels``, then its install.py with
    ``args``) in ``python``; returns its exit status.

    The workspace's own ``.venv`` is refused: install.py installs with pip, which ignores the workspace's
    ``uv.toml`` overrides, and the workspace installs its projects editable through ``aisland setup``.
    """
    if not zip_path.is_file():
        raise AislandError(f"no zip at {zip_path}")
    if workspace_root is not None:
        venv = (workspace_root / ".venv").absolute()
        if venv in python.absolute().parents:
            raise AislandError(
                f"{python} is the workspace's .venv; install the zip into another environment (--python <its python>)"
            )
    return subprocess.run([str(python), str(zip_path), "--into", str(into), *args]).returncode


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
    click.echo(f"==> Comparing {project.name} with PyPI {pypi_version}", err=True)
    remote = _download(str(entry["url"]), scratch / "pypi" / str(entry["filename"]))
    owners.update({module: project.distribution for module in wheel_modules(remote)})
    if not project.checked_out:
        return PublishedDependency(project.name, project.distribution, pypi_version, None, [], "no checkout here")
    export_head(project.path, scratch / "src" / project.name)
    local = build_wheel(uv, scratch / "src" / project.name, scratch / "local")
    differing = wheel_differences(remote, local)
    if differing:
        new_names[project.distribution] = _names_only_here(remote, local, differing)
    return PublishedDependency(project.name, project.distribution, pypi_version, _wheel_version(local), differing)


# --- rendering -------------------------------------------------------------------------------


def human_size(n_bytes: int) -> str:
    return f"{n_bytes / 1e6:.1f} MB" if n_bytes >= 1e6 else f"{n_bytes / 1e3:.0f} kB"


def _files(files: List[str]) -> str:
    shown = ", ".join(files[:3]) + (", …" if len(files) > 3 else "")
    return f"{len(files)} {'file differs' if len(files) == 1 else 'files differ'} ({shown})"


def _table(rows: List[List[str]]) -> List[str]:
    """Rows as two-space-indented, left-aligned columns (the last column unpadded)."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  " + "  ".join([c.ljust(w) for c, w in zip(row, widths)] + [row[-1]]).rstrip() for row in rows]


def _built_lines(report: WheelsReport) -> List[str]:
    """The report's first block: what was built, for the scope it ran with."""
    target, n = report.target, len(report.built)
    where = f"{report.wheels_dir} ({human_size(report.wheels_bytes)})"
    if report.scope == "unpublished":
        if not n:
            return [f"{target} needs no workspace project that is not on PyPI; {report.wheels_dir} holds no wheel."]
        noun = (
            "1 workspace project that is not on PyPI. Its wheel is"
            if n == 1
            else f"{n} workspace projects that are not on PyPI. Their wheels are"
        )
        lines = [f"{target} needs {noun} in {where}:"]
        return lines + _table([[b.project, b.version, f"{b.branch} {b.commit}"] for b in report.built])
    count = len(report.needed_projects)
    needs = f"{target} needs {count} workspace project{'s' if count != 1 else ''}"
    if report.scope == "needed":
        lines = [f"{needs}; {n} {'is' if n == 1 else 'are'} built from this checkout into {where}:"]
    else:
        lines = [f"Every workspace project with a checkout here is built into {where}; {needs}:"]
    rows = []
    for b in report.built:
        notes = ([f"on PyPI {b.on_pypi}"] if b.on_pypi else []) + ([] if b.needed else ["not needed"])
        rows.append([b.project, b.version, f"{b.branch} {b.commit}", ", ".join(notes)])
    lines += _table(rows) if rows else ["  (no wheel)"]
    if report.not_built:
        lines.append("Not built: " + ", ".join(f"{nb.project} ({nb.reason})" for nb in report.not_built))
    return lines


def render_report(report: WheelsReport) -> str:
    """The text ``aisland wheels`` prints."""
    target = report.target
    lines = _built_lines(report)
    own = report.project_wheel
    if own is not None:
        lines.append(f"{target}'s own wheel (--include-project), installed in place of its folder:")
        lines += _table([[own.project, own.version, f"{own.branch} {own.commit}"]])
    lines.append("")

    forced = [b for b in report.built + ([own] if own else []) if b.uncommitted]
    if forced:
        lines.append("Built with changes no commit holds (--force); no commit reproduces these wheels:")
        for b in forced:
            lines.append(f"  {b.project}")
            lines += [f"    {line}" for line in b.uncommitted]
        lines.append("")

    if report.published and not report.checked:
        count = len(report.published)
        taken = ", ".join(f"{p.project} {p.pypi_version}" for p in report.published)
        lines.append(
            f"It takes {count} workspace project{'s' if count > 1 else ''} from PyPI, not compared (--no-check): {taken}"
        )
    elif report.published:
        count = len(report.published)
        lines.append(
            f"It takes {count} workspace project{'s' if count > 1 else ''} from PyPI, compared with this checkout:"
        )
        rows = []
        for p in report.published:
            version = f"PyPI {p.pypi_version}"
            if p.checkout_version and p.checkout_version != p.pypi_version:
                version += f", checkout {p.checkout_version}"
            if p.note:
                verdict = f"not compared: {p.note}"
            elif p.differing_files:
                verdict = f"behind: {_files(p.differing_files)}"
            else:
                verdict = "up to date"
            rows.append([p.project, version, verdict])
        lines += _table(rows)
    else:
        lines.append("It takes no workspace project from PyPI.")
    lines.append("")

    if report.name_clashes:
        lines.append("Names PyPI gives to other projects:")
        lines += [f"  {clash}" for clash in report.name_clashes]
        lines.append("")

    if not report.checked:
        lines.append(
            f"Not checked (--no-check): {target} was not installed in an empty venv, so nothing says these wheels import."
        )
    elif report.install_error is not None:
        lines.append(f"Installing {target} in an empty venv failed:")
        lines += [f"    {line}" for line in report.install_error.splitlines()]
    else:
        n_packages = len(report.packages)
        lines.append(
            f"Installed {target} in an empty venv (its wheels + PyPI) and imported every module of its "
            f"{n_packages} package{'s' if n_packages != 1 else ''}:"
        )
        n_imported = len(report.imported)
        modules = f"module{'s' if n_imported != 1 else ''}"
        if report.import_failures:
            rows = [
                [f.module, f.error + (f"  ({', '.join(f.blamed)})" if f.blamed else "")] for f in report.import_failures
            ]
            lines += _table(rows)
            lines.append(f"  {n_imported} other {modules} imported")
        elif report.import_warnings:
            lines.append(f"  {n_imported} {modules} imported")
        else:
            lines.append(f"  all {n_imported} {modules} imported")
        if report.import_warnings:
            lines.append("")
            lines.append("Not imported, missing what no workspace project ships (an optional extra's?):")
            lines += _table([[f.module, f.error] for f in report.import_warnings])
    lines.append("")

    if report.install_error is not None:
        if report.behind:
            lines.append(f"Behind: {', '.join(report.behind)}")
    elif report.checked:
        if report.release_first:
            lines.append(f"Release first: {', '.join(report.release_first)}")
        elif report.import_failures:
            lines.append(f"{target} does not import, and no project it takes from PyPI is the cause.")
        if report.behind_only:
            lines.append(f"Behind, nothing breaks: {', '.join(report.behind_only)}")
        if not report.import_failures and not report.behind:
            lines.append(f"PyPI is up to date for {target}.")
    tail = []
    if report.extra is not None:
        n_wheels = len(report.built)
        state = "written into" if report.extra_changed else "unchanged in"
        kept = ", which holds its generated block" if report.extra_remembered else ""
        tail.append(
            f"The extra {report.extra} ({n_wheels} wheel{'s' if n_wheels != 1 else ''}) {state} "
            f"{target}/pyproject.toml{kept}; install it with: uv pip install -e ./{target}[{report.extra}]"
        )
    if report.extra_left is not None:
        tail.append(
            f"The extra {report.extra_left} in {target}/pyproject.toml was left as it is: --include-project builds "
            f"{target}'s own wheel. Pass --extra {report.extra_left} to write it for these wheels."
        )
    if report.tracked_warning:
        tail.append(report.tracked_warning)
    if report.committed:
        tail.append(f"Committed {report.wheels_dir}: {report.committed}")
    elif report.commit_note:
        tail.append(report.commit_note[0].upper() + report.commit_note[1:])
    if report.zip_file:
        zip_name = Path(report.zip_file).name
        where = "any folder" if report.zip_standalone else f"the {target} folder"
        tail.append(
            f"Zip: {report.zip_file} ({human_size(report.zip_bytes)}) — install it from {where} with: python {zip_name}"
        )
    tail += _bundle_lines(report)
    if tail:
        lines += ["", *tail]
    return "\n".join(lines).rstrip() + "\n"


def _bundle_lines(report: WheelsReport) -> List[str]:
    """The bundle ``--bundle`` wrote: where it is, what it holds, how it installs and what its own check found."""
    bundle = report.bundle
    if bundle is None:
        return []
    count = len(bundle.projects)
    lines = [
        f"Bundle: {bundle.wheel} ({human_size(bundle.bytes)}) — {count} project{'s' if count != 1 else ''} in one wheel "
        f"({', '.join(bundle.projects)}); install it with: pip install {Path(bundle.wheel).name}"
    ]
    if not report.checked:
        return lines + ["  Not checked (--no-check): the bundle was not installed in an empty venv."]
    if report.bundle_install_error is not None:
        lines.append("  Installing the bundle alone in an empty venv failed:")
        return lines + [f"    {line}" for line in report.bundle_install_error.splitlines()]
    n_imported = len(report.bundle_imported)
    modules = f"module{'s' if n_imported != 1 else ''}"
    if not report.bundle_import_failures:
        return lines + [f"  Installed alone in an empty venv (it + PyPI): all {n_imported} {modules} imported"]
    lines.append("  Installed alone in an empty venv (it + PyPI); these did not import:")
    lines += ["  " + row for row in _table([[f.module, f.error] for f in report.bundle_import_failures])]
    return lines + [f"    {n_imported} other {modules} imported"]


def regenerate_command(
    target: str, scope: WheelsScope, include_project: bool = False, extra: Optional[str] = None
) -> str:
    """The command that wrote a generated file — its header's ``Generated by:`` and ``To regenerate:``."""
    own = _INCLUDE_PROJECT_FLAG if include_project else ""
    return f"aisland wheels build {target}{_SCOPE_FLAGS[scope]}{own}{f' --extra {extra}' if extra else ''}"


def _header(prefix: str, command: str, source: str) -> List[str]:
    """The generator-provenance lines every generated file carries, each behind ``prefix``."""
    return [
        f"{prefix}AUTO-GENERATED FILE — DO NOT EDIT BY HAND",
        f"{prefix}Generated by: {command}",
        f"{prefix}Source: {source}",
        f"{prefix}To regenerate: {command}",
    ]


def render_readme(
    target: str,
    scope: WheelsScope,
    built: List[BuiltWheel],
    project_wheel: Optional[BuiltWheel] = None,
    extra: Optional[str] = None,
    command_extra: Optional[str] = "",
) -> str:
    """``wheels/README.md``: its generator, the install command, and each wheel's commit (``project_wheel``, the
    target's own, last); with ``extra``, the uv command that installs the extra first. ``command_extra`` is the
    ``--extra`` the regenerate command names (``""``: ``extra``; ``None``: none, the project remembering it).

    No date and no PyPI verdict, so a rebuild of unchanged projects changes no file.
    """
    flag = extra if command_extra == "" else command_extra
    command = regenerate_command(target, scope, project_wheel is not None, flag)
    shipped = built + ([project_wheel] if project_wheel else [])
    rows = "\n".join(
        f"| {b.wheel} | {b.project} | {b.branch} | {b.commit} | {'yes' if b.needed else 'no'} |" for b in shipped
    )
    table = (
        f"| Wheel | Project | Branch | Commit | Needed |\n| --- | --- | --- | --- | --- |\n{rows}"
        if shipped
        else "No wheel is needed."
    )
    if project_wheel is None:
        how = f"""\
Install {target} from its own folder, editable, with the wheels marked `install: true` in
`wheels/{WHEELS_YAML}` (by default the ones it needs):"""
    else:
        how = f"""\
{target}'s own wheel is here too. Install it with the wheels marked `install: true` in
`wheels/{WHEELS_YAML}` (by default the ones it needs); set its own entry's `install:` to false to
install {target} from its folder, editable, instead:"""
    title, intro = {
        "unpublished": (
            f"The wheels {target} needs",
            f"The workspace projects {target} depends on that are not on PyPI, each built from the commit\n"
            "below. Everything else it needs comes from PyPI.",
        ),
        "needed": (
            f"The wheels {target} needs",
            f"Every workspace project {target} depends on that had a checkout where these were built, each\n"
            "built from the commit below — those on PyPI too, so the commit, not the release, is what\n"
            "installs. Everything else it needs comes from PyPI.",
        ),
        "all": (
            f"The workspace's wheels, for {target}",
            "Every workspace project that had a checkout where these were built, each built from the commit\n"
            f"below. *Needed* marks the ones {target} depends on; everything else it needs comes from PyPI.",
        ),
    }[scope]
    header = "\n".join(_header("", command, "aisland/aisland/wheels.py (render_readme)"))
    if extra is not None:
        how = f"""\
With uv, from the folder holding {target}, install {target} editable together with every wheel
here: its `pyproject.toml` has the extra `{extra}`, whose uv sources are these files. After the
wheels are rebuilt, the same command takes the new ones.

    uv pip install -e ./{target}[{extra}]

Or with the script beside the wheels, which pip runs too.

{how}"""
    return f"""\
<!--
=========================================================================
{header}
=========================================================================
-->
# {title}

{intro}

{how}

    python wheels/{INSTALL_SCRIPT}

`python wheels/{INSTALL_SCRIPT} --help` lists the choices: `--all`, `--needed`, `--only <projects>`,
`--no-project`, `--dry-run`, `--installer uv`, and pip's own options after `--`. The script names each
wheel by its file, so pip takes it over PyPI's wheel of the same version. A rebuild keeps each wheel's
version, so an environment that already has them takes the new ones with:

    python wheels/{INSTALL_SCRIPT} --reinstall

{table}
"""


def render_wheels_yaml(
    target: str,
    scope: WheelsScope,
    built: List[BuiltWheel],
    not_built: List[NotBuilt],
    keep: Dict[str, bool],
    project_wheel: Optional[BuiltWheel] = None,
    extra: Optional[str] = None,
) -> str:
    """``wheels/wheels.yaml``: each wheel, its commit, whether ``target`` needs it, and ``install:``; the target's
    own wheel (``project_wheel``) last.

    ``install:`` is the value in ``keep`` (the user's, read from the file this one replaces) for a project
    listed there, else whether it is needed (the target's own wheel: true). Strings are written as JSON strings,
    which YAML reads as they are (a version such as ``1.0`` would otherwise read as a number).
    """
    command = regenerate_command(target, scope, project_wheel is not None, extra)
    lines = [_HEADER_RULE.rstrip("\n")]
    # The header's first line, said for this one generated file the user DOES edit: its `install:` values.
    lines += ["# AUTO-GENERATED FILE — edit only the `install:` values (a rerun keeps them)"]
    lines += _header("# ", command, "aisland/aisland/wheels.py (render_wheels_yaml)")[1:]
    lines += [
        _HEADER_RULE.rstrip("\n"),
        "# install.py installs the wheels marked `install: true`, then",
        f"# {target} editable. `needed: true` marks what {target} depends on.",
    ]
    if project_wheel is not None:
        lines += [
            f"# The last wheel is {target}'s own: while its `install:` is true, it is",
            f"# installed in place of {target}'s folder.",
        ]
    lines.append(f"project: {json.dumps(target)}")
    shipped = built + ([project_wheel] if project_wheel else [])

    def _string(value: Optional[str]) -> str:
        return "null" if value is None else json.dumps(value, ensure_ascii=False)

    lines.append("wheels:" if shipped else "wheels: []")
    for b in shipped:
        install = keep.get(b.project, b.needed)
        lines += [
            f"  - wheel: {_string(b.wheel)}",
            f"    project: {_string(b.project)}",
            f"    commit: {_string(f'{b.branch} {b.commit}')}",
            f"    needed: {'true' if b.needed else 'false'}",
            f"    on_pypi: {_string(b.on_pypi)}",
            f"    install: {'true' if install else 'false'}",
        ]
    lines.append("not_built:" if not_built else "not_built: []")
    for nb in not_built:
        lines += [f"  - project: {_string(nb.project)}", f"    reason: {_string(nb.reason)}"]
    return "\n".join(lines) + "\n"


def render_install_script(
    target: str, scope: WheelsScope, include_project: bool = False, extra: Optional[str] = None
) -> str:
    """``wheels/install.py``: :mod:`aisland.wheels_install` unchanged, under a generated header.

    The template's own header (between its first two rule lines) says it is the template; the copy's
    names the command that wrote it.
    """
    template = Path(wheels_install.__file__).read_text(encoding="utf-8")
    shebang, _template_header, rest = template.split(_HEADER_RULE, 2)
    command = regenerate_command(target, scope, include_project, extra)
    header = _header("# ", command, "aisland/aisland/wheels_install.py (copied unchanged below this header)")
    return shebang + _HEADER_RULE + "\n".join(header) + "\n" + _HEADER_RULE + rest


def render_unzip_script(target: str, standalone: bool = False) -> str:
    """A zip's ``__main__.py``: :mod:`aisland.wheels_unzip` under a generated header, naming ``target`` and whether
    the zip installs the project's own wheel (``standalone``: it then runs from any folder)."""
    template = Path(wheels_unzip.__file__).read_text(encoding="utf-8")
    shebang, _template_header, rest = template.split(_HEADER_RULE, 2)
    header = _header("# ", f"aisland wheels zip {target}", "aisland/aisland/wheels_unzip.py (copied below this header)")
    for placeholder, value in (
        ('PROJECT = "<project>"', f"PROJECT = {json.dumps(target)}"),
        ("STANDALONE = False", f"STANDALONE = {standalone}"),
    ):
        if rest.count(placeholder) != 1:
            raise RuntimeError(f"{wheels_unzip.__file__}: expected one {placeholder!r} line to fill in")
        rest = rest.replace(placeholder, value)
    return shebang + _HEADER_RULE + "\n".join(header) + "\n" + _HEADER_RULE + rest
