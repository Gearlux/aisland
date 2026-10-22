"""What the workspace's projects declare to aisland, found in two stages.

STAGE 1 (:func:`discover`) reads every project's ``pyproject.toml`` and needs nothing installed — a fresh machine
asks "which frameworks?" before the project that defines that question is installed::

    [tool.aisland.selection.framework]       # the table with `question` DEFINES the selection
    question = "Which ML frameworks should this machine install?"
    choices = ["torch", "keras"]             # every project's `choices` are its menu: extras of its own

    [tool.aisland.service.aim]               # a service the project needs, run by aisland's supervisor
    command = ["aim", "up", "--repo", "{data}", "--port", "{port}"]
    port = 43800
    ready = "/"

    [project.entry-points."aisland.plugins"] # code, for what a declaration cannot say
    acme-db = "acme_plugin:Database"

STAGE 2 (:func:`load`) loads those entry points once their project is installed, and checks each against the
methods its kind needs. A project never imports aisland: a plugin is any object with a ``kind`` and those methods.

Everything refused names the file and the line of the table it is about (:class:`DeclarationError`).
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, get_args

import click

from aisland.manager import workspace_folders

#: The entry-point group a project's code plugins are declared in.
ENTRY_POINT_GROUP = "aisland.plugins"

#: The kinds a code plugin can be, and the methods each needs (each taking one argument, the context aisland passes).
PluginKind = Literal["service"]
PLUGIN_KINDS: Tuple[str, ...] = get_args(PluginKind)
_KIND_METHODS: Dict[str, Tuple[str, ...]] = {"service": ("start", "stop", "status")}

#: The settings a declared selection and a declared service take.
_SELECTION_KEYS = ("question", "choices")
_SERVICE_KEYS = ("command", "port", "ready", "needs")

#: Whether an installed plugin could be loaded yet.
PluginState = Literal["installed", "not installed"]


class DeclarationError(click.ClickException, ValueError):
    """A declaration aisland cannot use; the message names the file and the line."""


@dataclass(frozen=True)
class Where:
    """A place in a project's ``pyproject.toml``: the file, and the line of the table (``None`` when not found)."""

    file: Path
    line: Optional[int]

    def __str__(self) -> str:
        return f"{self.file}:{self.line}" if self.line else str(self.file)


@dataclass(frozen=True)
class Declared:
    """Which project declared something, and where."""

    project: str
    where: Where


@dataclass(frozen=True)
class Selection:
    """A selection one project defines (its question) and every project offers choices for."""

    name: str
    question: str
    defined_by: Declared
    choices: Dict[str, List[str]]


@dataclass(frozen=True)
class Service:
    """A service a project declares: the command the supervisor runs, its port and readiness path.

    ``needs`` maps a selection to the choice that must be selected for the service to be offered."""

    name: str
    project: str
    where: Where
    command: Tuple[str, ...]
    port: Optional[int] = None
    ready: Optional[str] = None
    needs: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Plugin:
    """A code plugin a project declares as an entry point; ``distribution`` is the project's package name."""

    name: str
    project: str
    distribution: str
    target: str
    where: Where


@dataclass(frozen=True)
class Loaded:
    """A declared plugin after stage 2: loaded (``obj``, its ``kind``) or not installed yet."""

    plugin: Plugin
    state: PluginState
    kind: Optional[str] = None
    # The plugin object is whatever the project's code defines — aisland only checks its kind's methods.
    obj: Any = None


@dataclass
class Discovery:
    """Everything stage 1 found; ``undefined`` maps a selection no project defines to the projects offering it."""

    selections: Dict[str, Selection] = field(default_factory=dict)
    services: Dict[str, Service] = field(default_factory=dict)
    plugins: Dict[str, Plugin] = field(default_factory=dict)
    undefined: Dict[str, List[str]] = field(default_factory=dict)


# --- locating a table in the text ----------------------------------------------------------------------------------


def _part(name: str) -> str:
    """One dotted-key part as a pattern: bare, or quoted either way."""
    escaped = re.escape(name)
    return rf'(?:{escaped}|"{escaped}"|\'{escaped}\')'


def _header_line(text: str, *parts: str) -> Optional[int]:
    """The 1-based line of the table header ``[a.b.c]`` (spaces and quoting allowed), or None."""
    dotted = r"\s*\.\s*".join(_part(part) for part in parts)
    pattern = re.compile(rf"^\s*\[\s*{dotted}\s*\]")
    for number, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return number
    return None


def _key_line(text: str, header: int, key: str) -> Optional[int]:
    """The line of ``key = …`` below the table header on line ``header``: the table's own, since valid TOML keeps a
    table's keys under its header and cannot define the key again in a later table."""
    pattern = re.compile(rf"^\s*{_part(key)}\s*=")
    lines = text.splitlines()[header:]
    return next((number for number, line in enumerate(lines, start=header + 1) if pattern.match(line)), None)


# --- stage 1 --------------------------------------------------------------------------------------------------------


def _read(path: Path) -> Tuple[str, Dict[str, Any]]:
    text = path.read_text()
    try:
        return text, tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DeclarationError(f"{path}: {exc}") from exc


def _strings(value: object) -> Optional[List[str]]:
    """``value`` as a list of strings, or None when it is not one."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _project_tables(root: Path) -> List[Tuple[str, Path, str, Dict[str, Any]]]:
    """``(project, pyproject path, text, data)`` for every project of the workspace with a ``pyproject.toml``."""
    found = []
    for name, path, _active in workspace_folders(root):
        pyproject = root / path / "pyproject.toml"
        if pyproject.is_file():
            text, data = _read(pyproject)
            found.append((name, pyproject, text, data))
    return found


def _selections(projects: List[Tuple[str, Path, str, Dict[str, Any]]], found: Discovery) -> None:
    defined: Dict[str, Tuple[str, Declared]] = {}
    offered: Dict[str, Dict[str, List[str]]] = {}
    for project, path, text, data in projects:
        tables = data.get("tool", {}).get("aisland", {}).get("selection", {})
        extras = set(data.get("project", {}).get("optional-dependencies", {}))
        for name, table in tables.items():
            where = Where(path, _header_line(text, "tool", "aisland", "selection", name))
            if not isinstance(table, dict):
                raise DeclarationError(f"{where}: selection {name!r} is not a table")
            unknown = [key for key in table if key not in _SELECTION_KEYS]
            if unknown:
                raise DeclarationError(
                    f"{where}: selection {name!r} has the unknown setting {unknown[0]!r} "
                    f"(a selection takes {' and '.join(_SELECTION_KEYS)})"
                )
            choices = _strings(table.get("choices", []))
            if choices is None:
                raise DeclarationError(f'{where}: selection {name!r}: choices must be a list of names, as ["a", "b"]')
            for choice in choices:
                if choice not in extras:
                    raise DeclarationError(
                        f"{where}: {project} offers {choice!r} for selection {name!r}, "
                        "but defines no extra of that name"
                    )
            offered.setdefault(name, {})[project] = choices
            if "question" in table:
                if not isinstance(table["question"], str):
                    raise DeclarationError(f'{where}: selection {name!r}: question must be text, as question = "…?"')
                if name in defined:
                    first = defined[name][1].where
                    raise DeclarationError(
                        f"selection {name!r} is defined twice: {first} and {where} both ask its question "
                        "— keep it in one project"
                    )
                defined[name] = (table["question"], Declared(project, where))
    for name, offers in offered.items():
        if name in defined:
            question, declared = defined[name]
            found.selections[name] = Selection(name, question, declared, offers)
        else:
            found.undefined[name] = list(offers)


def _services(projects: List[Tuple[str, Path, str, Dict[str, Any]]], found: Discovery) -> None:
    for project, path, text, data in projects:
        for name, table in data.get("tool", {}).get("aisland", {}).get("service", {}).items():
            where = Where(path, _header_line(text, "tool", "aisland", "service", name))
            if not isinstance(table, dict):
                raise DeclarationError(f"{where}: service {name!r} is not a table")
            unknown = [key for key in table if key not in _SERVICE_KEYS]
            if unknown:
                raise DeclarationError(
                    f"{where}: service {name!r} has the unknown setting {unknown[0]!r} "
                    f"(a service takes {', '.join(_SERVICE_KEYS[:-1])} and {_SERVICE_KEYS[-1]})"
                )
            command = _strings(table.get("command"))
            if not command:
                raise DeclarationError(
                    f"{where}: service {name!r} has no command — "
                    'write it as  command = ["<program>", "<argument>", …]'
                )
            port = table.get("port")
            if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
                raise DeclarationError(f"{where}: service {name!r}: port must be a number, as port = 8080")
            ready = table.get("ready")
            if ready is not None and not isinstance(ready, str):
                raise DeclarationError(f'{where}: service {name!r}: ready must be a path, as ready = "/health"')
            needs = table.get("needs", {})
            if not isinstance(needs, dict) or not all(isinstance(v, str) for v in needs.values()):
                raise DeclarationError(
                    f'{where}: service {name!r}: needs must name a choice per selection, as needs = {{ tracker = "aim" }}'
                )
            if name in found.services:
                raise DeclarationError(
                    f"service {name!r} is declared twice: {found.services[name].where} and {where} "
                    "— keep it in one project"
                )
            found.services[name] = Service(name, project, where, tuple(command), port, ready, dict(needs))


def _plugins(projects: List[Tuple[str, Path, str, Dict[str, Any]]], found: Discovery) -> None:
    for project, path, text, data in projects:
        declared = data.get("project", {}).get("entry-points", {}).get(ENTRY_POINT_GROUP, {})
        distribution = str(data.get("project", {}).get("name") or project)
        header = _header_line(text, "project", "entry-points", ENTRY_POINT_GROUP)
        for name, target in declared.items():
            where = Where(path, _key_line(text, header, name) if header else None)
            if name in found.plugins:
                raise DeclarationError(
                    f"plugin {name!r} is declared twice: {found.plugins[name].where} and {where} "
                    "— keep it in one project"
                )
            found.plugins[name] = Plugin(name, project, distribution, str(target), where)


def discover(root: Path) -> Discovery:
    """Stage 1: what the projects of the workspace at ``root`` declare. Raises :class:`DeclarationError`."""
    projects = _project_tables(root)
    found = Discovery()
    _selections(projects, found)
    _services(projects, found)
    _plugins(projects, found)
    return found


# --- stage 2 --------------------------------------------------------------------------------------------------------


def _normalized(name: str) -> str:
    """A distribution name as packaging compares them (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _entry_point(plugin: Plugin) -> Optional[importlib.metadata.EntryPoint]:
    """The installed entry point of ``plugin``: its name, in the group, from the declaring project's distribution."""
    for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP, name=plugin.name):
        if entry.dist is not None and _normalized(entry.dist.name) == _normalized(plugin.distribution):
            return entry
    return None


def _check(plugin: Plugin, obj: object) -> str:
    """The plugin's kind, after checking it has the methods that kind needs."""
    what = f"{plugin.where}: plugin {plugin.name!r} ({plugin.target})"
    kind = getattr(obj, "kind", None)
    if kind not in PLUGIN_KINDS:
        raise DeclarationError(f"{what} has kind {kind!r} (a plugin's kind is {', '.join(PLUGIN_KINDS)})")
    for method in _KIND_METHODS[kind]:
        if not callable(getattr(obj, method, None)):
            raise DeclarationError(f"{what} is a {kind} but has no {method}(ctx)")
    return str(kind)


def load(found: Discovery) -> Dict[str, Loaded]:
    """Stage 2: each declared plugin, loaded when its project is installed. Raises :class:`DeclarationError`.

    Only what a workspace project declares is loaded — an installed package of the same group that no project of
    this workspace declares is not a plugin of this workspace. A class is instantiated with no arguments."""
    loaded: Dict[str, Loaded] = {}
    for name, plugin in found.plugins.items():
        entry = _entry_point(plugin)
        if entry is None:
            loaded[name] = Loaded(plugin, "not installed")
            continue
        try:
            target = entry.load()
            obj = target() if isinstance(target, type) else target
        except Exception as exc:  # the project's own code: any failure of it is reported, never aisland's crash
            raise DeclarationError(
                f"{plugin.where}: plugin {name!r} ({plugin.target}) cannot be loaded: {exc}"
            ) from exc
        loaded[name] = Loaded(plugin, "installed", _check(plugin, obj), obj)
    return loaded


# --- reporting ------------------------------------------------------------------------------------------------------


def _place(declared_project: str, where: Where) -> Dict[str, Any]:
    return {"project": declared_project, "file": str(where.file), "line": where.line}


def as_json(found: Discovery, loaded: Mapping[str, Loaded]) -> Dict[str, Any]:
    """Everything found, as one JSON-shaped object (what ``aisland workspace show --json`` prints)."""
    return {
        "selections": {
            name: {
                "question": selection.question,
                "defined_by": _place(selection.defined_by.project, selection.defined_by.where),
                "choices": selection.choices,
            }
            for name, selection in found.selections.items()
        },
        "services": {
            name: {
                **_place(service.project, service.where),
                "command": list(service.command),
                "port": service.port,
                "ready": service.ready,
                "needs": service.needs,
            }
            for name, service in found.services.items()
        },
        "plugins": {
            name: {
                "project": plugin.project,
                "target": plugin.target,
                "state": loaded[name].state,
                "file": str(plugin.where.file),
                "line": plugin.where.line,
            }
            for name, plugin in found.plugins.items()
        },
        "undefined": found.undefined,
    }


def report_lines(found: Discovery, loaded: Mapping[str, Loaded]) -> List[str]:
    """What ``aisland workspace show`` prints: one aligned row per declaration."""
    rows: List[Tuple[str, str, str, str]] = []
    for name, selection in found.selections.items():
        offered = ", ".join(f"{', '.join(choices)} ({project})" for project, choices in selection.choices.items())
        rows.append(("selection", name, selection.defined_by.project, f"choices: {offered}"))
    for name, service in found.services.items():
        rows.append(("service", name, service.project, f"port {service.port}" if service.port else "no port"))
    for name, plugin in found.plugins.items():
        state = loaded[name]
        detail = f"{plugin.target} — {state.kind}" if state.state == "installed" else f"{plugin.target} — not installed"
        rows.append(("plugin", name, plugin.project, detail))
    if not rows:
        lines = ["Declared by the projects: nothing"]
    else:
        widths = [max(len(row[column]) for row in rows) for column in range(3)]
        lines = ["Declared by the projects:"] + [
            "  " + "  ".join(cell.ljust(width) for cell, width in zip(row[:3], widths)) + "  " + row[3] for row in rows
        ]
    for name, projects in found.undefined.items():
        lines.append(f"Choices for a selection no project defines: {name} ({', '.join(projects)})")
    return lines
