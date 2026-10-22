"""``aisland workspace configure``: one set of commands for every plugin of the workspace.

A plugin declares its keys and three functions; the commands are built from them (``aisland/cli.py``)::

    aisland workspace configure                                   every plugin, and how to set it
    aisland workspace configure <plugin> [--json]                 its rows (and notes)
    aisland workspace configure <plugin> set PROJECT VALUE [--<key> VALUE…] [--machine-only]
    aisland workspace configure <plugin> unset PROJECT [--machine-only]

so a plugin writes no command of its own. A plugin's settings live in ``workspace.toml`` (committed) and in
``.git/config`` as ``aisland.<plugin>.<project>.<key>`` (this machine); the plugin's functions read and write them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from aisland import sources


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


def overview() -> List[str]:
    """What ``aisland workspace configure`` prints: each plugin, what it is for, and how to set it."""
    width = max(len(name) for name in PLUGINS)
    lines: List[str] = []
    for name in sorted(PLUGINS):
        plugin = PLUGINS[name]
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


def _source_notes(root: Path) -> List[str]:
    """This machine's settings in the old one-key spelling, each with the command that sets it again."""
    lines: List[str] = []
    for project, value in sources.old_overrides(root).items():
        lines.append(f"This machine's aisland.source.{project} = {value} is no longer read; set it again with:")
        try:
            old = sources.ProjectSource.parse(value)
        except ValueError:
            lines.append(f"  aisland workspace configure source set {project} <local|github|pypi> --machine-only")
            continue
        detail = (
            f" --branch {old.ref}"
            if old.kind in sources.REF_KINDS
            else f" --version {old.version}" if old.version else ""
        )
        lines.append(f"  aisland workspace configure source set {project} {old.kind}{detail} --machine-only")
    return lines


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
    notes=_source_notes,
)

#: Every plugin of ``aisland workspace configure``, by name.
PLUGINS: Dict[str, Plugin] = {SOURCE.name: SOURCE}
