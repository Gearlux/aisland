"""Wheels for a project installed OUTSIDE the workspace (``aisland wheels <project>``).

A private project (signals-bridge) depends on workspace projects of which some are on PyPI
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
   points at ``github.com/Gearlux/`` and has a file that is not yanked. Pre-releases COUNT
   (recordstream 0.1.0a2: pip takes it when no final release exists), which is where this
   question differs from :func:`aisland.sources.released_on_pypi`, the CI scaffolder's. A
   stranger's package under the same name is not ours — and is reported, because pip takes the
   higher version of the two.
3. **The wheels** (:func:`export_head`, :func:`build_wheel`): each from an export of the
   project's last COMMIT, never its folder — a setuptools ``build/lib`` left in a checkout puts
   files the project deleted long ago into the wheel (measured 2026-10-08). A project with
   uncommitted changes is refused, so no one ships a change believing it is in. The folder's old
   ``*.whl`` go; anything else in it stays. Unchanged sources rebuild byte for byte, so git
   sees no change.
4. **The PyPI half, twice** (:func:`wheel_differences`, :func:`install_and_import`): each
   published dependency's PyPI wheel against the one its checkout builds, file by file ("behind");
   then the project installed in an EMPTY venv from its wheels plus PyPI, and every module of
   the project and of its wheels imported — not only the packages: a module no package imports
   is checked too (``signals.bridge.labelling`` needs ``recordstream.draws``, and
   ``import signals.bridge`` never reaches it). A failed import names the project whose module
   is missing — the one to release first (measured 2026-10-08: ``signals.lte`` needs
   ``recordstream.draws``, which recordstream 0.1.0a2 lacks).

The report is JSON-serializable (:meth:`WheelsReport.to_dict`, ``aisland wheels --json``): the
shape an MCP tool returns.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

import click
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from aisland.manager import ensure_uv_in_venv, submodule_active_map
from aisland.sources import _points_at_gearlux, _pypi_json, read_committed_sources

#: The folder inside the project that receives the wheels; the install line names it.
WHEELS_DIR = "wheels"

#: The file written beside the wheels: what each one was built from and how to install.
README_NAME = "README.md"

#: Where uv finds PyPI, for the builds and the empty-venv install. Empty = uv's own default
#: (pypi.org, or the index the user's uv configuration names). A test points it at a local folder.
_PYPI_INDEX_ARGS: Tuple[str, ...] = ()

#: The labels :func:`wheel_differences` uses for what is not a file of the wheel.
_DEPENDENCIES = "(its dependencies)"
_ENTRY_POINTS = "(its entry points)"

#: Run inside the empty venv: imports each package named after the output path and, when it
#: imports, every module under it (``__main__`` excepted: importing it runs the program), then
#: writes ``{"failures": {module: {"error", "missing"}}, "imported": [module, ...]}``. ``missing``
#: is the module an ``ImportError`` names (``recordstream.draws``), which says whose release lacks
#: it. A package that fails is reported alone; the modules under it cannot be reached.
_IMPORT_SCRIPT = """\
import importlib, json, pkgutil, sys
failures, imported, seen = {}, [], set()
def attempt(name):
    seen.add(name)
    try:
        module = importlib.import_module(name)
    except BaseException as error:  # an import may raise anything, SystemExit included
        failures[name] = {"error": f"{type(error).__name__}: {error}", "missing": getattr(error, "name", None)}
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


@dataclass(frozen=True)
class WorkspaceProject:
    """One submodule of the workspace that is a Python project.

    Args:
        name: Its folder (= its ``.gitmodules`` path), the name a user types.
        distribution: Its distribution name (``aisland-manager`` for ``aisland``).
        path: Its folder on this machine.
        pyproject: Its parsed ``pyproject.toml``; ``None`` when the folder is empty (a detached
            source or an inactive submodule) — then ``distribution`` comes from ``config.toml``,
            or is the folder name.
    """

    name: str
    distribution: str
    path: Path
    pyproject: Optional[Dict[str, Any]]

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

    ``missing`` is the module the error names; ``distribution`` the project that owns it.
    """

    module: str
    error: str
    missing: Optional[str]
    distribution: Optional[str]


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

    @property
    def ok(self) -> bool:
        """The target installed in an empty venv, every package imported, and no module missed a workspace project."""
        return self.install_error is None and not self.import_failures

    @property
    def release_first(self) -> List[str]:
        """The published projects a failed import points at: PyPI lacks what the wheels use."""
        owners = {f.distribution for f in self.import_failures if f.distribution}
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

    def to_dict(self) -> Dict[str, Any]:
        """The report as JSON-ready data, verdicts included."""
        data = asdict(self)
        data.update(ok=self.ok, release_first=self.release_first, behind=self.behind)
        return data


# --- the workspace's projects and what a project needs -----------------------------------------


def workspace_projects(workspace_root: Path) -> Dict[str, WorkspaceProject]:
    """Every submodule that is a Python project, keyed by canonical distribution name.

    An empty folder is kept (``pyproject=None``): a requirement on it must be told apart from a
    requirement on something outside the workspace.
    """
    registry = read_committed_sources(workspace_root)
    projects: Dict[str, WorkspaceProject] = {}
    for _name, relative, _active in submodule_active_map(workspace_root):
        path = workspace_root / relative
        pyproject_file = path / "pyproject.toml"
        if pyproject_file.is_file():
            with open(pyproject_file, "rb") as handle:
                data = tomllib.load(handle)
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
            raise click.ClickException(f"{project.name}/pyproject.toml: cannot read the requirement {spec!r}: {error}")
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


def pypi_state(distribution: str) -> Tuple[PypiState, Optional[Dict[str, Any]]]:
    """Whether PyPI has ``distribution`` as THIS workspace's project, with PyPI's JSON when it answered.

    Raises ``click.ClickException`` when PyPI cannot be reached: a guess would either ship a
    wheel PyPI already serves or leave out one it does not.
    """
    status, payload = _pypi_json(distribution)
    if status == "missing":
        return "absent", None
    if status == "unreachable":
        raise click.ClickException(f"cannot tell whether {distribution} is on PyPI: {payload}. Retry when it answers.")
    if not _points_at_gearlux(payload.get("info") or {}):
        return "foreign", payload
    for files in (payload.get("releases") or {}).values():
        if any(not f.get("yanked", False) for f in files or []):
            return "published", payload
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
        raise click.ClickException(f"{project_dir.name}: cannot export its last commit: {reason}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
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
            raise click.ClickException(f"building the wheel of {source_dir.name} failed:\n{proc.stderr.strip()}")
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

    A namespace shim's package is not one of them (``signals/__init__.py`` in a child
    project's wheel); its child is, by its dotted name (``signals.bridge``).
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


def _pure_wheel(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The pure-Python wheel among PyPI's files of its latest version."""
    entries: List[Dict[str, Any]] = payload.get("urls") or []
    for entry in entries:
        if entry.get("packagetype") == "bdist_wheel" and str(entry.get("filename", "")).endswith("-none-any.whl"):
            return entry
    return None


# --- the empty venv --------------------------------------------------------------------------


def install_and_import(
    uv: str, target_dir: Path, wheels_dir: Path, packages: List[str], scratch: Path
) -> Tuple[Optional[str], Dict[str, Dict[str, Optional[str]]], List[str]]:
    """Install ``target_dir`` the way its wheels' README says, in an empty venv, and import every
    module of ``packages``.

    Returns ``(install_error, failures, imported)``. The venv is made in ``scratch``, and uv runs there so
    the workspace's own ``uv.toml`` (its overrides) is not found; the target's own ``uv.toml``
    (a private index it may need) is used when it has one. ``PYTHONPATH`` is dropped, so nothing
    of the workspace leaks into the imports.
    """
    venv = scratch / "venv"
    python = venv / "bin" / "python"
    created = subprocess.run(
        [uv, "venv", "--quiet", "--python", sys.executable, str(venv)], cwd=scratch, capture_output=True, text=True
    )
    if created.returncode != 0:
        return f"creating the venv failed:\n{created.stderr.strip()}", {}, []
    config = ["--config-file", str(target_dir / "uv.toml")] if (target_dir / "uv.toml").is_file() else []
    install = [uv, "pip", "install", "--quiet", "--python", str(python), *config, *_PYPI_INDEX_ARGS]
    installed = subprocess.run(
        [*install, "--find-links", str(wheels_dir), "-e", str(target_dir)], cwd=scratch, capture_output=True, text=True
    )
    if installed.returncode != 0:
        return installed.stderr.strip(), {}, []
    result = scratch / "imports.json"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    imported = subprocess.run(
        [str(python), "-c", _IMPORT_SCRIPT, str(result), *packages],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
    )
    if not result.is_file():
        return f"the import run ended without a result (exit {imported.returncode}):\n{imported.stderr.strip()}", {}, []
    outcome = json.loads(result.read_text())
    failures: Dict[str, Dict[str, Optional[str]]] = outcome["failures"]
    return None, failures, list(outcome["imported"])


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


# --- the run ---------------------------------------------------------------------------------


def _uv(workspace_root: Path) -> str:
    return ensure_uv_in_venv(workspace_root / ".venv" / "bin")


def _find_target(projects: Dict[str, WorkspaceProject], workspace_root: Path, name: str) -> WorkspaceProject:
    for project in projects.values():
        if project.name == name:
            break
    else:
        found = projects.get(canonicalize_name(name))
        if found is None:
            raise click.ClickException(f"no project {name!r} in the workspace {workspace_root}")
        project = found
    if not project.checked_out:
        raise click.ClickException(
            f"{project.name} is not checked out on this machine (its folder is empty). "
            f"Activate it with: aisland setup --activate {project.name}"
        )
    return project


def _refuse_uncommitted(projects: List[WorkspaceProject]) -> None:
    """Stop before any build when a project to ship has changes no commit holds."""
    problems = []
    for project in projects:
        lines = [line for line in _git(project.path, "status", "--porcelain").stdout.splitlines() if line]
        if lines:
            problems.append(f"{project.name} has changes no commit holds; its wheel would not contain them:")
            problems.extend(f"    {line}" for line in lines)
    if problems:
        raise click.ClickException("\n".join(problems + ["Commit or stash them, then run this again."]))


def _write_wheels(folder: Path, staged: List[Path], readme: str) -> None:
    """Replace the folder's ``*.whl`` with ``staged`` and write its README; other files stay."""
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.whl"):
        old.unlink()
    for wheel in staged:
        shutil.copy2(wheel, folder / wheel.name)
    (folder / README_NAME).write_text(readme)


def vendor_wheels(workspace_root: Path, project_name: str) -> WheelsReport:
    """``aisland wheels <project_name>``: build, write, compare, install, import. See the module docstring."""
    projects = workspace_projects(workspace_root)
    target = _find_target(projects, workspace_root, project_name)
    needed = dependency_closure(projects, target)

    states = {p.name: pypi_state(p.distribution) for p in needed}
    to_build = [p for p in needed if states[p.name][0] != "published"]
    published = [p for p in needed if states[p.name][0] == "published"]

    missing = [p for p in to_build if not p.checked_out]
    if missing:
        raise click.ClickException(
            "\n".join(
                f"{p.name} is not on PyPI and not checked out here, so its wheel cannot be built. "
                f"Check it out with: aisland source set {p.name} local"
                for p in missing
            )
        )
    _refuse_uncommitted(to_build)

    uv = _uv(workspace_root)
    folder = target.path / WHEELS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        built: List[BuiltWheel] = []
        staged: List[Path] = []
        owners: Dict[str, str] = {}
        name_clashes: List[str] = []
        for project in to_build:
            branch = _git(project.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            commit = _git(project.path, "rev-parse", "--short", "HEAD").stdout.strip()
            click.echo(f"==> Building {project.name} ({branch} {commit})", err=True)
            timestamp = export_head(project.path, scratch / "src" / project.name)
            wheel = build_wheel(uv, scratch / "src" / project.name, scratch / "wheels", timestamp)
            staged.append(wheel)
            version = _wheel_version(wheel)
            built.append(BuiltWheel(project.name, project.distribution, version, branch, commit, wheel.name))
            owners.update({module: project.distribution for module in wheel_modules(wheel)})
            state, payload = states[project.name]
            if state == "foreign" and payload is not None:
                name_clashes.extend(_clash(project, payload, version))

        _write_wheels(folder, staged, render_readme(target.name, built))

        compared = [_compare(uv, project, states[project.name][1] or {}, scratch, owners) for project in published]

        click.echo(f"==> Installing {target.name} in an empty venv and importing every module", err=True)
        export_head(target.path, scratch / "src" / target.name)
        target_wheel = build_wheel(uv, scratch / "src" / target.name, scratch / "target")
        own = wheel_modules(target_wheel)
        owners.update({module: target.distribution for module in own})
        packages = sorted(set(own) | {m for w in staged for m in wheel_modules(w)})
        install_error, failures, imported = install_and_import(uv, target.path, folder, packages, scratch)

    every_failure = [
        ImportFailure(module, str(f.get("error")), f.get("missing"), _owner(f.get("missing"), owners))
        for module, f in sorted(failures.items())
    ]
    # A package that fails, or any module that misses a workspace project's module, is a break. A
    # deeper module that misses something no workspace project ships is most often an optional
    # extra's (the empty venv installs none), so it is reported without failing the run.
    import_failures = [f for f in every_failure if f.module in packages or f.distribution is not None]
    import_warnings = [f for f in every_failure if f not in import_failures]
    return WheelsReport(
        target=target.name,
        wheels_dir=str(folder.relative_to(workspace_root)),
        wheels_bytes=sum((folder / b.wheel).stat().st_size for b in built),
        built=built,
        published=compared,
        name_clashes=name_clashes,
        install_error=install_error,
        import_failures=import_failures,
        imported=imported,
        packages=packages,
        import_warnings=import_warnings,
    )


def _clash(project: WorkspaceProject, payload: Dict[str, Any], version: str) -> List[str]:
    """The warning for a name PyPI gives to another project, when pip would take theirs."""
    theirs = str((payload.get("info") or {}).get("version") or "")
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
    uv: str, project: WorkspaceProject, payload: Dict[str, Any], scratch: Path, owners: Dict[str, str]
) -> PublishedDependency:
    """``project``'s PyPI wheel against the one its checkout builds; records whose modules are whose."""
    pypi_version = str((payload.get("info") or {}).get("version") or "")
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
    return PublishedDependency(project.name, project.distribution, pypi_version, _wheel_version(local), differing)


# --- rendering -------------------------------------------------------------------------------


def _size(n_bytes: int) -> str:
    return f"{n_bytes / 1e6:.1f} MB" if n_bytes >= 1e6 else f"{n_bytes / 1e3:.0f} kB"


def _files(files: List[str]) -> str:
    shown = ", ".join(files[:3]) + (", …" if len(files) > 3 else "")
    return f"{len(files)} {'file differs' if len(files) == 1 else 'files differ'} ({shown})"


def _table(rows: List[List[str]]) -> List[str]:
    """Rows as two-space-indented, left-aligned columns (the last column unpadded)."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  " + "  ".join([c.ljust(w) for c, w in zip(row, widths)] + [row[-1]]) for row in rows]


def render_report(report: WheelsReport) -> str:
    """The text ``aisland wheels`` prints."""
    target = report.target
    lines: List[str] = []
    n = len(report.built)
    if n:
        noun = (
            "1 workspace project that is not on PyPI. Its wheel is"
            if n == 1
            else (f"{n} workspace projects that are not on PyPI. Their wheels are")
        )
        lines.append(f"{target} needs {noun} in {report.wheels_dir} ({_size(report.wheels_bytes)}):")
        lines += _table([[b.project, b.version, f"{b.branch} {b.commit}"] for b in report.built])
    else:
        lines.append(f"{target} needs no workspace project that is not on PyPI; {report.wheels_dir} holds no wheel.")
    lines.append("")

    if report.published:
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

    if report.install_error is not None:
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
                [f.module, f.error + (f"  ({f.distribution})" if f.distribution else "")]
                for f in report.import_failures
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
    else:
        if report.release_first:
            lines.append(f"Release first: {', '.join(report.release_first)}")
        elif report.import_failures:
            lines.append(f"{target} does not import, and no project it takes from PyPI is the cause.")
        if report.behind_only:
            lines.append(f"Behind, nothing breaks: {', '.join(report.behind_only)}")
        if not report.import_failures and not report.behind:
            lines.append(f"PyPI is up to date for {target}.")
    return "\n".join(lines).rstrip() + "\n"


def render_readme(target: str, built: List[BuiltWheel]) -> str:
    """``wheels/README.md``: its generator, the install command, and each wheel's commit.

    No date and no PyPI verdict, so a rebuild of unchanged projects changes no file.
    """
    rows = "\n".join(f"| {b.wheel} | {b.project} | {b.branch} | {b.commit} |" for b in built)
    table = (
        f"| Wheel | Project | Branch | Commit |\n| --- | --- | --- | --- |\n{rows}" if built else "No wheel is needed."
    )
    return f"""\
<!--
=========================================================================
AUTO-GENERATED FILE — DO NOT EDIT BY HAND
Generated by: aisland wheels {target}
Source: aisland/aisland/wheels.py (render_readme)
To regenerate: aisland wheels {target}
=========================================================================
-->
# The wheels {target} needs

The workspace projects {target} depends on that are not on PyPI, each built from the commit
below. Everything else it needs comes from PyPI.

Install {target} from its own folder, editable:

    pip install --find-links wheels -e .

A rebuild keeps each wheel's version, so an environment that already has them takes the new
ones with:

    pip install --find-links wheels --force-reinstall --no-deps wheels/*.whl

{table}
"""
