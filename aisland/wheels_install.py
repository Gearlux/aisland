#!/usr/bin/env python3
# =========================================================================
# THE TEMPLATE of every <project>/wheels/install.py — edit it here, never in a copy.
# Source: aisland/aisland/wheels_install.py
# =========================================================================
"""Install the wheels of this folder, then the project above it, editable — or its own wheel, when listed.

``wheels.yaml`` beside this script lists every wheel ``aisland wheels`` built and whether to install it
(``install:``, yours to edit). The project's own wheel (``aisland wheels build <project> --include-project``)
is listed last: while its ``install:`` is true, it is installed in place of the project's folder, which then
need not be there. The command run names each chosen wheel by its FILE: pip takes a wheel named
by its file over PyPI's wheel of the same version, while a wheel it only finds through ``--find-links``
loses to PyPI's (measured 2026-10-09 on recordstream 0.1.0a2, built from a checkout ahead of its release).

    python wheels/install.py                 # the wheels marked install: true, then the project
    python wheels/install.py --all           # every wheel listed
    python wheels/install.py --needed        # the wheels the project needs
    python wheels/install.py --only confluid,recordstream
    python wheels/install.py --no-project    # the chosen wheels only (neither the folder nor the project's wheel)
    python wheels/install.py --reinstall     # reinstall the chosen wheels first (a rebuild keeps their version)
    python wheels/install.py --dry-run       # print the commands, run nothing
    python wheels/install.py --installer uv  # uv pip instead of pip
    python wheels/install.py -- --index-url https://...   # everything after -- goes to pip / uv

Everything installs into the interpreter running this script. It needs PyYAML to read ``wheels.yaml`` and
installs it first when it is missing (the projects that ship this script depend on it anyway).
"""

import argparse
import importlib.util
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import List, Literal, Optional, Sequence, Tuple, get_args

#: The file beside this script that lists the wheels.
WHEELS_FILE = "wheels.yaml"

Installer = Literal["pip", "uv"]


class WheelsFileError(Exception):
    """``wheels.yaml`` cannot be read; the message names the file and the line."""


@dataclass(frozen=True)
class Entry:
    """One wheel of ``wheels.yaml``; ``line`` is where its entry starts."""

    wheel: str
    project: str
    needed: bool
    install: bool
    line: int


def _yaml() -> ModuleType:
    # Imported here, not at the top: this script must start in an environment without PyYAML, to install it.
    import yaml

    return yaml


def read_wheels(path: Path) -> Tuple[str, List[Entry]]:
    """``(project, entries)`` from ``wheels.yaml``; a malformed file raises ``WheelsFileError`` at its line."""
    yaml = _yaml()
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
        root = yaml.compose(text)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark
        raise WheelsFileError(f"{path}:{mark.line + 1}:{mark.column + 1}: {error.problem}") from error
    if not isinstance(data, dict) or not isinstance(data.get("project"), str):
        raise WheelsFileError(f"{path}:1: expected a mapping with `project:` and `wheels:`")
    nodes = {key.value: value for key, value in root.value}
    items = data.get("wheels") or []
    if not isinstance(items, list):
        raise WheelsFileError(f"{path}:{nodes['wheels'].start_mark.line + 1}: `wheels:` must be a list")
    entries = []
    for item, node in zip(items, nodes["wheels"].value if items else []):
        line = node.start_mark.line + 1
        if not isinstance(item, dict):
            raise WheelsFileError(f"{path}:{line}: a wheel must be a mapping")
        fields = {key.value: value.start_mark.line + 1 for key, value in node.value}
        for name, kind in (("wheel", str), ("project", str), ("needed", bool), ("install", bool)):
            if not isinstance(item.get(name), kind):
                where = fields.get(name, line)
                want = "true or false" if kind is bool else "a name"
                raise WheelsFileError(f"{path}:{where}: `{name}:` must be {want}, not {item.get(name)!r}")
        entries.append(Entry(item["wheel"], item["project"], item["needed"], item["install"], line))
    return data["project"], entries


def choose(entries: List[Entry], which: Literal["install", "all", "needed"], only: Sequence[str] = ()) -> List[Entry]:
    """The entries to install: those marked ``install``, all, the needed ones, or the ``only`` projects by name."""
    if only:
        known = {e.project: e for e in entries}
        unknown = [name for name in only if name not in known]
        if unknown:
            have = ", ".join(sorted(known)) or "none"
            raise WheelsFileError(f"no wheel for {', '.join(map(repr, unknown))} in {WHEELS_FILE} (have: {have})")
        return [known[name] for name in only]
    if which == "all":
        return list(entries)
    if which == "needed":
        return [e for e in entries if e.needed]
    return [e for e in entries if e.install]


def _base(installer: Installer, uv: Optional[str], python: str) -> List[str]:
    if installer == "uv":
        return [str(uv), "pip", "install", "--python", python]
    return [python, "-m", "pip", "install"]


def commands(
    installer: Installer,
    uv: Optional[str],
    python: str,
    folder: Path,
    chosen: List[Entry],
    project: bool,
    reinstall: bool,
    extra: Sequence[str],
    project_wheel: Optional[Entry] = None,
) -> List[List[str]]:
    """The commands to run, in order: the reinstall of the chosen wheels (asked for), then the install.

    ``project`` installs the project: its own wheel when ``project_wheel`` is given, else its folder, editable.
    """
    base = _base(installer, uv, python)
    own = [project_wheel] if project and project_wheel is not None else []
    files = [str(folder / e.wheel) for e in [*chosen, *own]]
    result = []
    if reinstall and files:
        flag = "--reinstall" if installer == "uv" else "--force-reinstall"
        result.append([*base, *extra, flag, "--no-deps", *files])
    if files or project:
        editable = ["-e", str(folder.parent)] if project and project_wheel is None else []
        result.append([*base, *extra, "--find-links", str(folder), *editable, *files])
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install the wheels of this folder, then the project above it, editable — or its own wheel.",
    )
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="every wheel listed in wheels.yaml")
    which.add_argument("--needed", action="store_true", help="the wheels the project needs")
    which.add_argument("--only", default="", help="these projects' wheels, comma-separated")
    parser.add_argument(
        "--no-project", action="store_true", help="install the wheels only: neither the project nor its own wheel"
    )
    parser.add_argument("--reinstall", action="store_true", help="reinstall the chosen wheels first")
    parser.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    parser.add_argument("--installer", choices=get_args(Installer), default="pip", help="pip (default) or uv")
    return parser


def main(argv: Sequence[str], folder: Optional[Path] = None) -> int:
    """Run the install; returns the exit status (2 for a usage or ``wheels.yaml`` error)."""
    folder = folder or Path(__file__).resolve().parent
    argv = list(argv)
    own, extra = (argv[: argv.index("--")], argv[argv.index("--") + 1 :]) if "--" in argv else (argv, [])
    args = _parser().parse_args(own)
    installer: Installer = args.installer
    uv = shutil.which("uv") if installer == "uv" else None
    if installer == "uv" and uv is None:
        print("install.py: --installer uv needs `uv` on PATH", file=sys.stderr)
        return 2
    python = sys.executable
    if importlib.util.find_spec("yaml") is None:
        if args.dry_run:
            print("install.py: --dry-run needs PyYAML to read wheels.yaml; run without it to install PyYAML first")
            return 2
        bootstrap = [*_base(installer, uv, python), *extra, "pyyaml"]
        print(f"$ {shlex.join(bootstrap)}", flush=True)
        if subprocess.run(bootstrap).returncode != 0:
            return 1
    which: Literal["install", "all", "needed"] = "all" if args.all else "needed" if args.needed else "install"
    only = [name.strip() for name in args.only.split(",") if name.strip()]
    try:
        project, entries = read_wheels(folder / WHEELS_FILE)
        own_entry = next((e for e in entries if e.project == project), None)
        chosen = choose([e for e in entries if e is not own_entry], which, only)
    except WheelsFileError as error:
        print(f"install.py: {error}", file=sys.stderr)
        return 2
    project_wheel = own_entry if own_entry is not None and own_entry.install and not args.no_project else None
    shipped = chosen + ([project_wheel] if project_wheel else [])
    missing = [e for e in shipped if not (folder / e.wheel).is_file()]
    if missing:
        for e in missing:
            print(f"install.py: {folder / e.wheel} is listed at {WHEELS_FILE}:{e.line} but missing", file=sys.stderr)
        return 2
    planned = commands(installer, uv, python, folder, chosen, not args.no_project, args.reinstall, extra, project_wheel)
    if not planned:
        print("install.py: nothing to install")
        return 0
    for command in planned:
        print(f"$ {shlex.join(command)}", flush=True)
        if not args.dry_run:
            status = subprocess.run(command).returncode
            if status != 0:
                return status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
