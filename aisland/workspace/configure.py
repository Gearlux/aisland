"""``aisland workspace configure``: one set of commands for every plugin of the workspace.

A plugin declares its keys and three functions; the commands are built from them (``aisland/cli.py``)::

    aisland workspace configure                                   every plugin, and how to set it
    aisland workspace configure <plugin> [--json]                 its rows (and notes)
    aisland workspace configure <plugin> set PROJECT VALUE [--<key> VALUE…] [--machine-only]
    aisland workspace configure <plugin> unset PROJECT [--machine-only]

so a plugin writes no command of its own. A plugin's settings live in ``workspace.toml`` (committed) and in
``.git/config`` as ``aisland.<plugin>.<project>.<key>`` (this machine); the plugin's functions read and write them.

A SELECTION (:mod:`aisland.workspace.selection`: aisland's ``extras`` and each one a project declares) is a plugin too, of the
whole workspace rather than of a project — ``set NAME… [--machine-only]`` and ``unset [NAME…] [--machine-only]``,
committed as ``[workspace] <selection>`` and chosen here as ``aisland.<selection>.selected``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

from aisland.workspace import activation, mirrors, selection, sources


@dataclass(frozen=True)
class Key:
    """One setting a plugin takes: the value of ``set`` (the plugin's main key) or a ``--<name>`` option of it."""

    name: str
    help: str
    choices: Optional[Tuple[str, ...]] = None


#: What a plugin's ``set`` is called with: the workspace, the project, the main key's value, each option's value
#: (None when not given) and whether only this machine changes. It returns the command's exit code.
SetFunction = Callable[[Path, str, str, Dict[str, Optional[str]], bool], int]


@dataclass(frozen=True)
class Plugin:
    """A plugin of ``aisland workspace configure``: its name, what it is for, its keys and its three functions."""

    name: str
    summary: str
    main: Key
    options: Tuple[Key, ...]
    rows: Callable[[Path], List[Dict[str, str]]]
    set: SetFunction
    unset: Callable[[Path, str, bool], int]
    empty: str
    notes: Callable[[Path], List[str]] = field(default=lambda root: [])

    def usage(self) -> str:
        """``set``'s synopsis: ``set PROJECT {a|b} [--key KEY] [--machine-only]``."""
        value = "{" + "|".join(self.main.choices) + "}" if self.main.choices else self.main.name.upper()
        options = " ".join(f"[--{key.name} {key.name.upper()}]" for key in self.options)
        return " ".join(part for part in ("set PROJECT", value, options, "[--machine-only]") if part)


def table(rows: List[Dict[str, str]]) -> List[str]:
    """``rows`` as aligned columns under their upper-cased keys, two spaces apart."""
    columns = list(rows[0])
    widths = {column: max(len(column), *(len(row[column]) for row in rows)) for column in columns}
    header = "  " + "  ".join(column.upper().ljust(widths[column]) for column in columns)
    lines = [header.rstrip()]
    for row in rows:
        lines.append(("  " + "  ".join(row[column].ljust(widths[column]) for column in columns)).rstrip())
    return lines


@dataclass(frozen=True)
class SelectionPlugin:
    """A selection of :mod:`aisland.workspace.selection` as a plugin: one list of names for the whole workspace."""

    axis: selection.Axis

    @property
    def name(self) -> str:
        return self.axis.name

    @property
    def summary(self) -> str:
        return self.axis.question

    def usage(self) -> str:
        return "set NAME… [--machine-only]"


AnyPlugin = Union[Plugin, SelectionPlugin]


def plugins(root: Optional[Path]) -> Dict[str, AnyPlugin]:
    """Every plugin: the built-ins of :data:`PLUGINS`, and — in a workspace — each selection. Raises
    :class:`aisland.workspace.discovery.DeclarationError` for a selection declared in a way aisland cannot use."""
    found: Dict[str, AnyPlugin] = dict(PLUGINS)
    if root is not None:
        found.update({axis.name: SelectionPlugin(axis) for axis in selection.axes(root)})
    return found


def overview(root: Optional[Path]) -> List[str]:
    """What ``aisland workspace configure`` prints: each plugin, what it is for, and how to set it."""
    found = plugins(root)
    width = max(len(name) for name in found)
    lines: List[str] = []
    for name in sorted(found):
        plugin = found[name]
        lines.append(f"  {name.ljust(width)}  {plugin.summary}")
        lines.append(f"  {' ' * width}  {plugin.usage()}")
    return lines


# --- source ----------------------------------------------------------------------------------------------------------


def _source_set(root: Path, project: str, kind: str, options: Dict[str, Optional[str]], machine_only: bool) -> int:
    return sources.set_project_source(
        project=project,
        kind=kind,
        ref=options.get("branch"),
        version=options.get("version"),
        machine_only=machine_only,
        workspace_root=root,
    )


def _source_unset(root: Path, project: str, machine_only: bool) -> int:
    return sources.unset_project_source(project=project, machine_only=machine_only, workspace_root=root)


SOURCE = Plugin(
    name="source",
    summary="Where each project's package comes from.",
    main=Key(
        "source",
        "local: checked out, installed editable; github: no checkout, installed from the remote; pypi: the published "
        "package.",
        choices=sources.SOURCE_KINDS,
    ),
    options=(
        Key("branch", "The branch a local or github source checks out or installs from (default: the project's)."),
        Key("version", "A version specifier for a pypi source, such as '>=0.1' (default: the latest)."),
    ),
    rows=lambda root: sources.describe(workspace_root=root),
    set=_source_set,
    unset=_source_unset,
    empty=(
        "No project has a source: each one is checked out and installed editable.\n"
        "Set one with: aisland workspace configure source set <project> pypi"
    ),
)

# --- mirror ----------------------------------------------------------------------------------------------------------


def _mirror_set(root: Path, project: str, url: str, options: Dict[str, Optional[str]], machine_only: bool) -> int:
    return mirrors.set_mirror(project, url, machine_only=machine_only, workspace_root=root)


def _mirror_unset(root: Path, project: str, machine_only: bool) -> int:
    return mirrors.unset_mirror(project, machine_only=machine_only, workspace_root=root)


MIRROR = Plugin(
    name="mirror",
    summary="A second host each project is copied to (`aisland workspace mirror sync` pushes it).",
    main=Key("mirror", "The mirror's git address (ssh or https); the primary stays the .gitmodules remote."),
    options=(),
    rows=lambda root: mirrors.rows(root),
    set=_mirror_set,
    unset=_mirror_unset,
    empty="No project has a mirror.\nSet one with: aisland workspace configure mirror set <project> <url>",
)

# --- active ----------------------------------------------------------------------------------------------------------


def _active_set(root: Path, project: str, value: str, options: Dict[str, Optional[str]], machine_only: bool) -> int:
    return activation.set_active(root, project, value == "true", machine_only)


ACTIVE = Plugin(
    name="active",
    summary="Which projects this machine has (a project with a committed value is optional, the value its default).",
    main=Key(
        "active", "true: checked out and installed; false: left out, its folder empty.", choices=("true", "false")
    ),
    options=(),
    rows=lambda root: activation.rows(root),
    set=_active_set,
    unset=lambda root, project, machine_only: activation.unset_active(root, project, machine_only),
    empty=(
        "Every project is part of the workspace on this machine.\n"
        "Make one optional with: aisland workspace configure active set <project> false"
    ),
)

#: Every plugin of ``aisland workspace configure``, by name.
PLUGINS: Dict[str, Plugin] = {plugin.name: plugin for plugin in (ACTIVE, MIRROR, SOURCE)}
