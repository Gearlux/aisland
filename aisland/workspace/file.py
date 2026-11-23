"""``workspace.toml``: the workspace's one committed file, shaped like ``.gitmodules``.

Each project is a ``[project."<name>"]`` table — ``path`` (the folder, written only when it is not the name), ``url``, ``branch`` —
as ``.gitmodules`` has ``[submodule "<name>"]``; ``[workspace]`` holds the workspace's own choices. Settings of the
later steps (``active``, ``source``, …) sit in the same tables and are kept as written::

    [workspace]
    framework = ["torch"]

    [project."models"]
    path = "m"
    url = "git@example.com:acme/models.git"
    branch = "dev"

A machine without aisland reads it to start (its first lines say how), ``aisland workspace init --from`` creates the
workspace from it, and ``aisland setup`` keeps its projects current (:func:`refresh`) from ``.gitmodules`` or, without
one, from the clones. Every refusal is a :class:`FileError` naming the file and the line.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import tomlkit
from tomlkit import items as tomlkit_items
from tomlkit.container import OutOfOrderTableProxy

from aisland import __version__, atomic, gitcmd, locations
from aisland.errors import AislandError
from aisland.toml_places import FORMAT_KEY, Where, format_refusal, key_line, table_line
from aisland.workspace import gitmodules, root_project

#: The file, at the workspace root.
FILE_NAME = "workspace.toml"

#: The sections the file has: the workspace's own choices, its CI template set, its harness sets, and its projects.
_SECTIONS = ("workspace", "ci", "harness", "project")
#: The section naming the CI template set and its variables (:mod:`aisland.ci.ci_templates`).
CI_SECTION = "ci"
#: The key of ``[ci]`` and of a project's ``ci`` table naming the set; every other key of a project's ``ci`` is a
#: variable's value. ``[ci] variables`` gives every project's.
CI_TEMPLATES_KEY = "templates"
CI_VARIABLES_KEY = "variables"
#: The section listing the agent harness sets the workspace combines (:mod:`aisland.setup.harness`), and its key.
HARNESS_SECTION = "harness"
HARNESS_SETS_KEY = "sets"

#: The ``[workspace]`` key naming the workspace (else its origin's repository, else its folder:
#: :func:`aisland.workspace.projects.get_workspace_name`); no selection may take it.
NAME_KEY = "name"

#: The ``[workspace]`` key saying which shape of the file this is, and the one this aisland reads and writes. A file
#: without it is read as format 1; a newer format is refused, naming the upgrade.
FORMAT = 1

#: A project's own keys, in the order they are written; any other key of its table follows, as written.
PROJECT_KEYS = ("path", "url", "branch")

#: A folder of the workspace: one plain, visible folder name.
_FOLDER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


#: A table of the file as tomlkit holds it: one written in a piece, or one whose parts are apart in the file.
_Table = Union[tomlkit_items.Table, OutOfOrderTableProxy]


class FileError(AislandError, ValueError):
    """A ``workspace.toml`` aisland cannot use; the message names the file and the line."""


@dataclass(frozen=True)
class Project:
    """One ``[project."<name>"]``: its folder, address, branch, and the settings of the other steps (``others``)."""

    name: str
    path: str
    url: Optional[str] = None
    branch: Optional[str] = None
    others: Dict[str, Any] = field(default_factory=dict)  # Any: TOML values, each plugin's to check
    where: Optional[Where] = field(default=None, compare=False)


@dataclass
class WorkspaceFile:
    """The file's sections: ``[workspace]`` (``settings``), ``[ci]`` (the CI template set, ``ci``), ``[harness]``
    (the agent harness sets, ``harness``) and the projects, in the file's order."""

    settings: Dict[str, Any] = field(default_factory=dict)  # Any: TOML values, each reader's to check
    projects: Dict[str, Project] = field(default_factory=dict)
    ci: Dict[str, Any] = field(default_factory=dict)  # Any: TOML values, ci_templates' to check
    harness: Dict[str, Any] = field(default_factory=dict)  # Any: TOML values, harness' to check


# --- reading -------------------------------------------------------------------------------------------------------


def _top_line(text: str, key: str) -> Optional[int]:
    """The line where the top-level ``key`` starts: its table header (``[key]``, ``[key.…]``) or ``key = …``."""
    part = re.escape(key)
    pattern = re.compile(rf'^\s*(?:\[\s*(?:{part}|"{part}")\s*[.\]]|(?:{part}|"{part}")\s*=)')
    for number, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return number
    return None


def read(path: Path) -> WorkspaceFile:
    """The workspace file at ``path``, checked. Raises :class:`FileError` naming the file and the line."""
    if not path.is_file():
        raise FileError(f"{path} does not exist: name the {FILE_NAME} to create the workspace from.")
    text = path.read_text()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise FileError(f"{path}: {exc}") from exc
    for key in data:
        if key in _SECTIONS:
            continue
        where = Where(path, _top_line(text, key))
        if key == "projects":
            raise FileError(
                f'{where}: [projects] is the old workspace file — write each project as [project."<name>"] '
                "with path, url and branch"
            )
        raise FileError(
            f'{where}: [{key}] is no section of {FILE_NAME} — it has [workspace], [ci], [harness] and [project."<name>"]'
        )
    # Another person's file once the workspace is cloned: a section that is no table is a refusal, never a crash.
    if not isinstance(data.get("project", {}), dict):
        raise FileError(
            f'{Where(path, _top_line(text, "project"))}: project must hold one table per project: '
            'write each as [project."<name>"]'
        )
    if not isinstance(data.get("workspace", {}), dict):
        raise FileError(f"{Where(path, _top_line(text, 'workspace'))}: [workspace] must be a table")
    projects: Dict[str, Project] = {}
    for name, table in data.get("project", {}).items():
        line = table_line(text, "project", name)
        where = Where(path, line if line else _top_line(text, "project"))
        if not isinstance(table, dict):
            raise FileError(f'{where}: project {name!r} is not a table: write it as [project."{name}"]')
        for key in PROJECT_KEYS:
            if key in table and not isinstance(table[key], str):
                raise FileError(f'{where}: project {name!r}: {key} must be text, as {key} = "<text>"')
        folder = table.get("path", name)
        if not _FOLDER_NAME.fullmatch(folder):
            raise FileError(f"{where}: project {name!r}: path {folder!r} is not a folder of the workspace")
        others = {key: value for key, value in table.items() if key not in PROJECT_KEYS}
        projects[name] = Project(name, folder, table.get("url"), table.get("branch"), others, where)
    _refuse_a_shared_folder(projects)
    settings = dict(data.get("workspace", {}))
    _check_format(path, text, settings.pop(FORMAT_KEY, FORMAT))
    if NAME_KEY in settings and not (
        isinstance(settings[NAME_KEY], str) and _FOLDER_NAME.fullmatch(settings[NAME_KEY])
    ):
        header = table_line(text, "workspace")
        where = Where(path, key_line(text, header, NAME_KEY) if header else None)
        raise FileError(
            f'{where}: the workspace\'s name must be one plain name (letters, digits, _ . -), as {NAME_KEY} = "studio"'
        )
    ci = data.get(CI_SECTION, {})
    if not isinstance(ci, dict):
        raise FileError(f"{Where(path, _top_line(text, CI_SECTION))}: [{CI_SECTION}] must be a table")
    harness = data.get(HARNESS_SECTION, {})
    if not isinstance(harness, dict):
        raise FileError(f"{Where(path, _top_line(text, HARNESS_SECTION))}: [{HARNESS_SECTION}] must be a table")
    return WorkspaceFile(settings, projects, dict(ci), dict(harness))


def _check_format(path: Path, text: str, value: object) -> None:
    """Refuse a format this aisland cannot read: a newer one (written by a newer aisland) or a value that is none."""
    header = table_line(text, "workspace")
    refusal = format_refusal(
        Where(path, key_line(text, header, FORMAT_KEY) if header else None), FILE_NAME, value, FORMAT
    )
    if refusal:
        raise FileError(refusal)


def name_of(root: Path) -> Optional[str]:
    """The workspace's name its ``workspace.toml`` gives (``[workspace] name``), or None: no file, no name, or a file
    aisland cannot read — setup's upkeep reports that one, and whoever only wants a name goes on without it."""
    path = root / FILE_NAME
    if not path.is_file():
        return None
    try:
        name = read(path).settings.get(NAME_KEY)
    except FileError:
        return None
    return name if isinstance(name, str) else None


def _refuse_a_shared_folder(projects: Dict[str, Project]) -> None:
    seen: Dict[str, Project] = {}
    for project in projects.values():
        first = seen.get(project.path)
        if first is not None:
            raise FileError(
                f"{first.where} and {project.where}: projects {first.name!r} and {project.name!r} are both checked "
                f"out in the folder {project.path!r}"
            )
        seen[project.path] = project


def load(root: Path) -> WorkspaceFile:
    """The workspace's file, or an empty one when it has none: its own, else — before setup links it — its root
    project's (:func:`aisland.locations.workspace_listing`)."""
    path = locations.workspace_listing(root)
    return read(path) if path.is_file() else WorkspaceFile()


# --- writing -------------------------------------------------------------------------------------------------------


def _header(name: str, root: Optional[Project] = None) -> str:
    """The how-to the file starts with: it is read on a machine that has no aisland yet. A file naming a root project
    (``root``, its row) says to clone that project and run setup; any other says to run ``aisland workspace init``."""
    if root is None or root.url is None:
        return f"""\
# {FILE_NAME} — the {name} workspace: its projects and its choices, committed.
# Kept by `aisland setup` (aisland/aisland/workspace/file.py): it adds a new project, follows a changed url or folder
# and drops a project that is gone, changing nothing else; the rest is yours to edit.
# =========================================================================
# A workspace aisland creates on a new machine. From the folder that will hold it:
#
#   1. {_venv_step(name)}
#   2. The workspace: each project below cloned on its branch, the starting files, the git aliases:
#        .venv/bin/aisland workspace init . --from {FILE_NAME}
#
#   3. The projects installed into .venv:
#        .venv/bin/aisland setup
# =========================================================================
"""
    return f"""\
# {FILE_NAME} — the {name} workspace: its projects and its choices, committed.
# It lives in the root project {root.name}. `aisland setup` adds a row for a new clone and fills in
# a missing branch; it never changes a url or drops a row. The rest is yours to edit.
# =========================================================================
# A workspace aisland creates on a new machine:
#
#   1. The root project, cloned into the folder that will hold the workspace:
#        mkdir {name} && cd {name}
#        {clone_command(root)}
#
#   2. {_venv_step(name)}
#   3. Every other project cloned beside it, and all of them installed into .venv:
#        .venv/bin/aisland setup
# =========================================================================
"""


def clone_command(project: Project) -> str:
    """The ``git clone`` that makes ``project``'s folder on its branch, as a person types it."""
    branch = f"-b {project.branch} " if project.branch else ""
    return f"git clone {branch}{project.url} {project.path}"


def _venv_step(name: str) -> str:
    """The how-to's step making the venv, from the text after its number to the blank line closing it."""
    return f"""\
A virtual environment with pip and aisland in it. aisland finds it at .venv in this folder;
#      --prompt names it, so the shell shows ({name}) once it is activated. With uv:
#        uv venv --seed --prompt {name} .venv
#        .venv/bin/pip install aisland
#      --seed puts pip in it: `aisland setup` installs uv into the venv with that pip and stops
#      without it. A venv made without --seed gets it with:
#        uv pip install --python .venv/bin/python pip
#      or without uv:
#        python3 -m venv --prompt {name} .venv
#        .venv/bin/pip install aisland
#
#      A venv kept elsewhere under a name of its own, linked here as .venv:
#        python3 -m venv ~/venvs/{name}
#        ln -s ~/venvs/{name} .venv
#        .venv/bin/pip install aisland
#
#      From a wheel instead of PyPI, name the file in place of aisland:
#        .venv/bin/pip install ./aisland-{__version__}-py3-none-any.whl
#      (built in a checkout of aisland with: uv build --wheel aisland -o dist)
#"""


#: The ruled lines that open and close the how-to (:func:`_header`).
_RULE = "# ========"


def _with_header(text: str, header: str) -> str:
    """``text`` under the current how-to, ``header``: the one a file starts with (its first line names the file, its
    second ruled line closes it) is replaced, so a new name, a root project or aisland's version reaches it; a file
    starting with no comment gets one; a file opening with comments of its own keeps them as they are."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith(f"# {FILE_NAME} — "):
        rules = [number for number, line in enumerate(lines) if line.startswith(_RULE)]
        if len(rules) >= 2 and all(line.startswith("#") for line in lines[: rules[1] + 1]):
            return header + "".join(lines[rules[1] + 1 :])
        return text
    if text.lstrip().startswith("#"):
        return text
    return header + "\n" + text.lstrip("\n")


def root_row(workspace: WorkspaceFile) -> Optional[Project]:
    """The row of the root project the file names (``[workspace] root``), or None: no such key, or no such row."""
    name = workspace.settings.get(locations.ROOT_KEY)
    return workspace.projects.get(name) if isinstance(name, str) else None


def _item(value: object) -> tomlkit_items.Item:
    """A value as tomlkit writes it inside a table: a table of it inline, so a project stays one table as in
    ``.gitmodules`` (``ci = { templates = "x" }``, a harness set ``{ git = "…" }``)."""
    if isinstance(value, dict):
        table = tomlkit.inline_table()
        for key, part in value.items():
            table[key] = _item(part)
        return table
    if isinstance(value, list):
        array = tomlkit.array()
        for part in value:
            array.append(_item(part))
        return array
    return tomlkit.item(value)


def _sub_table(value: object) -> tomlkit_items.Item:
    """A ``[ci]`` value: a table of it is a ``[ci.<key>]`` table of its own (``[ci.variables]``)."""
    if isinstance(value, dict):
        table = tomlkit.table()
        for key, part in value.items():
            table[key] = _item(part)
        return table
    return _item(value)


#: What tomlkit hands over for a table of the file: one written in a piece, one apart in the file, or one inline.
_TABLES = (tomlkit_items.Table, OutOfOrderTableProxy, tomlkit_items.InlineTable)


def _sync(
    table: Union[_Table, tomlkit_items.InlineTable],
    wanted: Dict[str, Any],  # Any: TOML values
    convert: Callable[[object], tomlkit_items.Item],
) -> None:
    """Make ``table`` hold ``wanted``: a key it no longer has goes, a changed value is replaced where it stands, a new
    key is added at the end, and a value equal to the one written is left as written (its comment, its spelling). A
    table inside it that changed is edited the same way, key by key, never replaced (``[ci.variables]`` keeps its
    comments)."""
    for key in [key for key in table if key not in wanted]:
        del table[key]
    for key, value in wanted.items():
        if key in table and _plain(table[key]) == value:
            continue
        held = table[key] if key in table else None
        if isinstance(held, _TABLES) and isinstance(value, dict):
            _sync(held, value, _item)
            continue
        table[key] = convert(value)


def _plain(value: object) -> object:
    """A value tomlkit read, as tomllib reads it (tomlkit hands a boolean over as it is, everything else wrapped)."""
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def _section(document: tomlkit.TOMLDocument, name: str, first: Optional[Dict[str, object]] = None) -> _Table:
    """The document's ``[name]`` table, added in its place among :data:`_SECTIONS` when the file has none (holding
    ``first``: a table is written whole before the sections after it go back). One the file writes inline
    (``ci = { templates = "x" }``) becomes a table of its own in its place, every value kept."""
    current = document[name] if name in document else None
    if current is not None and not isinstance(current, (tomlkit_items.Table, OutOfOrderTableProxy)):
        held = _plain(current)
        if not isinstance(held, dict):
            raise FileError(f"{FILE_NAME}: {name} is no table: write it as [{name}]")
        first = {**(first or {}), **held}
        current = None
    if current is None:
        later = [section for section in _SECTIONS[_SECTIONS.index(name) + 1 :] if section in document]
        moved = [(section, document[section]) for section in later]
        for section in [name, *later]:
            if section in document:
                del document[section]
        added = tomlkit.table(is_super_table=name == "project")
        for key, value in (first or {}).items():
            added.add(key, _sub_table(value) if name == CI_SECTION else _item(value))
        document[name] = added
        for section, item in moved:
            document[section] = item
    table = document[name]
    if not isinstance(table, (tomlkit_items.Table, OutOfOrderTableProxy)):  # pragma: no cover - made a table above
        raise FileError(f"{FILE_NAME}: [{name}] could not be written as a table")
    return table


def _project_table(tables: _Table, name: str) -> _Table:
    """The ``[project."<name>"]`` table, added (quoted, as ``.gitmodules`` writes ``[submodule "<name>"]``) when the
    file has none; one the file writes inline (``models = { url = "u" }`` under ``[project]``) becomes a table of its
    own, every value kept."""
    held = tables[name] if name in tables else None
    values: Dict[str, object] = {}
    if held is not None and not isinstance(held, (tomlkit_items.Table, OutOfOrderTableProxy)):
        plain = _plain(held)
        if not isinstance(plain, dict):
            raise FileError(f'{FILE_NAME}: project {name!r} is not a table: write it as [project."{name}"]')
        values = plain
        del tables[name]
        held = None
    if held is None:
        added = tomlkit.table()
        for key, value in values.items():
            added.add(key, _item(value))
        if isinstance(tables, tomlkit_items.Table):
            tables.append(tomlkit_items.SingleKey(name, t=tomlkit_items.KeyType.Basic), added)
        else:
            tables[name] = added
    table = tables[name]
    if not isinstance(table, (tomlkit_items.Table, OutOfOrderTableProxy)):  # pragma: no cover - made a table above
        raise FileError(f"{FILE_NAME}: project {name!r} could not be written as a table")
    return table


def _drop(document: tomlkit.TOMLDocument, name: str) -> None:
    if name in document:
        del document[name]


def _project_keys(project: Project) -> Dict[str, Any]:  # Any: TOML values
    """The keys a project's table holds: ``path`` only when it is not the name, ``url``, ``branch``, then the rest."""
    keys: Dict[str, Any] = {}  # Any: TOML values
    if project.path != project.name:
        keys["path"] = project.path
    if project.url is not None:
        keys["url"] = project.url
    if project.branch is not None:
        keys["branch"] = project.branch
    keys.update(project.others)
    return keys


def render(workspace: WorkspaceFile, name: str, text: str = "") -> str:
    """The file: the how-to, ``[workspace]`` (its ``format`` first), ``[ci]``, ``[harness]``, then a
    ``[project."<name>"]`` per project — written into ``text``, the file as it is, so every comment, the order of the
    keys and the type of each value a person wrote stays where it was (only a value that changed is rewritten)."""
    document = tomlkit.parse(_with_header(text, _header(name, root_row(workspace))))
    settings = _section(document, "workspace", {FORMAT_KEY: FORMAT})
    if FORMAT_KEY not in settings:
        # `format` goes first: the table is written again with it ahead of what the file holds — its comments too,
        # when the file writes it in one piece; one apart in the file is gathered into one, its values kept.
        if isinstance(settings, tomlkit_items.Table):
            first = tomlkit.table()
            first.add(FORMAT_KEY, FORMAT)
            for key, item in settings.value.body:
                first.value.append(key, item)
            document["workspace"] = first
        else:
            held = _plain(settings)
            del document["workspace"]
            _section(document, "workspace", {FORMAT_KEY: FORMAT, **(held if isinstance(held, dict) else {})})
        settings = _section(document, "workspace")
    _sync(settings, {FORMAT_KEY: FORMAT, **workspace.settings}, _item)
    for name_of_section, values, convert in (
        (CI_SECTION, workspace.ci, _sub_table),
        (HARNESS_SECTION, workspace.harness, _item),
    ):
        if values:
            _sync(_section(document, name_of_section), values, convert)
        else:
            _drop(document, name_of_section)
    if not workspace.projects:
        _drop(document, "project")
    else:
        tables = _section(document, "project")
        for gone in [key for key in tables if key not in workspace.projects]:
            del tables[gone]
        for project in workspace.projects.values():
            table = _project_table(tables, project.name)
            keys = _project_keys(project)
            if "path" in table and "path" not in keys:
                keys = {"path": project.path, **keys}  # a `path` equal to the name the user wrote stays
            _sync(table, keys, _item)
    return tomlkit.dumps(document).rstrip("\n") + "\n"


def write(root: Path, workspace: WorkspaceFile) -> Optional[Path]:
    """Write ``workspace`` to ``<root>/workspace.toml``, editing the file that is there; with nothing in it, remove the
    file and return None. In a workspace with a root project the file is the root project's, reached through the
    folder's link (or, before setup has made it, at its place in the root project)."""
    path = locations.workspace_listing(root)
    if not workspace.settings and not workspace.projects and not workspace.ci and not workspace.harness:
        path.unlink(missing_ok=True)
        return None
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic.write_text(path, render(workspace, workspace.settings.get(NAME_KEY) or root.name, text))
    return path


def set_settings(root: Path, keys: Tuple[str, ...], values: Dict[str, Dict[str, Any]]) -> Path:  # Any: TOML values
    """Set one plugin's ``keys`` on the projects of ``<root>/workspace.toml``, leaving every other setting alone.

    ``values[project]`` holds the project's values of ``keys`` — a key it leaves out is removed, and a project not in
    ``values`` loses all of them; a ``branch`` among them sets the project's own branch. A project left with no url
    and nothing else (it was in the file only for these keys) is dropped. Returns the file's path."""
    current = load(root)
    projects: Dict[str, Project] = {}
    for name in [*current.projects, *sorted(set(values) - set(current.projects))]:
        old = current.projects.get(name) or Project(name, name)
        others = {key: value for key, value in old.others.items() if key not in keys}
        given = dict(values.get(name, {}))
        branch = given.pop("branch", old.branch)
        others.update(given)
        if old.url is None and not others:
            continue
        projects[name] = Project(name, old.path, old.url, branch, others)
    write(root, replace(current, settings=dict(current.settings), projects=projects))
    return root / FILE_NAME


# --- the projects a workspace holds ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """A project found in the workspace folder: named after its repository, checked out in ``path``."""

    name: str
    path: str
    url: str
    branch: Optional[str]


def submodules(root: Path) -> List[Tuple[str, str, Optional[str]]]:
    """``(path, url, branch)`` of every submodule ``.gitmodules`` declares, in its order; no ``branch`` line is None."""
    return [(submodule.path, submodule.url or "", submodule.branch) for submodule in gitmodules.read(root)]


def last_part(url: str) -> str:
    """A repository address's last part, a ``.git`` at its end dropped (https, ssh and scp-style:
    ``https://host/acme/models.git``, ``git@host:my.tools.git``, ``/srv/git/models/``), whatever it spells."""
    return re.split(r"[/:]", url.rstrip("/"))[-1].removesuffix(".git")


def repository_name(url: str) -> Optional[str]:
    """The repository's own name, read from its address (``…/models.git`` -> ``models``), or None when the address
    ends in no folder name (:func:`last_part`)."""
    name = last_part(url)
    return name if _FOLDER_NAME.fullmatch(name) else None


def workspace_projects(root: Path) -> List[Found]:
    """The projects the workspace at ``root`` holds, as its ``workspace.toml`` writes them.

    With ``.gitmodules``, its submodules (a branch of ``.`` — follow the superproject's — is written as the root's
    branch: a new workspace has no superproject branch to follow yet). Without one, each direct subfolder that is a
    git clone, by its ``origin`` and the branch it is on; a folder with no ``origin`` is no project of the file.

    A project is named after its repository unless that name is no folder name or another project's folder; its path
    is the link in the workspace folder naming its folder (:func:`aisland.locations.alias_names`), else its folder —
    a workspace made from the file checks it out straight into that path."""
    found: List[Tuple[str, str, Optional[str]]] = []
    if (root / ".gitmodules").is_file():
        root_branch = gitcmd.read(root, "branch", "--show-current") or None
        for path, url, branch in submodules(root):
            if _FOLDER_NAME.fullmatch(path) and url:
                found.append((path, url, root_branch if branch == "." else branch))
    else:
        for child in sorted(root.iterdir(), key=lambda c: c.name):
            if not child.is_dir() or child.name.startswith(".") or locations.is_alias(child, root):
                continue
            if (child / ".git").exists():
                origin = gitcmd.read(child, "remote", "get-url", "origin")
                if origin:
                    found.append((child.name, origin, gitcmd.read(child, "branch", "--show-current") or None))
    folders = {folder for folder, _url, _branch in found}
    short = locations.alias_names(root)
    used: Set[str] = set()
    projects = []
    for folder, url, branch in found:
        repository = repository_name(url)
        name = repository if repository and repository not in used and repository not in folders - {folder} else folder
        used.add(name)
        projects.append(Found(name, short.get((root / folder).resolve()) or folder, url, branch))
    return projects


# --- `aisland setup` keeps it current --------------------------------------------------------------------------------


def refresh(root: Path) -> List[str]:
    """Bring ``<root>/workspace.toml`` in line with the projects the workspace holds; one line per change.

    A new project is added with its path, url and branch; a changed url or path is followed; a project the workspace
    lacks stays when it has a url (setup's activation step clones it) and is dropped otherwise. In a workspace with a
    root project the file is what every machine shares, so it wins: a row's url is never replaced by a clone's
    origin (setup's activation step points the clone at the file's url instead), and a row stays whether this machine
    has the project or not; only a clone the file does not list yet is added. A branch already written is kept (``aisland workspace configure source`` changes it), and every other
    setting of a project stays as written. A project of the file is matched by its folder first, so a name written by
    hand survives. With no row changed, a how-to out of date (:func:`_with_header`) is rewritten alone, and said; nothing
    changed: the file is not touched. Raises :class:`FileError` for a file aisland cannot use."""
    current = load(root)
    shared = root_project.rooted(root)
    by_path = {project.path: project for project in current.projects.values()}
    found = workspace_projects(root)
    kept_paths = {project.path for project in found}
    gone = [
        project
        for project in current.projects.values()
        if project.path not in kept_paths and project.name not in {f.name for f in found}
    ]
    # An optional project (an `active` key) the workspace does not have is not gone: it is not switched on here. Nor is
    # one with a url to clone from: the file is a way to add a project, and setup's activation step clones it. Only a project the workspace lacks and nothing could bring back is dropped.
    optional = [project for project in gone if "active" in project.others or shared]
    to_add = [project for project in gone if project not in optional and project.url is not None]
    lines = [
        f"{FILE_NAME}: dropped {project.name} (no longer in the workspace)"
        for project in gone
        if project not in optional and project not in to_add
    ]
    lines += [f"{FILE_NAME}: {project.name} is not in the workspace yet; setup adds it" for project in to_add]
    projects: Dict[str, Project] = {}
    for item in found:
        old = by_path.get(item.path) or current.projects.get(item.name)
        if old is None:
            projects[item.name] = Project(item.name, item.path, item.url, item.branch)
            lines.append(f"{FILE_NAME}: added {item.name}")
            continue
        url = old.url if shared and old.url is not None else item.url
        if old.url != url:
            lines.append(f"{FILE_NAME}: {old.name} url {item.url}")
        if old.path != item.path:
            lines.append(f"{FILE_NAME}: {old.name} path {item.path}")
        branch = old.branch
        if branch is None and item.branch is not None:
            branch = item.branch
            lines.append(f"{FILE_NAME}: {old.name} branch {branch}")
        projects[old.name] = Project(old.name, item.path, url, branch, dict(old.others))
    for project in optional + to_add:
        projects[project.name] = project
    updated = replace(current, settings=dict(current.settings), projects=projects)
    path = locations.workspace_listing(root)
    if updated != current or (updated.projects and not path.is_file()):
        write(root, updated)
    elif path.is_file():
        text = path.read_text(encoding="utf-8")
        how_to = _with_header(text, _header(current.settings.get(NAME_KEY) or root.name, root_row(current)))
        if how_to != text:
            atomic.write_text(path, how_to)
            lines.append(f"{FILE_NAME}: the how-to rewritten for aisland {__version__}")
    return lines
