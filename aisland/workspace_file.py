"""``workspace.toml``: the workspace's one committed file, shaped like ``.gitmodules``.

Each project is a ``[project."<name>"]`` table — ``path`` (the folder; its name when left out), ``url``, ``branch`` —
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import click

from aisland import __version__, locations
from aisland.discovery import Where, table_line

#: The file, at the workspace root.
FILE_NAME = "workspace.toml"

#: The sections the file has.
_SECTIONS = ("workspace", "project")

#: A project's own keys, in the order they are written; any other key of its table follows, as written.
_PROJECT_KEYS = ("path", "url", "branch")

#: A folder of the workspace: one plain, visible folder name.
_FOLDER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class FileError(click.ClickException, ValueError):
    """A ``workspace.toml`` aisland cannot use; the message names the file and the line."""


@dataclass(frozen=True)
class Project:
    """One ``[project."<name>"]``: its folder, address, branch, and the settings of the other steps (``others``)."""

    name: str
    path: str
    url: str
    branch: Optional[str] = None
    others: Dict[str, Any] = field(default_factory=dict)
    where: Optional[Where] = field(default=None, compare=False)


@dataclass
class WorkspaceFile:
    """The file's two sections: ``[workspace]`` (``settings``) and the projects, in the file's order."""

    settings: Dict[str, Any] = field(default_factory=dict)
    projects: Dict[str, Project] = field(default_factory=dict)


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
        raise FileError(f'{where}: [{key}] is no section of {FILE_NAME} — it has [workspace] and [project."<name>"]')
    projects: Dict[str, Project] = {}
    for name, table in data.get("project", {}).items():
        line = table_line(text, "project", name)
        where = Where(path, line if line else _top_line(text, "project"))
        if not isinstance(table, dict):
            raise FileError(f'{where}: project {name!r} is not a table: write it as [project."{name}"]')
        for key in _PROJECT_KEYS:
            if key in table and not isinstance(table[key], str):
                raise FileError(f'{where}: project {name!r}: {key} must be text, as {key} = "<text>"')
        if "url" not in table:
            raise FileError(f"{where}: project {name!r} has no url")
        folder = table.get("path", name)
        if not _FOLDER_NAME.fullmatch(folder):
            raise FileError(f"{where}: project {name!r}: path {folder!r} is not a folder of the workspace")
        others = {key: value for key, value in table.items() if key not in _PROJECT_KEYS}
        projects[name] = Project(name, folder, table["url"], table.get("branch"), others, where)
    _refuse_a_shared_folder(projects)
    return WorkspaceFile(dict(data.get("workspace", {})), projects)


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
#   1. A virtual environment with aisland in it. aisland finds it at .venv in this folder;
#      --prompt names it, so the shell shows ({name}) once it is activated. With uv:
#        uv venv --seed --prompt {name} .venv
#        uv pip install --python .venv/bin/python aisland
#      or with pip:
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


def _value(value: Any) -> str:
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
    """The whole file: the how-to, ``[workspace]``, then a ``[project."<name>"]`` per project."""
    lines = [_header(name)]
    if workspace.settings:
        lines.append("[workspace]")
        lines += [f"{_key(key)} = {_value(value)}" for key, value in workspace.settings.items()]
        lines.append("")
    for project in workspace.projects.values():
        lines.append(f"[project.{json.dumps(project.name)}]")
        lines.append(f"path = {_value(project.path)}")
        lines.append(f"url = {_value(project.url)}")
        if project.branch is not None:
            lines.append(f"branch = {_value(project.branch)}")
        lines += [f"{_key(key)} = {_value(value)}" for key, value in project.others.items()]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write(root: Path, workspace: WorkspaceFile) -> Optional[Path]:
    """Write ``workspace`` to ``<root>/workspace.toml``; with nothing in it, remove the file and return None."""
    path = root / FILE_NAME
    if not workspace.settings and not workspace.projects:
        path.unlink(missing_ok=True)
        return None
    path.write_text(render(workspace, root.name))
    return path


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
    if not (root / ".gitmodules").is_file():
        return []
    listing = subprocess.run(
        ["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url|branch)$"],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout
    fields: Dict[str, Dict[str, str]] = {"path": {}, "url": {}, "branch": {}}
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        name, setting = key[len("submodule.") :].rsplit(".", 1)
        fields[setting][name] = value
    return [(path, fields["url"].get(name, ""), fields["branch"].get(name)) for name, path in fields["path"].items()]


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

    A new project is added with its path, url and branch; a changed url or path is followed; a project that is gone
    is dropped. A branch already written is kept (``aisland workspace configure source`` changes it), and every other
    setting of a project stays as written. A project of the file is matched by its folder first, so a name written by
    hand survives. Nothing changed: the file is not touched. Raises :class:`FileError` for a file aisland cannot use."""
    current = load(root)
    by_path = {project.path: project for project in current.projects.values()}
    found = workspace_projects(root)
    kept_paths = {project.path for project in found}
    lines = [
        f"{FILE_NAME}: dropped {project.name} (no longer in the workspace)"
        for project in current.projects.values()
        if project.path not in kept_paths and project.name not in {f.name for f in found}
    ]
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
    updated = WorkspaceFile(dict(current.settings), projects)
    if updated != current or (updated.projects and not (root / FILE_NAME).is_file()):
        write(root, updated)
    return lines
