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

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from aisland import __version__, gitmodules, locations
from aisland.discovery import Where, key_line, table_line
from aisland.errors import AislandError

#: The file, at the workspace root.
FILE_NAME = "workspace.toml"

#: The sections the file has: the workspace's own choices, its CI template set, its harness sets, and its projects.
_SECTIONS = ("workspace", "ci", "harness", "project")
#: The section naming the CI template set and its variables (:mod:`aisland.ci_templates`).
CI_SECTION = "ci"
#: The section listing the agent harness sets the workspace combines (:mod:`aisland.harness`).
HARNESS_SECTION = "harness"

#: The ``[workspace]`` key naming the workspace (else its origin's repository, else its folder:
#: :func:`aisland.projects.get_workspace_name`); no selection may take it.
NAME_KEY = "name"

#: The ``[workspace]`` key saying which shape of the file this is, and the one this aisland reads and writes. A file
#: without it is read as format 1; a newer format is refused, naming the upgrade.
FORMAT_KEY = "format"
FORMAT = 1

#: A project's own keys, in the order they are written; any other key of its table follows, as written.
PROJECT_KEYS = ("path", "url", "branch")

#: A folder of the workspace: one plain, visible folder name.
_FOLDER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


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
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= FORMAT:
        return
    header = table_line(text, "workspace")
    where = Where(path, key_line(text, header, FORMAT_KEY) if header else None)
    if isinstance(value, int) and not isinstance(value, bool) and value > FORMAT:
        raise FileError(
            f"{where}: {FILE_NAME} is format {value}, written by a newer aisland; this one reads format {FORMAT}. "
            "Upgrade it: pip install --upgrade aisland"
        )
    raise FileError(f"{where}: {FORMAT_KEY} must be a whole number, as {FORMAT_KEY} = {FORMAT}")


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
    """The workspace's file, or an empty one when it has none."""
    path = root / FILE_NAME
    return read(path) if path.is_file() else WorkspaceFile()


# --- writing -------------------------------------------------------------------------------------------------------


def _header(name: str) -> str:
    """The how-to the file starts with: it is read on a machine that has no aisland yet."""
    return f"""\
# {FILE_NAME} — the {name} workspace: its projects and its choices, committed.
# Kept by `aisland setup` (aisland/aisland/workspace_file.py): it adds a new project, follows a changed url or folder
# and drops a project that is gone; the rest is yours to edit.
# =========================================================================
# A workspace aisland creates on a new machine. From the folder that will hold it:
#
#   1. A virtual environment with pip and aisland in it. aisland finds it at .venv in this folder;
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
#
#   2. The workspace: each project below cloned on its branch, the starting files, the git aliases:
#        .venv/bin/aisland workspace init . --from {FILE_NAME}
#
#   3. The projects installed into .venv:
#        .venv/bin/aisland setup
# =========================================================================
"""


def _value(value: object) -> str:
    """One TOML value: text, a number, true/false, a list, or an inline table of them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def _key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def render(workspace: WorkspaceFile, name: str) -> str:
    """The whole file: the how-to, ``[workspace]`` (its ``format`` first), then a ``[project."<name>"]`` per project."""
    lines = [_header(name), "[workspace]", f"{FORMAT_KEY} = {FORMAT}"]
    lines += [f"{_key(key)} = {_value(value)}" for key, value in workspace.settings.items()]
    lines.append("")
    if workspace.ci:
        lines.append(f"[{CI_SECTION}]")
        lines += [
            f"{_key(key)} = {_value(value)}" for key, value in workspace.ci.items() if not isinstance(value, dict)
        ]
        lines.append("")
        for key, table in workspace.ci.items():
            if isinstance(table, dict):
                lines.append(f"[{CI_SECTION}.{_key(key)}]")
                lines += [f"{_key(name)} = {_value(item)}" for name, item in table.items()]
                lines.append("")
    if workspace.harness:
        lines.append(f"[{HARNESS_SECTION}]")
        lines += [f"{_key(key)} = {_value(value)}" for key, value in workspace.harness.items()]
        lines.append("")
    for project in workspace.projects.values():
        lines.append(f"[project.{json.dumps(project.name)}]")
        if project.path != project.name:
            lines.append(f"path = {_value(project.path)}")
        if project.url is not None:
            lines.append(f"url = {_value(project.url)}")
        if project.branch is not None:
            lines.append(f"branch = {_value(project.branch)}")
        lines += [f"{_key(key)} = {_value(value)}" for key, value in project.others.items()]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write(root: Path, workspace: WorkspaceFile) -> Optional[Path]:
    """Write ``workspace`` to ``<root>/workspace.toml``; with nothing in it, remove the file and return None."""
    path = root / FILE_NAME
    if not workspace.settings and not workspace.projects and not workspace.ci and not workspace.harness:
        path.unlink(missing_ok=True)
        return None
    path.write_text(render(workspace, workspace.settings.get(NAME_KEY) or root.name))
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


def _git_out(folder: Path, *args: str) -> Optional[str]:
    """``git -C folder <args>``'s output, or None when it fails."""
    result = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def repository_name(url: str) -> Optional[str]:
    """The repository's own name, read from its address (``…/models.git`` -> ``models``), or None when the address
    ends in no folder name."""
    last = re.split(r"[/:]", url.rstrip("/"))[-1]
    name = last[: -len(".git")] if last.endswith(".git") else last
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
        root_branch = _git_out(root, "branch", "--show-current") or None
        for path, url, branch in submodules(root):
            if _FOLDER_NAME.fullmatch(path) and url:
                found.append((path, url, root_branch if branch == "." else branch))
    else:
        for child in sorted(root.iterdir(), key=lambda c: c.name):
            if not child.is_dir() or child.name.startswith(".") or locations.is_alias(child, root):
                continue
            if (child / ".git").exists():
                origin = _git_out(child, "remote", "get-url", "origin")
                if origin:
                    found.append((child.name, origin, _git_out(child, "branch", "--show-current") or None))
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
    lacks stays when it has a url (setup's activation step clones it) and is dropped otherwise. A branch already written is kept (``aisland workspace configure source`` changes it), and every other
    setting of a project stays as written. A project of the file is matched by its folder first, so a name written by
    hand survives. Nothing changed: the file is not touched. Raises :class:`FileError` for a file aisland cannot use."""
    current = load(root)
    by_path = {project.path: project for project in current.projects.values()}
    found = workspace_projects(root)
    kept_paths = {project.path for project in found}
    gone = [
        project
        for project in current.projects.values()
        if project.path not in kept_paths and project.name not in {f.name for f in found}
    ]
    # An optional project (an `active` key) the workspace does not have is not gone: it is not switched on here. Nor is
    # one with a url to clone from: the file is a way to add a project, and setup's activation step clones it (user
    # choice 2026-10-28). Only a project the workspace lacks and nothing could bring back is dropped.
    optional = [project for project in gone if "active" in project.others]
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
        if old.url != item.url:
            lines.append(f"{FILE_NAME}: {old.name} url {item.url}")
        if old.path != item.path:
            lines.append(f"{FILE_NAME}: {old.name} path {item.path}")
        branch = old.branch
        if branch is None and item.branch is not None:
            branch = item.branch
            lines.append(f"{FILE_NAME}: {old.name} branch {branch}")
        projects[old.name] = Project(old.name, item.path, item.url, branch, dict(old.others))
    for project in optional + to_add:
        projects[project.name] = project
    updated = replace(current, settings=dict(current.settings), projects=projects)
    if updated != current or (updated.projects and not (root / FILE_NAME).is_file()):
        write(root, updated)
    return lines
