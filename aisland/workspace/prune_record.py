"""The record ``aisland wheels prune`` leaves on origin, read by everything that must know a prune happened.

A prune (:mod:`aisland.wheels.prune`) rewrites a project's history and pushes :data:`PRUNE_REF`, a chain of one commit
per prune, each holding the wheel versions it stripped (:data:`STRIPPED_FILE`) and each commit it rewrote, ``<old>
<new>`` per line (:data:`COMMITS_FILE`). ``aisland wheels reset`` follows it on another clone, and ``aisland workspace
mirror sync`` reads it to tell a mirror still on the old history from one someone pushed to: a mirror's branch at an
``<old>`` commit is the primary's history before the prune, and receives the rewritten one. Here, below both, so the
workspace layer reads the record without importing the wheels run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

from aisland import gitcmd

#: Where origin keeps the record of every prune: a chain of commits, one per prune, never a branch or a tag.
PRUNE_REF = "refs/wheels-prune/map"

#: The record commit's file listing the wheel versions (blob ids) that prune stripped, one per line.
STRIPPED_FILE = "stripped-blobs"

#: The record commit's file mapping each commit the prune rewrote, ``<old> <new>`` per line.
COMMITS_FILE = "commit-map"


@dataclass(frozen=True)
class Prune:
    """One of origin's prunes: its record commit, the versions it stripped and the commits it rewrote."""

    record: str
    stripped: List[str]
    changed: Dict[str, str]


def prunes(repo: Path, record: str = PRUNE_REF) -> List[Prune]:
    """The prunes of the chain ``record`` names in ``repo`` (a ref or a commit id), oldest first; raises
    :class:`aisland.errors.AislandError` when git cannot read it."""
    found = []
    chain = gitcmd.git(repo, "rev-list", "--reverse", record, check=True).stdout.split()
    for commit in chain:
        stripped = gitcmd.git(repo, "show", f"{commit}:{STRIPPED_FILE}", check=True).stdout.split()
        lines = gitcmd.git(repo, "show", f"{commit}:{COMMITS_FILE}", check=True).stdout.splitlines()
        pairs = [line.split() for line in lines if line]
        found.append(Prune(commit, stripped, {old: new for old, new in pairs}))
    return found


def compose(chain: Sequence[Prune]) -> Dict[str, str]:
    """Each commit any prune rewrote and the id it has after the last prune."""
    composed: Dict[str, str] = {}
    for one in chain:
        composed = {old: one.changed.get(new, new) for old, new in composed.items()}
        composed.update({old: new for old, new in one.changed.items() if old not in composed})
    return composed
