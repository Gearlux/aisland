"""One wheel holding a project and the workspace projects it needs (``aisland wheels build <project> --bundle``).

A wheel carries exactly one ``.dist-info`` — pip refuses one with more ("multiple .dist-info directories
found") — so a bundle is ONE distribution, ``<project>-bundle``, whose files are the merged files of every wheel
it holds. pip then knows only that name: ``pip show <dependency>`` finds nothing, and a package that reads its
version from its own distribution (``importlib.metadata.version``) gets its fallback. That is the price of one
file that installs with plain ``pip install``; the separate wheels beside it keep every name.

What the bundle requires is read from each project's ``pyproject.toml``, never from the built METADATA, where an
extra survives only as an ``extra == "…"`` clause mixed into the environment markers:

- a requirement on a project inside the bundle is replaced by that project's own requirements (its base ones
  and those of every extra the requirement names), each under the requirement's marker, and so on down;
- every other requirement — an outside package, a workspace project taken from PyPI — is kept as written;
- the project's own extras stay extras of the bundle; the extras of the projects inside it are only what their
  requirements name.

The merge refuses what would install wrongly: a wheel that is not pure Python, one path holding different bytes
in two wheels, one entry point name pointing at two objects, a requirement that the bundled version does not
satisfy. Members are written in name order with one fixed time, so an unchanged set of wheels makes the same file.
"""

from __future__ import annotations

import base64
import configparser
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from aisland.errors import AislandError

#: The folder inside ``<project>/wheels/`` that receives the bundle. A subfolder, so neither the zip (which packs
#: the folder's files) nor the run's ``*.whl`` cleanup of the folder ever takes it for one of the separate wheels.
BUNDLE_DIR = "bundle"

#: The bundle's distribution name, from the project's.
BUNDLE_NAME = "{distribution}-bundle"

#: The tags a wheel inside a bundle may carry: the bundle is ``py3-none-any``, so everything in it must be.
_PURE_TAGS = {"py3-none-any", "py2.py3-none-any"}

#: The files of a wheel's dist-info a bundle carries over (each under ``licenses/<distribution>/``).
_LICENSE = re.compile(r"^(licenses/.+|(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*)$", re.IGNORECASE)

#: ``requirements(project, extra)``: a project's base requirements (``extra=None``) or one extra's.
Requirements = Callable[[str, Optional[str]], List[Requirement]]


@dataclass
class BundlePart:
    """One wheel that goes into the bundle.

    Args:
        project: Its folder, the name the report shows.
        distribution: Its distribution name, under which its licence files are kept.
        wheel: The wheel file.
    """

    project: str
    distribution: str
    wheel: Path


@dataclass
class BundleRequirements:
    """What the bundle requires: ``base`` always, ``extras`` per extra of the project, ``python`` its interpreter."""

    base: List[str]
    extras: Dict[str, List[str]] = field(default_factory=dict)
    python: Optional[str] = None


def bundle_wheel_name(distribution: str, version: str) -> str:
    """The file name of the bundle ``distribution`` (``app-bundle``): ``app_bundle-<version>-py3-none-any.whl``."""
    name = re.sub(r"[-_.]+", "_", distribution).lower()
    return f"{name}-{version}-py3-none-any.whl"


# --- what the bundle requires ----------------------------------------------------------------


def _with_marker(requirement: Requirement, *markers: Optional[str]) -> str:
    """``requirement`` with its own marker and ``markers`` joined by ``and`` — each one parenthesized."""
    clauses = [m for m in (str(requirement.marker) if requirement.marker else None, *markers) if m]
    bare = Requirement(str(requirement))
    bare.marker = None
    if not clauses:
        return str(bare)
    joined = clauses[0] if len(clauses) == 1 else " and ".join(f"({c})" for c in clauses)
    # A space before the semicolon: after a URL (`name @ git+https://…`) one is required.
    return str(Requirement(f"{bare} ; {joined}"))


def _expand(
    requirements: Requirements,
    versions: Dict[str, str],
    project: str,
    extra: Optional[str],
    marker: Optional[str],
    seen: Set[Tuple[str, Optional[str]]],
) -> List[str]:
    """The requirements of ``project`` (``extra``: one of its extras), a bundled project's replaced by its own,
    each under ``marker`` (the marker of the requirement that led here)."""
    if (project, extra) in seen:
        return []
    seen.add((project, extra))
    out: List[str] = []
    for requirement in requirements(project, extra):
        name = canonicalize_name(requirement.name)
        if name not in versions:
            out.append(_with_marker(requirement, marker))
            continue
        if not requirement.specifier.contains(versions[name], prereleases=True):
            raise AislandError(
                f"{project} requires {requirement}, but the bundle holds {requirement.name} {versions[name]}"
            )
        inner = str(requirement.marker) if requirement.marker else None
        joined = " and ".join(f"({m})" for m in (inner, marker) if m) if inner and marker else inner or marker
        for named in (None, *sorted(requirement.extras)):
            out += _expand(requirements, versions, name, named, joined, seen)
    return out


def _unique(items: List[str], skip: Sequence[str] = ()) -> List[str]:
    """``items`` in their order, each once, without those in ``skip``."""
    kept: List[str] = []
    for item in items:
        if item not in kept and item not in skip:
            kept.append(item)
    return kept


def bundle_requirements(
    requirements: Requirements,
    target: str,
    target_extras: Sequence[str],
    versions: Dict[str, str],
    requires_python: Sequence[str] = (),
) -> BundleRequirements:
    """What the bundle of ``target`` and the projects in ``versions`` requires (see the module docstring).

    Args:
        requirements: Each project's requirements, by its canonical name (see :data:`Requirements`).
        target: The canonical name of the project the bundle is for.
        target_extras: Its extras, which become the bundle's.
        versions: Every project inside the bundle (``target`` too), canonical name → its wheel's version.
        requires_python: Every bundled project's ``requires-python``; the bundle requires all of them.
    """
    base = _unique(_expand(requirements, versions, target, None, None, set()))
    extras: Dict[str, List[str]] = {}
    for extra in target_extras:
        clause = f'extra == "{extra}"'
        # An extra's requirements without the bundle's base ones, each under the extra's marker.
        found = _unique(_expand(requirements, versions, target, extra, None, set()), base)
        extras[extra] = [_with_marker(Requirement(spec), clause) for spec in found]
    python = SpecifierSet()
    for spec in requires_python:
        python &= SpecifierSet(spec)
    return BundleRequirements(base, extras, str(python) or None)


# --- the merge -------------------------------------------------------------------------------


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _dist_info(archive: zipfile.ZipFile, wheel: Path) -> str:
    """The wheel's one ``<name>-<version>.dist-info`` folder."""
    tops = {name.split("/", 1)[0] for name in archive.namelist() if "/" in name}
    found = sorted(top for top in tops if top.endswith(".dist-info"))
    if len(found) != 1:
        raise AislandError(f"{wheel.name}: expected one .dist-info folder, found {found or 'none'}")
    return found[0]


def _tags(wheel_file: str) -> List[str]:
    return [line.split(":", 1)[1].strip() for line in wheel_file.splitlines() if line.startswith("Tag:")]


def _entry_points(text: str, part: BundlePart) -> Dict[str, Dict[str, str]]:
    parser = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # entry point names keep their case
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise AislandError(f"{part.wheel.name}: cannot read its entry_points.txt: {error}") from error
    return {group: dict(parser.items(group)) for group in parser.sections()}


def render_metadata(
    distribution: str,
    version: str,
    summary: str,
    requires: BundleRequirements,
    description: str,
) -> str:
    """The bundle's METADATA: its name, version, requirements and extras, then ``description`` (Markdown)."""
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {version}",
        f"Summary: {summary}",
    ]
    if requires.python:
        lines.append(f"Requires-Python: {requires.python}")
    lines += [f"Requires-Dist: {spec}" for spec in requires.base]
    for extra, specs in requires.extras.items():
        lines.append(f"Provides-Extra: {extra}")
        lines += [f"Requires-Dist: {spec}" for spec in specs]
    lines.append("Description-Content-Type: text/markdown")
    return "\n".join(lines) + "\n\n" + description


#: The mode a file the bundle writes itself gets, as a zip member's ``external_attr``.
_REGULAR = 0o644 << 16


@dataclass
class _Members:
    """What goes into the bundle: each member's bytes, mode and owner, and what its parts' dist-infos bring."""

    #: ``{path: (bytes, external_attr, owner)}``; the first owner of a path keeps it.
    files: Dict[str, Tuple[bytes, int, str]] = field(default_factory=dict)
    #: Each path or entry point two parts hold differently.
    clashes: List[str] = field(default_factory=list)
    #: ``{group: {name: (target, project)}}``.
    entry_points: Dict[str, Dict[str, Tuple[str, str]]] = field(default_factory=dict)
    top_level: Set[str] = field(default_factory=set)

    def add(self, path: str, data: bytes, attributes: int, owner: str) -> None:
        if path in self.files and self.files[path][0] != data:
            self.clashes.append(f"{path} differs between {self.files[path][2]} and {owner}")
        self.files.setdefault(path, (data, attributes, owner))

    def add_entry_points(self, text: str, part: BundlePart) -> None:
        for group, names in _entry_points(text, part).items():
            for key, value in names.items():
                held = self.entry_points.setdefault(group, {}).get(key)
                if held is not None and held[0] != value:
                    self.clashes.append(
                        f"entry point {group}:{key} is {held[0]} in {held[1]} and {value} in {part.project}"
                    )
                self.entry_points[group].setdefault(key, (value, part.project))


def _take_wheel(members: _Members, part: BundlePart, info: str, data_dir: str) -> None:
    """Every member of ``part``'s wheel into ``members``: its dist-info read for entry points, top-level names and licence
    files (kept under ``licenses/<distribution>/``), its ``.data`` folder renamed to the bundle's. A wheel that is not
    pure Python is refused."""
    with zipfile.ZipFile(part.wheel) as archive:
        own_info = _dist_info(archive, part.wheel)
        own_data = own_info[: -len(".dist-info")] + ".data"
        wheel_file = archive.read(f"{own_info}/WHEEL").decode("utf-8", errors="replace")
        impure = [tag for tag in _tags(wheel_file) if tag not in _PURE_TAGS]
        if impure or "Root-Is-Purelib: false" in wheel_file:
            raise AislandError(
                f"{part.project}'s wheel {part.wheel.name} is built for {', '.join(impure) or 'platlib'}; "
                "a bundle holds pure-Python wheels only"
            )
        for item in archive.infolist():
            if item.is_dir():
                continue
            payload = archive.read(item)
            top, _, rest = item.filename.partition("/")
            if top == own_info:
                if rest == "entry_points.txt":
                    members.add_entry_points(payload.decode("utf-8"), part)
                elif rest == "top_level.txt":
                    members.top_level.update(filter(None, payload.decode("utf-8").splitlines()))
                elif _LICENSE.match(rest):
                    leaf = rest.removeprefix("licenses/")
                    members.add(
                        f"{info}/licenses/{part.distribution}/{leaf}", payload, item.external_attr, part.project
                    )
            elif top == own_data:
                members.add(f"{data_dir}/{rest}", payload, item.external_attr, part.project)
            else:
                members.add(item.filename, payload, item.external_attr, part.project)


def _add_dist_info(members: _Members, info: str, distribution: str, metadata: str) -> None:
    """The bundle's own dist-info: ``metadata``, its WHEEL, and the parts' entry points and top-level names merged."""
    members.add(f"{info}/METADATA", metadata.encode("utf-8"), _REGULAR, distribution)
    wheel_text = "Wheel-Version: 1.0\nGenerator: aisland wheels build --bundle\nRoot-Is-Purelib: true\n"
    members.add(f"{info}/WHEEL", (wheel_text + "Tag: py3-none-any\n").encode(), _REGULAR, distribution)
    if members.entry_points:
        blocks = [
            f"[{group}]\n" + "".join(f"{key} = {value}\n" for key, (value, _) in sorted(names.items()))
            for group, names in sorted(members.entry_points.items())
        ]
        members.add(f"{info}/entry_points.txt", "\n".join(blocks).encode("utf-8"), _REGULAR, distribution)
    if members.top_level:
        top_level = "".join(f"{t}\n" for t in sorted(members.top_level)).encode()
        members.add(f"{info}/top_level.txt", top_level, _REGULAR, distribution)


def _write_bundle(
    files: Dict[str, Tuple[bytes, int, str]], info: str, bundle: Path, date_time: Tuple[int, int, int, int, int, int]
) -> None:
    """Write ``files`` and their RECORD into ``bundle``, every member stamped ``date_time``, by way of a ``.partial``
    file so a failed write leaves no wheel."""
    record = io.StringIO()
    for path in sorted(files):
        record.write(f"{path},{_record_hash(files[path][0])},{len(files[path][0])}\n")
    record.write(f"{info}/RECORD,,\n")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    partial = bundle.with_suffix(".whl.partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(files):
            data, attributes, _ = files[path]
            entry = zipfile.ZipInfo(path, date_time=date_time)
            entry.external_attr = attributes or _REGULAR
            entry.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(entry, data)
        entry = zipfile.ZipInfo(f"{info}/RECORD", date_time=date_time)
        entry.external_attr = _REGULAR
        entry.compress_type = zipfile.ZIP_DEFLATED
        zipped.writestr(entry, record.getvalue())
    partial.replace(bundle)


def merge_wheels(
    parts: Sequence[BundlePart],
    distribution: str,
    version: str,
    metadata: str,
    out_dir: Path,
    date_time: Tuple[int, int, int, int, int, int],
) -> Path:
    """Write the bundle of ``parts`` into ``out_dir`` and return it.

    Every member of every wheel goes in as it is, but its dist-info: of that, the entry points are merged, the
    top-level names joined and the licence files kept under ``licenses/<distribution>/``; a ``.data`` folder is
    renamed to the bundle's. ``metadata`` is the bundle's METADATA (:func:`render_metadata`).
    """
    name = bundle_wheel_name(distribution, version)
    stem = name.split("-py3-none-any.whl")[0]
    info, data_dir = f"{stem}.dist-info", f"{stem}.data"
    members = _Members()
    for part in parts:
        _take_wheel(members, part, info, data_dir)
    if members.clashes:
        raise AislandError("the wheels cannot share one bundle:\n" + "\n".join(f"    {c}" for c in members.clashes))
    _add_dist_info(members, info, distribution, metadata)
    bundle = out_dir / name
    _write_bundle(members.files, info, bundle, date_time)
    return bundle
