"""The checks of `aisland wheels build`: what a wheel holds, and the project installed in an EMPTY venv.

:func:`wheel_differences` compares a published dependency's PyPI wheel with the one its checkout builds, file by file;
:func:`install_and_import` (and :func:`install_bundle_and_import`) installs the project in an empty venv and imports
every module of it and of its wheels; :func:`attribute_failure` names the project whose release lacks what a failed
import needed. The run that calls them is :mod:`aisland.wheels`.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from aisland.wheels.files import INSTALL_SCRIPT
from aisland.wheels.report import ImportRun, ModuleFailure, PublishedDependency

#: Where uv finds PyPI, for the builds and the empty-venv install. Empty = uv's own default
#: (pypi.org, or the index the user's uv configuration names). A test points it at a local folder.
PYPI_INDEX_ARGS: Tuple[str, ...] = ()


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


#: What an empty venv's check returns: the install's error (``None`` when it installed), the modules that failed to
#: import, the modules that imported.
CheckResult = Tuple[Optional[str], Dict[str, ModuleFailure], List[str]]


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
        install(python, [*config, *PYPI_INDEX_ARGS]), cwd=scratch, env=env, capture_output=True, text=True
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


def owner_of(module: Optional[str], owners: Dict[str, str]) -> Optional[str]:
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


def names_only_here(published: Path, local: Path, differing: List[str]) -> Set[str]:
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
    return owner_of(inside[0].removesuffix(".py"), owners)


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
