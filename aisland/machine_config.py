"""This machine's settings of a workspace: git's ``--local`` config of a repository, never committed.

What one machine chooses lives in the root's ``.git/config`` — ``aisland.<plugin>.<project>.<key>`` (a project's
source, whether it is active, tracked, mirrored, recorded; the editor's theme) — and so do the few git keys aisland
writes for the machine (``submodule.<name>.active``, ``pull.rebase``). Every module reads and writes them through the
four functions here, so a key is always read, written and forgotten the same way. A read of a key that is not set, or
of a folder that is no repository, answers "nothing" and never raises; a write that git refuses is silent unless the
caller says ``check=True``.

``pytest_stats`` reads its own keys with the standard library: it loads in every pytest run, where aisland is not
installed (:mod:`aisland.pytest_stats`).

A workspace with a root project has no root repository: its folder holds plain clones. There the machine's settings
live in the root project's own ``.git/config`` — still this machine's, never committed — and every function here,
given the workspace folder, reads and writes that one (:func:`settings_repository`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Union

from aisland import gitcmd, locations

Value = Union[str, bool]


def _text(value: Value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else value


def settings_repository(repo: Path) -> Path:
    """The repository whose ``.git/config`` holds the settings asked of ``repo``: ``repo`` itself when it is a
    repository, else — a workspace folder with a root project, which is none — the root project's clone."""
    if (repo / ".git").exists():
        return repo
    project = locations.root_project(repo)
    return project if project is not None else repo


def get(repo: Path, key: str) -> Optional[str]:
    """The value of ``key`` in ``repo``'s own config, or ``None`` when it is not set (``""`` is a value)."""
    run = gitcmd.git(settings_repository(repo), "config", "--local", "--get", key)
    return run.stdout.strip() if run.returncode == 0 else None


def get_bool(repo: Path, key: str) -> Optional[bool]:
    """``key`` read as git reads a boolean (``true``/``yes``/``on``/``1``), or ``None`` when it is not set."""
    run = gitcmd.git(settings_repository(repo), "config", "--local", "--type=bool", "--get", key)
    return run.stdout.strip() == "true" if run.returncode == 0 else None


def get_all(repo: Path, pattern: str) -> Dict[str, str]:
    """Every setting whose key matches the regular expression ``pattern``, ``{key: value}`` in git's order; none when
    git cannot say (``repo`` is no repository). Git writes a key's section and name in lower case."""
    run = gitcmd.git(settings_repository(repo), "config", "--local", "--get-regexp", pattern)
    found: Dict[str, str] = {}
    for line in run.stdout.splitlines() if run.returncode == 0 else []:
        key, _, value = line.partition(" ")
        found[key] = value.strip()
    return found


def put(repo: Path, key: str, value: Optional[Value], *, check: bool = False) -> None:
    """Set ``key`` to ``value`` (a bool as ``true``/``false``), or forget it when ``value`` is ``None``.

    ``check=True`` raises an :class:`~aisland.errors.AislandError` naming git's own words when git refuses to write;
    forgetting a key that is not set is never an error.
    """
    where = settings_repository(repo)
    if value is None:
        gitcmd.git(where, "config", "--local", "--unset", key)
        return
    gitcmd.git(where, "config", "--local", key, _text(value), check=check)
