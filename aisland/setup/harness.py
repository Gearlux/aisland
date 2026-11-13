"""The agent harness: the commands, helper scripts and rules every AI coding session in the workspace reads.

A workspace combines HARNESS SETS, listed in ``workspace.toml`` (:data:`SECTION` ``sets``, in order; without the
table, aisland's own):

* ``"aisland"`` — aisland's own set, in the package (``aisland/harness/``);
* the name of a project of the workspace — the folder its ``pyproject.toml`` names in ``[tool.aisland] harness``,
  else the project folder itself;
* a folder of the workspace;
* the name an installed package registers through the :data:`ENTRY_POINT_GROUP` entry point (its value: the package
  holding the set);
* ``{ git = "<url>", ref = "<tag, branch or commit>" }`` — a repository elsewhere, cloned into aisland's state
  folder and fetched again only when its url or ref changes.

Any of them may carry ``exclude = [...]``, the set's files left out (``{ set = "aisland", exclude = [...] }``).

A set's folder says what it brings in ``harness.toml`` (:data:`SET_FILE`): ``rules`` (one file), ``commands`` and
``scripts`` (glob patterns), and ``[files]`` (a pattern → a place in the workspace, a folder when it ends in ``/``).
A folder without one is read in the places agents use (:func:`_usual_places`): ``AGENTS.md`` (else ``CLAUDE.md``),
``.claude/commands/*.md``, ``.cursor/rules/*`` and ``.github/copilot-instructions.md`` — so a project that knows
nothing of aisland can be one.

Each set's rules land in ``.agents/<set>/AGENTS.md``, its commands in ``.agents/workflows/`` (``.claude/commands``
links there, at the root and in every project folder, as Claude Code reads only the folder a session starts in), its
scripts in ``.agents/scripts/``, its other files where it says. ``.agents/rules.md`` (:data:`RULES_INDEX`) imports
every set's rules in the list's order; the workspace's ``AGENTS.md`` imports that one file with its first line. Two
sets placing a file at the same path are refused before anything is installed, naming both: one of them excludes it.

How a file is installed follows how aisland is (:data:`aisland.locations.FROM_CHECKOUT`): from a checkout each file
is a relative LINK to its source, so an edit made through the workspace is an edit of the set's own file; from a
wheel it is a COPY. A copy is replaced on the next install only while it is still the copy aisland wrote —
:data:`RECORD` holds each copy's fingerprint, and each link —, so a copy someone edited is kept and named, never
overwritten. A file under an installed name that aisland never wrote is kept the same way, and any other file in
those folders (a command of the workspace's own) is never touched. A file no set ships any more is taken away, unless
it was edited.

The installed files are per machine: each is listed in the root ``.gitignore`` by its own path. A workspace without
an ``AGENTS.md`` gets one that imports the rules, with ``CLAUDE.md`` and ``GEMINI.md`` linking to it; an existing
``AGENTS.md`` is never edited — the report names the missing import line instead.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Literal, Optional, Tuple, TypedDict

from aisland import atomic, git_exclude, gitcmd, locations
from aisland import pyproject as pyproject_toml
from aisland.errors import AislandError
from aisland.toml_places import FORMAT_KEY, Where, format_refusal, key_line, table_line
from aisland.workspace import file as workspace_file
from aisland.workspace.model import Workspace
from aisland.workspace.projects import workspace_folders

logger = logging.getLogger(__name__)

#: aisland's own set, in the package (a wheel carries it: ``[tool.setuptools.package-data]``).
BUILTIN = "aisland"
HARNESS_DIR = locations.PACKAGE_DIR / "harness"
#: The file describing a set, and the entry point group through which an installed package registers one.
SET_FILE = "harness.toml"
ENTRY_POINT_GROUP = "aisland.harnesses"
#: ``workspace.toml``'s table and key listing the sets, and a project's ``[tool.aisland]`` key naming its set.
SECTION = workspace_file.HARNESS_SECTION
SETS_KEY = workspace_file.HARNESS_SETS_KEY
PROJECT_KEY = "harness"
#: Where the workspace's agents read the commands, the helper scripts and each set's rules.
AGENTS = ".agents"
WORKFLOWS = ".agents/workflows"
SCRIPTS = ".agents/scripts"
RULES_INDEX = ".agents/rules.md"
#: aisland's own rules, where every workspace has read them: ``.agents/<set>/AGENTS.md`` for the set ``aisland``.
RULES = ".agents/aisland/AGENTS.md"
#: What aisland installed: each copy's fingerprint, each link (``"link"``).
RECORD = ".agents/installed.json"
#: The first line of a workspace ``AGENTS.md`` that takes every set's rules.
IMPORT_LINE = f"@{RULES_INDEX}"
#: Seconds a git set's clone or fetch may take.
CLONE_SECONDS = 600.0

#: The places an agent reads, used for a folder without ``harness.toml`` (:func:`_usual_places`).
_USUAL_RULES = ("AGENTS.md", "CLAUDE.md")
_USUAL_COMMANDS = ".claude/commands"
_USUAL_FILES = {
    ".cursor/rules/*": ".cursor/rules/",
    ".github/copilot-instructions.md": ".github/copilot-instructions.md",
}
#: The keys of ``harness.toml``, of a ``sets`` entry and of a git one.
_SET_KEYS = ("format", "rules", "commands", "scripts", "files")
#: The format of ``harness.toml`` this aisland reads; a set without ``format`` is format 1, one asking for a newer
#: format is refused naming the upgrade.
SET_FORMAT = 1
_ENTRY_KEYS = ("set", "git", "ref", "name", "exclude")
#: Where a set's files may not land: aisland's own bookkeeping, the repository's.
_RESERVED = (RULES_INDEX, RECORD)
_RESERVED_NAMES = ("workflows", "scripts")
#: A set's name is one plain path part: it names ``.agents/<name>/`` and, for a git set, its clone's folder.
_SET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

SetKind = Literal["aisland", "project", "folder", "package", "git"]

_STARTER_AGENTS = f"""{IMPORT_LINE}

# Workspace rules

The line above imports the rules of the workspace's harness sets (`aisland setup` writes `{RULES_INDEX}` from
`workspace.toml`'s `[{SECTION}] {SETS_KEY}`; change a set's rules in the set, never in the installed files). This
workspace's own rules go below; a rule for one project goes in that project's `AGENTS.md`. `CLAUDE.md` and
`GEMINI.md` link to this file.
"""

_INDEX_HEADER = f"""<!--
Generated by: aisland setup (aisland/setup/harness.py) from workspace.toml's [{SECTION}] {SETS_KEY}, every set's rules in order.
To regenerate: aisland workspace harness install
-->
"""


class HarnessError(AislandError, ValueError):
    """A harness set aisland cannot use; the message names the file and the line."""


@dataclass(frozen=True)
class HarnessSet:
    """One set: its name (``.agents/<name>/``), where it comes from, and each file it installs (source → place)."""

    name: str
    kind: SetKind
    folder: Path
    files: List[Tuple[Path, str]] = field(default_factory=list)
    rules: Optional[str] = None  # the place its rules land, when it has any

    def count(self, folder: str) -> int:
        return sum(1 for _source, place in self.files if place.startswith(folder + "/"))


# --- reading a set -------------------------------------------------------------------------------------------------


def _matches(folder: Path, pattern: str) -> List[Path]:
    """The files under ``folder`` a pattern names, sorted; a pattern leaving the folder names none."""
    if PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts:
        return []
    return sorted(path for path in folder.glob(pattern) if path.is_file())


def _place(where: Where, destination: str, source: Path) -> str:
    """The workspace path ``source`` lands at for ``destination`` (a folder when it ends in ``/``), checked."""
    place = destination + source.name if destination.endswith("/") else destination
    parts = PurePosixPath(place).parts
    # `.git` in any case and at any depth: a case-insensitive disk (macOS's) reads `.GIT/hooks` as git's own hooks.
    into_git = any(part.casefold() == ".git" for part in parts)
    if PurePosixPath(place).is_absolute() or ".." in parts or not parts or into_git or place in _RESERVED:
        raise HarnessError(f"{where}: {destination!r} is no place in the workspace a harness may write")
    return place


def _from_file(folder: Path) -> Tuple[Optional[Path], List[Tuple[Path, str]]]:
    """The rules and the other files of a set described by its ``harness.toml``; refused naming its line."""
    path = folder / SET_FILE
    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HarnessError(f"{path}: {exc}") from exc

    def at(key: str, table: Optional[int] = None) -> Where:
        return Where(path, key_line(text, table, key) if table else key_line(text, 0, key))

    refusal = format_refusal(at(FORMAT_KEY), SET_FILE, data.get(FORMAT_KEY, SET_FORMAT), SET_FORMAT)
    if refusal:
        raise HarnessError(refusal)
    for key in data:
        if key not in _SET_KEYS:
            raise HarnessError(f"{at(key)}: {key} is no setting of a harness set (it has {', '.join(_SET_KEYS)})")
    rules: Optional[Path] = None
    if "rules" in data:
        if not isinstance(data["rules"], str) or not (folder / data["rules"]).is_file():
            raise HarnessError(f"{at('rules')}: rules must name a file of the set, as rules = \"AGENTS.md\"")
        rules = folder / data["rules"]
    files: List[Tuple[Path, str]] = []
    for key, destination in (("commands", WORKFLOWS + "/"), ("scripts", SCRIPTS + "/")):
        patterns = data.get(key, [])
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise HarnessError(f'{at(key)}: {key} must be a list of patterns, as {key} = ["{key}/*"]')
        files += [
            (source, _place(at(key), destination, source)) for item in patterns for source in _matches(folder, item)
        ]
    table = data.get("files", {})
    header = table_line(text, "files")
    if not isinstance(table, dict) or not all(isinstance(value, str) for value in table.values()):
        raise HarnessError(f'{at("files")}: [files] maps a pattern to a place, as "cursor/*.mdc" = ".cursor/rules/"')
    for pattern, destination in table.items():
        where = at(pattern, header)
        found = _matches(folder, pattern)
        if not destination.endswith("/") and len(found) > 1:
            raise HarnessError(f"{where}: {pattern!r} names {len(found)} files and {destination!r} is one file")
        files += [(source, _place(where, destination, source)) for source in found]
    return rules, files


def _usual_places(folder: Path) -> Tuple[Optional[Path], List[Tuple[Path, str]]]:
    """A set without ``harness.toml``: the files where agents read them. ``.claude/commands`` that is a link (the one
    aisland makes in a project) is the workspace's own commands, never the set's."""
    rules = next((folder / name for name in _USUAL_RULES if (folder / name).is_file()), None)
    commands = folder / _USUAL_COMMANDS
    files = []
    if commands.is_dir() and not commands.is_symlink():
        files += [(source, f"{WORKFLOWS}/{source.name}") for source in _matches(commands, "*.md")]
    for pattern, destination in _USUAL_FILES.items():
        files += [(source, _place(Where(folder, None), destination, source)) for source in _matches(folder, pattern)]
    return rules, files


def _check_name(name: str, where: Where) -> None:
    """Refuse a set name that is aisland's own folder or no plain path part (``..``, ``a/b``, ``/``)."""
    if name in _RESERVED_NAMES:
        raise HarnessError(f"{where}: {name!r} cannot name a harness set (.agents/{name} is aisland's own folder)")
    if not _SET_NAME.fullmatch(name):
        raise HarnessError(
            f"{where}: {name!r} cannot name a harness set: a name is letters, digits, '.', '_' and '-', "
            'starting with a letter or digit, as name = "rules"'
        )


def load_set(folder: Path, name: str, kind: SetKind, exclude: List[str], where: Where) -> HarnessSet:
    """The set in ``folder``: what its ``harness.toml`` lists, else what the usual places hold, less ``exclude``."""
    _check_name(name, where)
    rules, files = _from_file(folder) if (folder / SET_FILE).is_file() else _usual_places(folder)
    for pattern in exclude:
        relative = [source.relative_to(folder).as_posix() for source, _place in files]
        relative += [rules.relative_to(folder).as_posix()] if rules else []
        if not any(fnmatch.fnmatchcase(path, pattern) for path in relative):
            raise HarnessError(f"{where}: exclude {pattern!r} names no file of the {name!r} harness set")

    def kept(source: Path) -> bool:
        return not any(fnmatch.fnmatchcase(source.relative_to(folder).as_posix(), pattern) for pattern in exclude)

    files = [(source, place) for source, place in files if kept(source)]
    rules_place = None
    if rules is not None and kept(rules):
        rules_place = f"{AGENTS}/{name}/AGENTS.md"
        files.append((rules, rules_place))
    return HarnessSet(name, kind, folder, files, rules_place)


# --- finding the sets ----------------------------------------------------------------------------------------------


def registered() -> Dict[str, Path]:
    """Every set an installed package registers (:data:`ENTRY_POINT_GROUP`): its name and its folder."""
    sets: Dict[str, Path] = {}
    for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            module = importlib.import_module(entry.value)
        except ImportError:
            continue
        location = getattr(module, "__file__", None)
        if location:
            sets[entry.name] = Path(location).parent
    return sets


def _project_set(root: Path, name: str, path: str, where: Where) -> Path:
    """A project's set: the folder its ``[tool.aisland] harness`` names, else the project folder itself."""
    folder = root / path
    if not (folder / "pyproject.toml").is_file():
        raise HarnessError(f"{where}: project {name!r} has no checkout here ({folder} holds no pyproject.toml)")
    own = pyproject_toml.read(folder / "pyproject.toml").get("tool", {}).get("aisland", {}).get(PROJECT_KEY)
    if own is None:
        return folder
    if not isinstance(own, str) or not (folder / own).is_dir():
        text = (folder / "pyproject.toml").read_text(encoding="utf-8")
        header = table_line(text, "tool", "aisland")
        declared = Where(folder / "pyproject.toml", key_line(text, header, PROJECT_KEY) if header else None)
        raise HarnessError(f'{declared}: {PROJECT_KEY} must name a folder of the project, as {PROJECT_KEY} = "harness"')
    return folder / own


def _named(root: Path, name: str, where: Where) -> Tuple[Path, SetKind]:
    """The folder of the set ``name``: aisland's, a project's, a folder of the workspace, an installed package's."""
    if name == BUILTIN:
        return HARNESS_DIR, "aisland"
    for project, path, _active in workspace_folders(root):
        if project == name or path == name:
            return _project_set(root, project, path, where), "project"
    if (root / name).is_dir() and not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts:
        return root / name, "folder"
    sets = registered()
    if name in sets:
        return sets[name], "package"
    known = ", ".join(sorted({BUILTIN, *sets}))
    raise HarnessError(
        f"{where}: {name!r} is no project or folder of the workspace and no harness set an installed package "
        f"registers (the packages' sets: {known})"
    )


def clones_dir() -> Path:
    """Where a git set is cloned: aisland's state folder of the workspace."""
    return locations.state_dir() / "data" / "harnesses"


def _clone(url: str, ref: str, name: str, where: Where) -> Path:
    """The checkout of ``url`` at ``ref``, cloned once and fetched again only when the url or the ref changes."""
    folder = clones_dir() / name
    stamp = clones_dir() / f"{name}.source"
    if folder.parent != clones_dir():  # _check_name keeps a name one plain part; never remove a folder outside
        raise HarnessError(f"{where}: {name!r} cannot name a harness set")
    wanted = f"{url} {ref}"
    if folder.is_dir() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == wanted:
        return folder
    if folder.exists():
        shutil.rmtree(folder)
    stamp.unlink(missing_ok=True)
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        cloned = gitcmd.git(folder.parent, "clone", "--quiet", "--", url, name, network=True, timeout=CLONE_SECONDS)
    except subprocess.TimeoutExpired as late:
        raise HarnessError(f"{where}: cloning {url} did not finish within {CLONE_SECONDS:.0f} s") from late
    if cloned.returncode != 0:
        reason = (cloned.stderr or cloned.stdout).strip().splitlines()
        raise HarnessError(f"{where}: could not clone {url}: {reason[-1] if reason else 'git failed'}")
    for candidate in (ref, f"origin/{ref}"):
        if gitcmd.git(folder, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").returncode == 0:
            gitcmd.git(folder, "checkout", "--quiet", "--detach", candidate, check=True)
            atomic.write_text(stamp, wanted + "\n")
            return folder
    shutil.rmtree(folder)
    raise HarnessError(f"{where}: {url} has no tag, branch or commit {ref!r}")


def _strings(value: object) -> Optional[List[str]]:
    return list(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else None


def sets(root: Path) -> List[HarnessSet]:
    """The workspace's harness sets in ``workspace.toml``'s order (aisland's own when it names none), each refusal
    naming the file and the line; two sets placing a file at one path are refused naming both."""
    path = root / workspace_file.FILE_NAME
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    table = workspace_file.load(root).harness
    header = table_line(text, SECTION)
    where = Where(path, key_line(text, header, SETS_KEY) if header else None)
    entries = table.get(SETS_KEY, [BUILTIN]) if table else [BUILTIN]
    if not isinstance(entries, list):
        raise HarnessError(f'{where}: [{SECTION}] {SETS_KEY} must be a list, as {SETS_KEY} = ["aisland", "agents"]')
    found: List[HarnessSet] = []
    for entry in entries:
        found.append(_one(root, entry, where))
    names: Dict[str, HarnessSet] = {}
    owners: Dict[str, str] = {}
    for harness_set in found:
        if harness_set.name in names:
            raise HarnessError(f'{where}: two harness sets are named {harness_set.name!r}: give one a name = "…"')
        names[harness_set.name] = harness_set
        for _source, place in harness_set.files:
            if place in owners:
                raise HarnessError(
                    f"{where}: two harness sets ship {place}: {owners[place]} and {harness_set.name} — exclude it "
                    f"from one of them in [{SECTION}] {SETS_KEY}"
                )
            owners[place] = harness_set.name
    return found


def _one(root: Path, entry: object, where: Where) -> HarnessSet:
    """One entry of ``sets``: a name, ``{ set = …, exclude = […] }`` or ``{ git = …, ref = …, name = …, exclude }``."""
    if isinstance(entry, str):
        folder, kind = _named(root, entry, where)
        return load_set(folder, PurePosixPath(entry).name, kind, [], where)
    if not isinstance(entry, dict):
        raise HarnessError(f'{where}: each of {SETS_KEY} is a name or a table, as {{ set = "aisland", exclude = [] }}')
    unknown = [key for key in entry if key not in _ENTRY_KEYS]
    if unknown:
        raise HarnessError(
            f"{where}: {unknown[0]} is no setting of a harness set entry (it has {', '.join(_ENTRY_KEYS)})"
        )
    exclude = _strings(entry.get("exclude", []))
    if exclude is None:
        raise HarnessError(f'{where}: exclude must be a list of the set\'s files, as exclude = ["commands/ship.md"]')
    if "git" in entry:
        url, ref = entry.get("git"), entry.get("ref")
        if not isinstance(url, str) or not isinstance(ref, str) or not ref or "set" in entry:
            raise HarnessError(f'{where}: a git set names its url and a ref, as {{ git = "<url>", ref = "v1" }}')
        name = entry.get("name") or workspace_file.last_part(url)  # judged by _check_name below
        if not isinstance(name, str) or not name:
            raise HarnessError(f'{where}: name the git set, as name = "rules"')
        _check_name(name, where)
        return load_set(_clone(url, ref, name, where), name, "git", exclude, where)
    if not isinstance(entry.get("set"), str):
        raise HarnessError(f'{where}: a table of {SETS_KEY} names its set, as {{ set = "aisland" }} or {{ git = … }}')
    reference: str = entry["set"]
    folder, kind = _named(root, reference, where)
    given = entry.get("name")
    return load_set(
        folder, given if isinstance(given, str) and given else PurePosixPath(reference).name, kind, exclude, where
    )


def shipped_files() -> List[Tuple[Path, str]]:
    """aisland's own set's files, each with the workspace path it is installed at."""
    return load_set(HARNESS_DIR, BUILTIN, "aisland", [], Where(HARNESS_DIR / SET_FILE, None)).files


# --- installing ----------------------------------------------------------------------------------------------------


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_record(root: Path) -> Dict[str, str]:
    record = root / RECORD
    if not record.is_file():
        return {}
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError):
        return {}  # an unreadable record only means no copy counts as aisland's: each is kept, none lost
    return {str(name): str(value) for name, value in data.items()} if isinstance(data, dict) else {}


def _write_record(root: Path, record: Dict[str, str]) -> None:
    path = root / RECORD
    if not record:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(dict(sorted(record.items())), indent=2) + "\n")


def _owner(name: str) -> str:
    return "aisland's" if name == BUILTIN else f"the {name} harness's"


def _replaceable(target: Path, name: str, source: Path, record: Dict[str, str]) -> bool:
    """Whether ``target`` may be replaced: a link, a copy aisland wrote, or a file that already equals the source."""
    if target.is_symlink():
        return True
    fingerprint = _fingerprint(target)
    return fingerprint == record.get(name) or (source.is_file() and fingerprint == _fingerprint(source))


def _install_one(root: Path, source: Path, name: str, owner: str, link: bool, record: Dict[str, str]) -> Optional[str]:
    """Install one file; returns the line to report, or None when there is nothing to say."""
    target = root / name
    relative = os.path.relpath(source, target.parent)
    if link and target.is_symlink() and os.readlink(target) == relative:
        record[name] = "link"
        return None
    if not link and target.is_file() and not target.is_symlink() and _fingerprint(target) == _fingerprint(source):
        record[name] = _fingerprint(target)
        return None
    if os.path.lexists(target):
        if not _replaceable(target, name, source, record):
            return f"kept your edited {name} — delete it to take {_owner(owner)}"
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if link:
        os.symlink(relative, target)
        record[name] = "link"
    else:
        target.write_bytes(source.read_bytes())
        record[name] = _fingerprint(target)
    return None


def _remove_dropped(root: Path, installed: List[str], record: Dict[str, str]) -> List[str]:
    """Take away what an earlier install put in and no set ships now: a link, or a copy still as aisland wrote it.
    An edited copy stays, named. A link in the commands or scripts folder into aisland's own set counts too."""
    report = []
    candidates = set(record)
    for folder in (WORKFLOWS, SCRIPTS):
        if (root / folder).is_dir():
            candidates.update(
                f"{folder}/{path.name}"
                for path in (root / folder).iterdir()
                if path.is_symlink() and Path(os.path.realpath(path)).is_relative_to(os.path.realpath(HARNESS_DIR))
            )
    for name in sorted(candidates - set(installed)):
        target = root / name
        written = record.pop(name, None)
        if target.is_symlink():
            target.unlink()
        elif target.is_file():
            if _fingerprint(target) == written:
                target.unlink()
            else:
                report.append(f"kept your edited {name} — no harness set of the workspace ships it any more")
    return report


def _link_claude_commands(root: Path, folder: Path, own_set: bool) -> Optional[str]:
    """``<folder>/.claude/commands`` → the workspace's ``.agents/workflows``, where Claude Code looks for commands.

    Claude Code reads ``.claude/commands`` of the folder a session starts in, not of the workspace above it (measured
    2026-10-20: started in a project folder it listed none of the workspace's commands), so the root and every project
    folder get the link. A project whose own folder is one of the sets keeps its folder, said nothing about: its
    commands are the set's."""
    commands = folder / ".claude" / "commands"
    wanted = os.path.relpath(root / WORKFLOWS, commands.parent)
    if commands.is_symlink():
        if os.readlink(commands) == wanted:
            return None
        commands.unlink()
    elif commands.exists():
        if own_set:
            return None
        where = commands.relative_to(root).as_posix()
        return (
            f"{where} is a folder of its own — move its files into .agents/workflows and delete it, "
            "so Claude finds the harness's commands"
        )
    commands.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(wanted, commands)
    return None


def _write_index(root: Path, found: List[HarnessSet]) -> None:
    """``.agents/rules.md``: an import of each set's rules, in the list's order; removed when no set has rules."""
    index = root / RULES_INDEX
    places = [harness_set.rules for harness_set in found if harness_set.rules]
    if not places:
        index.unlink(missing_ok=True)
        return
    text = _INDEX_HEADER + "".join(f"@{place}\n" for place in places)
    index.parent.mkdir(parents=True, exist_ok=True)
    if not index.is_file() or index.read_text(encoding="utf-8") != text:
        atomic.write_text(index, text)


def _workspace_agents_md(root: Path) -> Optional[str]:
    """Write ``AGENTS.md`` (and ``CLAUDE.md`` / ``GEMINI.md`` linking to it) when there is none; name a missing import
    line in an existing one, never editing it."""
    agents = root / "AGENTS.md"
    if os.path.lexists(agents):
        lines = agents.read_text().splitlines() if agents.is_file() else []
        if IMPORT_LINE in (line.strip() for line in lines):
            return None
        return f'AGENTS.md does not import the harness\'s rules — add "{IMPORT_LINE}" as its first line'
    atomic.write_text(agents, _STARTER_AGENTS)
    for name in ("CLAUDE.md", "GEMINI.md"):
        if not os.path.lexists(root / name):
            os.symlink("AGENTS.md", root / name)
    return None


def _ignore(root: Path, names: List[str]) -> None:
    """List each installed file in the root ``.gitignore`` by its own path (append-only, each line once)."""
    gitignore = root / ".gitignore"
    content = gitignore.read_text() if gitignore.exists() else ""
    present = {line.strip() for line in content.splitlines()}
    missing = [f"/{name}" for name in names if f"/{name}" not in present]
    if not missing:
        return
    header = (
        "# Installed by `aisland setup` (aisland/setup/harness.py): the harness sets' commands and rules, per machine."
    )
    block = ([header] if header not in present else []) + missing
    sep = "" if (not content or content.endswith("\n")) else "\n"
    atomic.write_text(gitignore, content + sep + "\n".join(block) + "\n")


def install_harness(root: Path, link: Optional[bool] = None) -> List[str]:
    """Install the workspace's harness sets into ``root``: every set is read, and checked against the others, before
    anything is written.

    ``link`` chooses links to the sets' files (default: when aisland runs from a checkout) or copies. Returns the lines
    to show the user — a file kept because someone edited it, an ``AGENTS.md`` without the import —, empty when all
    is in place.
    """
    if link is None:
        link = locations.FROM_CHECKOUT
    found = sets(root)
    record = _read_record(root)
    report: List[str] = []
    installed: List[str] = []
    for harness_set in found:
        for source, name in harness_set.files:
            installed.append(name)
            line = _install_one(root, source, name, harness_set.name, link, record)
            if line:
                report.append(line)
    report += _remove_dropped(root, installed, record)
    _write_record(root, record)
    _write_index(root, found)
    if not found:
        return report
    own = {harness_set.folder.resolve() for harness_set in found}
    for line in (_link_claude_commands(root, root, False), _workspace_agents_md(root) if _has_rules(found) else None):
        if line:
            report.append(line)
    for project in Workspace(root).top_level_folders:
        line = _link_claude_commands(root, project, project.resolve() in own)
        if line:
            report.append(line)
        elif (project / ".claude" / "commands").is_symlink():
            git_exclude.exclude(project / ".claude", ["commands"])
    _ignore(root, installed + [RULES_INDEX, RECORD])
    logger.debug("installed the harness sets %s (%d files)", [s.name for s in found], len(installed))
    return report


def _has_rules(found: List[HarnessSet]) -> bool:
    return any(harness_set.rules for harness_set in found)


def summary(found: List[HarnessSet]) -> str:
    """One line naming each set and what it brings: ``aisland (6 commands, 1 script, rules), models (rules)``."""
    parts = []
    for harness_set in found:
        counts = [(harness_set.count(WORKFLOWS), "command"), (harness_set.count(SCRIPTS), "script")]
        others = sum(1 for _s, place in harness_set.files if not place.startswith(AGENTS + "/"))
        counts.append((others, "other file"))
        bits = [f"{n} {word}{'s' if n != 1 else ''}" for n, word in counts if n]
        bits += ["rules"] if harness_set.rules else []
        parts.append(f"{harness_set.name} ({', '.join(bits) or 'nothing'})")
    return ", ".join(parts) or "none"


class SetRow(TypedDict):
    """One set as ``aisland workspace harness`` (and its ``--json``) shows it."""

    set: str
    kind: SetKind
    folder: str
    rules: Optional[str]
    files: List[str]


def rows(root: Path) -> List[SetRow]:
    """Each set as a row for ``aisland workspace harness``: its name, kind, folder, rules and each file's place."""
    return [
        SetRow(
            set=harness_set.name,
            kind=harness_set.kind,
            folder=str(harness_set.folder),
            rules=harness_set.rules,
            files=[place for _source, place in harness_set.files if place != harness_set.rules],
        )
        for harness_set in sets(root)
    ]


def export(target: Path) -> List[Path]:
    """Copy aisland's own set into ``target`` (a new or empty folder) to start a set of one's own; the files copied."""
    if target.exists() and any(target.iterdir()):
        raise HarnessError(f"{target} is not empty: export aisland's harness set into a new folder")
    copied = []
    for source in sorted(HARNESS_DIR.rglob("*")):
        if source.is_file() and "__pycache__" not in source.parts:
            destination = target / source.relative_to(HARNESS_DIR)
            destination.parent.mkdir(parents=True, exist_ok=True)
            copied.append(Path(shutil.copy2(source, destination)))
    return copied
