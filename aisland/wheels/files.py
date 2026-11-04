"""The files `aisland wheels build` writes into ``<project>/wheels/`` beside the wheels, and the zip of that folder.

``README.md`` (what each wheel was built from, how to install), ``wheels.yaml`` (each wheel and its ``install:``,
the user's choice, kept across reruns), ``install.py`` (:mod:`aisland.wheels.install` under a generated header) and
the ``.gitignore``; ``aisland wheels zip`` packs the folder with a ``__main__.py`` (:mod:`aisland.wheels.unzip`), and
``aisland wheels install`` runs that zip into a named interpreter. The run that decides what goes in is
:mod:`aisland.wheels`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aisland.errors import AislandError
from aisland.wheels import bundle as wheels_bundle
from aisland.wheels import install as wheels_install
from aisland.wheels import unzip as wheels_unzip
from aisland.wheels.install import WheelsFileError, read_wheels
from aisland.wheels.report import BuiltWheel, NotBuilt, WheelsScope, provenance_header, regenerate_command

#: The folder inside the project that receives the wheels; the install line names it.
WHEELS_DIR = "wheels"


#: The file written beside the wheels: what each one was built from and how to install.
README_NAME = "README.md"


#: The list of the wheels install.py reads; its ``install:`` values are the user's.
WHEELS_YAML = wheels_install.WHEELS_FILE


#: The generic installer written beside the wheels (a copy of :mod:`aisland.wheels.install`).
INSTALL_SCRIPT = "install.py"


#: The ignore file written into the folder: everything (the default) or only the zip (``--commit``).
GITIGNORE = ".gitignore"


IGNORE_ALL = "*\n"


IGNORE_ZIP = "*.zip\n"


#: The zip ``aisland wheels zip`` writes into the folder; ``python <it>`` unpacks and installs it.
ZIP_NAME = "{project}-wheels.zip"


#: Every zip member's time, so a zip of an unchanged folder is the same file (the zip format's epoch).
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


#: The line that frames a generated header (and the template's own) in the files written.
_HEADER_RULE = "# =========================================================================\n"


def write_wheels(folder: Path, staged: List[Path], files: Dict[str, str]) -> None:
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


def installs_own_wheel(folder: Path) -> bool:
    """Whether ``folder``'s ``wheels.yaml`` lists the project's own wheel with ``install: true``, and the wheel is
    there: ``install.py`` then installs it in place of the project's folder, so nothing needs the project's source."""
    if not (folder / WHEELS_YAML).is_file():
        return False
    project, entries = read_wheels(folder / WHEELS_YAML)
    return any(e.project == project and e.install and (folder / e.wheel).is_file() for e in entries)


def write_zip(target_name: str, folder: Path) -> Path:
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
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, data)
    os.replace(partial, archive)
    return archive


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


def _wheel_table(shipped: List[BuiltWheel], commits: bool) -> str:
    """The README's table of the wheels: each one's project, and its branch and commit unless ``commits`` is off."""
    if not shipped:
        return "No wheel is needed."
    if commits:
        rows = "\n".join(
            f"| {b.wheel} | {b.project} | {b.branch} | {b.commit} | {'yes' if b.needed else 'no'} |" for b in shipped
        )
        columns = "| Wheel | Project | Branch | Commit | Needed |\n| --- | --- | --- | --- | --- |"
    else:
        rows = "\n".join(f"| {b.wheel} | {b.project} | {'yes' if b.needed else 'no'} |" for b in shipped)
        columns = "| Wheel | Project | Needed |\n| --- | --- | --- |"
    return f"{columns}\n{rows}"


def _title_and_intro(target: str, scope: WheelsScope, commits: bool) -> Tuple[str, str]:
    """The README's title, and the paragraph that says which wheels the folder holds for ``scope``."""
    # Where each wheel comes from, in the intro's words: the commit the table names, or (no commits) its last one.
    source, rest, that = ("the commit", "below", "the commit") if commits else ("its last", "commit", "that commit")
    return {
        "unpublished": (
            f"The wheels {target} needs",
            f"The workspace projects {target} depends on that are not on PyPI, each built from {source}\n"
            f"{rest}. Everything else it needs comes from PyPI.",
        ),
        "needed": (
            f"The wheels {target} needs",
            f"Every workspace project {target} depends on that had a checkout where these were built, each\n"
            f"built from {source} {rest} — those on PyPI too, so {that}, not the release, is what\n"
            "installs. Everything else it needs comes from PyPI.",
        ),
        "all": (
            f"The workspace's wheels, for {target}",
            f"Every workspace project that had a checkout where these were built, each built from {source}\n"
            f"{rest}. *Needed* marks the ones {target} depends on; everything else it needs comes from PyPI.",
        ),
    }[scope]


def _how_to_install(target: str, project_wheel: Optional[BuiltWheel], extra: Optional[str]) -> str:
    """The README's install paragraph: from the project's folder or its own wheel, after the uv command of ``extra``."""
    if project_wheel is None:
        how = f"""\
Install {target} from its own folder, editable, with the wheels marked `install: true` in
`wheels/{WHEELS_YAML}` (by default the ones it needs):"""
    else:
        how = f"""\
{target}'s own wheel is here too. Install it with the wheels marked `install: true` in
`wheels/{WHEELS_YAML}` (by default the ones it needs); set its own entry's `install:` to false to
install {target} from its folder, editable, instead:"""
    if extra is None:
        return how
    return f"""\
With uv, from the folder holding {target}, install {target} editable together with every wheel
here: its `pyproject.toml` has the extra `{extra}`, whose uv sources are these files. After the
wheels are rebuilt, the same command takes the new ones.

    uv pip install -e ./{target}[{extra}]

Or with the script beside the wheels, which pip runs too.

{how}"""


def render_readme(
    target: str,
    scope: WheelsScope,
    built: List[BuiltWheel],
    project_wheel: Optional[BuiltWheel] = None,
    extra: Optional[str] = None,
    command_extra: Optional[str] = "",
    commits: bool = True,
) -> str:
    """``wheels/README.md``: its generator, the install command, and each wheel's commit (``project_wheel``, the
    target's own, last); with ``extra``, the uv command that installs the extra first. ``command_extra`` is the
    ``--extra`` the regenerate command names (``""``: ``extra``; ``None``: none, the project remembering it).
    Without ``commits`` (a target not on GitHub) no wheel's branch or commit is written.

    No date and no PyPI verdict, so a rebuild of unchanged projects changes no file.
    """
    flag = extra if command_extra == "" else command_extra
    command = regenerate_command(target, scope, project_wheel is not None, flag)
    header = "\n".join(provenance_header("", command, "aisland/aisland/wheels/files.py (render_readme)"))
    title, intro = _title_and_intro(target, scope, commits)
    how = _how_to_install(target, project_wheel, extra)
    table = _wheel_table(built + ([project_wheel] if project_wheel else []), commits)
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
    commits: bool = True,
) -> str:
    """``wheels/wheels.yaml``: each wheel, its commit (not without ``commits``: a target not on GitHub), whether
    ``target`` needs it, and ``install:``; the target's own wheel (``project_wheel``) last.

    ``install:`` is the value in ``keep`` (the user's, read from the file this one replaces) for a project
    listed there, else whether it is needed (the target's own wheel: true). Strings are written as JSON strings,
    which YAML reads as they are (a version such as ``1.0`` would otherwise read as a number).
    """
    command = regenerate_command(target, scope, project_wheel is not None, extra)
    lines = [_HEADER_RULE.rstrip("\n")]
    # The header's first line, said for this one generated file the user DOES edit: its `install:` values.
    lines += ["# AUTO-GENERATED FILE — edit only the `install:` values (a rerun keeps them)"]
    lines += provenance_header("# ", command, "aisland/aisland/wheels/files.py (render_wheels_yaml)")[1:]
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
        lines += [f"  - wheel: {_string(b.wheel)}", f"    project: {_string(b.project)}"]
        if commits:
            lines.append(f"    commit: {_string(f'{b.branch} {b.commit}')}")
        lines += [
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
    """``wheels/install.py``: :mod:`aisland.wheels.install` unchanged, under a generated header.

    The template's own header (between its first two rule lines) says it is the template; the copy's
    names the command that wrote it.
    """
    template = Path(wheels_install.__file__).read_text(encoding="utf-8")
    shebang, _template_header, rest = template.split(_HEADER_RULE, 2)
    command = regenerate_command(target, scope, include_project, extra)
    header = provenance_header("# ", command, "aisland/aisland/wheels/install.py (copied unchanged below this header)")
    return shebang + _HEADER_RULE + "\n".join(header) + "\n" + _HEADER_RULE + rest


def render_unzip_script(target: str, standalone: bool = False) -> str:
    """A zip's ``__main__.py``: :mod:`aisland.wheels.unzip` under a generated header, naming ``target`` and whether
    the zip installs the project's own wheel (``standalone``: it then runs from any folder)."""
    template = Path(wheels_unzip.__file__).read_text(encoding="utf-8")
    shebang, _template_header, rest = template.split(_HEADER_RULE, 2)
    header = provenance_header(
        "# ", f"aisland wheels zip {target}", "aisland/aisland/wheels/unzip.py (copied below this header)"
    )
    for placeholder, value in (
        ('PROJECT = "<project>"', f"PROJECT = {json.dumps(target)}"),
        ("STANDALONE = False", f"STANDALONE = {standalone}"),
    ):
        if rest.count(placeholder) != 1:
            raise RuntimeError(f"{wheels_unzip.__file__}: expected one {placeholder!r} line to fill in")
        rest = rest.replace(placeholder, value)
    return shebang + _HEADER_RULE + "\n".join(header) + "\n" + _HEADER_RULE + rest
