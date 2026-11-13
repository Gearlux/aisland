#!/usr/bin/env python3
# =========================================================================
# THE TEMPLATE of every <project>-wheels.zip's __main__.py — edit it here, never in a copy.
# Source: aisland/aisland/wheels/unzip.py
# =========================================================================
"""Unpack this zip's wheels into the project's ``wheels/`` folder, then run its ``install.py``.

Python runs a zip that holds a ``__main__.py``, so on a machine without the workspace (and without aisland)
the zip ``aisland wheels zip`` writes installs with plain Python, from the project's folder:

    python models-wheels.zip                            # unpack into ./wheels, then python wheels/install.py
    python models-wheels.zip --dry-run                  # every other argument goes to install.py
    python models-wheels.zip --into ../models --unpack-only

The folder must be the project's: its ``pyproject.toml`` names the project this zip was built for, or nothing
is unpacked. A zip that carries the project's own wheel (``aisland wheels build <project> --include-project``)
needs no source, so it also runs in a folder of no project — any folder without a ``pyproject.toml`` — unless its
``wheels/`` holds another project's wheels. The ``*.whl`` already in ``wheels/`` are replaced; any other file
there stays. Standard library only: it runs before anything is installed.
"""

import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

#: The project this zip was built for; the generated copy names it.
PROJECT = "<project>"

#: Whether this zip installs the project's own wheel, so it needs no source and runs from any folder; the
#: generated copy says.
STANDALONE = False

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


def _wheels_project(folder: Path) -> Optional[str]:
    """The ``project:`` an unpacked ``wheels.yaml`` names (this script reads no YAML: aisland writes the line as
    a JSON string), or None when the folder has none."""
    listing = folder / "wheels.yaml"
    if not listing.is_file():
        return None
    match = re.search(r'^project:\s*"([^"]*)"', listing.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def _members(zipped: zipfile.ZipFile) -> Union[List[Tuple[str, str]], str]:
    """Each ``wheels/<name>`` member with the file name it is unpacked as, or why the zip is refused: the folder is
    flat, so a name with a folder in it (``wheels/../x``, ``wheels/a/b``, ``wheels/C:x``) would write elsewhere."""
    members: List[Tuple[str, str]] = []
    for member in zipped.namelist():
        if not member.startswith(f"{_FOLDER}/") or member.endswith("/"):
            continue
        name = member.split("/", 1)[1]
        if name in ("", ".", "..") or any(sign in name for sign in ("/", "\\", ":")):
            return f"its member {member!r} is no file of the {_FOLDER}/ folder"
        members.append((member, name))
    return members


def main(argv: Sequence[str], archive: Optional[Path] = None, cwd: Optional[Path] = None) -> int:
    """Unpack, then run install.py with the rest of the line; returns the exit status (2: usage or wrong folder)."""
    archive = archive or Path(__file__).resolve().parent  # run as `python x.zip`, __file__ is x.zip/__main__.py
    into_arg, unpack_only, rest, error = _split(argv)
    if error:
        print(f"{archive.name}: {error}", file=sys.stderr)
        return 2
    project_dir = Path(into_arg) if into_arg else (cwd or Path.cwd())
    pyproject = project_dir / "pyproject.toml"
    where = f"the {PROJECT} folder or a folder of no project" if STANDALONE else f"the {PROJECT} folder"
    if pyproject.is_file():
        found = _project_name(pyproject)
        if found is None or _canonical(found) != _canonical(PROJECT):
            print(
                f"{archive.name}: run it from {where}, or name it with --into ({pyproject} names {found})",
                file=sys.stderr,
            )
            return 2
    elif not STANDALONE:
        print(f"{archive.name}: run it from {where}, or name it with --into ({pyproject} is missing)", file=sys.stderr)
        return 2
    folder = project_dir / _FOLDER
    if not pyproject.is_file() and any(folder.glob("*.whl")):
        owner = _wheels_project(folder)
        if owner is None or _canonical(owner) != _canonical(PROJECT):
            print(
                f"{archive.name}: {folder} holds the wheels of {owner or 'something else'}, which this zip would "
                f"replace; run it from another folder, or name one with --into",
                file=sys.stderr,
            )
            return 2
    with zipfile.ZipFile(archive) as zipped:
        members = _members(zipped)
        if isinstance(members, str):
            print(f"{archive.name}: {members}; nothing was unpacked", file=sys.stderr)
            return 2
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.whl"):
            old.unlink()
        for member, name in members:
            (folder / name).write_bytes(zipped.read(member))
    if unpack_only:
        print(f"unpacked {archive.name} into {folder}")
        return 0
    return subprocess.run([sys.executable, str(folder / "install.py"), *rest]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
