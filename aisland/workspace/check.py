"""The keys of ``workspace.toml`` no part of aisland reads — a typo, almost always.

:mod:`aisland.workspace.file` keeps every key of a project's table as written (``others``), because later steps own
them: ``source``, ``mirror``, ``active``, ``track``, ``test-stats``. A key nobody owns is kept too, and silently does
nothing — ``sorce = "pypi"`` leaves the project local. :func:`unknown_keys` names each such key with its line and the
closest key aisland reads; ``aisland setup``, ``aisland workspace show`` and ``aisland workspace configure`` print
them as warnings. A warning never fails a command: the file is still the user's.

The owners are listed here, next to each other, so a new setting is one more name: a project's keys are
``workspace_file``'s own (``path``, ``url``, ``branch``) and each plugin's; the workspace's are its ``name`` and
``format``, the switches every project follows (``track``, ``test-stats``) and every selection — ``extras`` and each
one a project of the workspace defines.
"""

from __future__ import annotations

import difflib
import tomllib
from pathlib import Path
from typing import List, Tuple

from aisland import activation, ci_templates, discovery, harness, mirrors
from aisland import pyproject as pyproject_toml
from aisland import pytest_stats, selection, sources, tracking
from aisland.toml_places import FORMAT_KEY, Where, key_line, table_line
from aisland.workspace import file as workspace_file

#: Every key a project's table may hold, and the part of aisland that reads it.
PROJECT_KEYS: Tuple[str, ...] = (
    *workspace_file.PROJECT_KEYS,
    *sources.COMMITTED_KEYS,
    mirrors.MIRROR_KEY,
    activation.ACTIVE_KEY,
    tracking.TRACK_KEY,
    pytest_stats.RECORD_KEY,
    workspace_file.CI_SECTION,
)
#: Every key ``[workspace]`` may hold besides the selections.
WORKSPACE_KEYS: Tuple[str, ...] = (
    workspace_file.NAME_KEY,
    FORMAT_KEY,
    tracking.TRACK_KEY,
    pytest_stats.RECORD_KEY,
)


def _selections(root: Path) -> Tuple[str, ...]:
    """The selections of the workspace; none but ``extras`` while a project's declaration cannot be read — the
    command that reads the declarations says why, and a warning here would only guess."""
    try:
        return tuple(axis.name for axis in selection.axes(root))
    except discovery.DeclarationError:
        return (selection.EXTRAS.name,)


def _hint(key: str, known: Tuple[str, ...]) -> str:
    close = difflib.get_close_matches(key, known, n=1)
    return f" (did you mean {close[0]}?)" if close else f" (it reads {', '.join(sorted(known))})"


def unknown_keys(root: Path) -> List[str]:
    """One warning line per key of ``<root>/workspace.toml`` that no part of aisland reads, in the file's order, each
    naming its file and line. None for a workspace without the file, or one the reader refuses (it says why)."""
    path = root / workspace_file.FILE_NAME
    if not path.is_file():
        return []
    text = path.read_text()
    try:
        data: pyproject_toml.Table = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    warnings: List[str] = []
    settings = data.get("workspace")
    if isinstance(settings, dict):
        known = WORKSPACE_KEYS + _selections(root)
        header = table_line(text, "workspace")
        for key in settings:
            if key not in known:
                where = Where(path, key_line(text, header, key) if header else None)
                warnings.append(f"Warning: {where}: [workspace] {key} is no setting aisland reads{_hint(key, known)}")
    ci = data.get(workspace_file.CI_SECTION)
    if isinstance(ci, dict):
        known = (ci_templates.TEMPLATES_KEY, ci_templates.VARIABLES_KEY)
        header = table_line(text, workspace_file.CI_SECTION)
        for key in ci:
            if key not in known:
                where = Where(path, key_line(text, header, key) if header else None)
                warnings.append(f"Warning: {where}: [ci] {key} is no setting aisland reads{_hint(key, known)}")
    harness_table = data.get(workspace_file.HARNESS_SECTION)
    if isinstance(harness_table, dict):
        known = (harness.SETS_KEY,)
        header = table_line(text, workspace_file.HARNESS_SECTION)
        for key in harness_table:
            if key not in known:
                where = Where(path, key_line(text, header, key) if header else None)
                warnings.append(f"Warning: {where}: [harness] {key} is no setting aisland reads{_hint(key, known)}")
    projects = data.get("project")
    for name, table in projects.items() if isinstance(projects, dict) else []:
        if not isinstance(table, dict):
            continue
        header = table_line(text, "project", name)
        for key in table:
            if key not in PROJECT_KEYS:
                where = Where(path, key_line(text, header, key) if header else None)
                warnings.append(
                    f"Warning: {where}: project {name!r}: {key} is no setting aisland reads{_hint(key, PROJECT_KEYS)}"
                )
    return warnings
