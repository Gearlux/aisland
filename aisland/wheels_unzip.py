#!/usr/bin/env python3
# =========================================================================
# THE TEMPLATE of every <project>-wheels.zip's __main__.py — edit it here, never in a copy.
# Source: aisland/aisland/wheels_unzip.py
# =========================================================================
"""Unpack this zip's wheels into the project's ``wheels/`` folder, then run its ``install.py``.

Python runs a zip that holds a ``__main__.py``, so on a machine without the workspace (and without aisland)
the zip ``aisland wheels zip`` writes installs with plain Python, from the project's folder:

    python navigaitor-wheels.zip                        # unpack into ./wheels, then python wheels/install.py
    python navigaitor-wheels.zip --dry-run              # every other argument goes to install.py
    python navigaitor-wheels.zip --into ../navigaitor --unpack-only

The folder must be the project's: its ``pyproject.toml`` names the project this zip was built for, or nothing
is unpacked. The ``*.whl`` already in ``wheels/`` are replaced; any other file there stays. Standard library
only: it runs before anything is installed.
"""

import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

#: The project this zip was built for; the generated copy names it.
PROJECT = "<project>"

#: The folder inside the zip (and inside the project) that holds the wheels.
_FOLDER = "wheels"


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _split(argv: Sequence[str]) -> Tuple[Optional[str], bool, List[str], Optional[str]]:
    """``(into, unpack_only, rest, error)``: this script's two options, everything else for install.py."""
    into: Optional[str] = None
    unpack_only = False
    rest: List[str] = []
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--":
            rest += [arg, *args]
            break
        if arg == "--unpack-only":
            unpack_only = True
        elif arg == "--into":
            if not args:
                return None, False, [], "--into needs a folder"
            into = args.pop(0)
        elif arg.startswith("--into="):
            into = arg.split("=", 1)[1]
        else:
            rest.append(arg)
    return into, unpack_only, rest, None


def _project_name(pyproject: Path) -> Optional[str]:
    with open(pyproject, "rb") as handle:
        name = (tomllib.load(handle).get("project") or {}).get("name")
    return name if isinstance(name, str) else None


def main(argv: Sequence[str], archive: Optional[Path] = None, cwd: Optional[Path] = None) -> int:
    """Unpack, then run install.py with the rest of the line; returns the exit status (2: usage or wrong folder)."""
    archive = archive or Path(__file__).resolve().parent  # run as `python x.zip`, __file__ is x.zip/__main__.py
    into_arg, unpack_only, rest, error = _split(argv)
    if error:
        print(f"{archive.name}: {error}", file=sys.stderr)
        return 2
    project_dir = Path(into_arg) if into_arg else (cwd or Path.cwd())
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        print(
            f"{archive.name}: run it from the {PROJECT} folder, or name it with --into ({pyproject} is missing)",
            file=sys.stderr,
        )
        return 2
    found = _project_name(pyproject)
    if found is None or _canonical(found) != _canonical(PROJECT):
        print(
            f"{archive.name}: run it from the {PROJECT} folder, or name it with --into ({pyproject} names {found})",
            file=sys.stderr,
        )
        return 2
    folder = project_dir / _FOLDER
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.whl"):
        old.unlink()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.namelist():
            if member.startswith(f"{_FOLDER}/") and not member.endswith("/"):
                (folder / member.split("/", 1)[1]).write_bytes(zipped.read(member))
    if unpack_only:
        print(f"unpacked {archive.name} into {folder}")
        return 0
    return subprocess.run([sys.executable, str(folder / "install.py"), *rest]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
