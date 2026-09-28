"""Which ML frameworks this workspace installs (``aisland framework``).

The framework-flavoured **facade** over :mod:`aisland.selection`, which implements the
two-layer declare/select machinery generically. The mechanism lives there because it is
not framework-specific: experiment-tracking backends use exactly the same one through the
sibling :data:`~aisland.selection.TRACKERS` axis, and a module named ``frameworks`` owning
the tracker axis would describe its first consumer rather than its role.

Everything about *how* the selection works — the declaration table, the committed
``frameworks`` key of ``config.toml`` vs. the per-machine ``.git/config aisland.frameworks``
override, why an unset selection is not an empty one, and why an override must never reach a
committed file — is documented in :mod:`aisland.selection`.

This module keeps the ``*_frameworks`` names the workspace already calls (``aisland
setup``, the Jenkins scaffolder, the CLI) bound to that axis, so nothing had to move when
the second axis arrived.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from aisland.config import CONFIG_FILE
from aisland.selection import FRAMEWORKS, Axis
from aisland.selection import apply_selection as _apply
from aisland.selection import available as _available
from aisland.selection import declarations as _declarations
from aisland.selection import declared as _declared
from aisland.selection import describe as _describe
from aisland.selection import effective as _effective
from aisland.selection import extras_for, install_target
from aisland.selection import local_override as _local_override
from aisland.selection import parse_selection
from aisland.selection import prompt_for as _prompt_for
from aisland.selection import read_committed as _read_committed
from aisland.selection import resolve_for_setup as _resolve_for_setup
from aisland.selection import set_local_override as _set_local_override
from aisland.selection import set_selection as _set_selection
from aisland.selection import unset_selection as _unset_selection
from aisland.selection import write_committed as _write_committed

#: The committed selection's key in the workspace's single ``config.toml``.
FRAMEWORKS_KEY = FRAMEWORKS.plural

#: The ``pyproject.toml`` table a project declares its framework extras in.
DECLARATION_TABLE = FRAMEWORKS.table_path

#: ``.git/config`` key holding the per-machine override (space-separated names).
CONFIG_KEY = FRAMEWORKS.config_key


def declared_frameworks(pyproject_path: Path) -> List[str]:
    """The framework extras a project declares, in declaration order."""
    return _declared(pyproject_path, FRAMEWORKS)


def declarations(workspace_root: Path) -> Dict[str, List[str]]:
    """``{project name: declared framework extras}`` for every project that declares any."""
    return _declarations(workspace_root, FRAMEWORKS)


def available_frameworks(workspace_root: Path) -> List[str]:
    """Every framework name declared by any project, sorted — the selectable vocabulary."""
    return _available(workspace_root, FRAMEWORKS)


def read_committed_frameworks(workspace_root: Path) -> Optional[List[str]]:
    """The committed selection, or ``None`` when ``config.toml`` has no ``frameworks`` key."""
    return _read_committed(workspace_root, FRAMEWORKS)


def write_committed_frameworks(workspace_root: Path, names: Sequence[str]) -> Path:
    """Record the committed selection in ``config.toml``, leaving its other sections intact."""
    return _write_committed(workspace_root, names, FRAMEWORKS)


def local_override(workspace_root: Path) -> Optional[List[str]]:
    """The per-machine selection from ``.git/config``, or ``None`` when unset."""
    return _local_override(workspace_root, FRAMEWORKS)


def set_local_override(workspace_root: Path, names: Optional[Sequence[str]]) -> None:
    """Set (or, with ``names=None``, remove) the per-machine selection."""
    _set_local_override(workspace_root, names, FRAMEWORKS)


def effective_frameworks(workspace_root: Path) -> Optional[List[str]]:
    """The selection that drives the venv: the per-machine override, else the committed one."""
    return _effective(workspace_root, FRAMEWORKS)


def prompt_for_frameworks(workspace_root: Path, choices: Sequence[str]) -> List[str]:
    """Ask which frameworks to install, returning the chosen names."""
    return _prompt_for(workspace_root, choices, FRAMEWORKS)


def resolve_for_setup(
    workspace_root: Path,
    requested: Optional[Sequence[str]],
    non_interactive: bool,
) -> Tuple[List[str], bool]:
    """The framework selection ``setup`` should install, plus whether it was just chosen."""
    return _resolve_for_setup(workspace_root, requested, non_interactive, FRAMEWORKS)


def describe(workspace_root: Optional[Path] = None) -> List[Dict[str, str]]:
    """One row per declared framework: who offers it, and whether each layer selects it."""
    return _describe(workspace_root, FRAMEWORKS)


def apply_frameworks(workspace_root: Path, selected: Sequence[str], uv_cmd: Optional[str] = None) -> int:
    """Reinstall every project that declares a framework, at the current selection."""
    return _apply(workspace_root, selected, FRAMEWORKS, uv_cmd)


def set_frameworks(names: Sequence[str], machine_only: bool = False, apply: bool = True) -> int:
    """Record a framework selection and (by default) install it. Returns an exit code."""
    return _set_selection(names, FRAMEWORKS, machine_only, apply)


def unset_frameworks(machine_only: bool = False, apply: bool = True) -> int:
    """Clear a selection: the per-machine override, or (default) the committed file too."""
    return _unset_selection(FRAMEWORKS, machine_only, apply)


__all__ = [
    "CONFIG_FILE",
    "CONFIG_KEY",
    "DECLARATION_TABLE",
    "FRAMEWORKS",
    "FRAMEWORKS_KEY",
    "Axis",
    "apply_frameworks",
    "available_frameworks",
    "declarations",
    "declared_frameworks",
    "describe",
    "effective_frameworks",
    "extras_for",
    "install_target",
    "local_override",
    "parse_selection",
    "prompt_for_frameworks",
    "read_committed_frameworks",
    "resolve_for_setup",
    "set_frameworks",
    "set_local_override",
    "unset_frameworks",
    "write_committed_frameworks",
]
