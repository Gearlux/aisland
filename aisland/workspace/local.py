"""A local workspace (``aisland workspace init --local``): a root repository that tracks only the projects.

Some workspaces have no root repository to share — each project is its own repository, and the root only holds them
together. Submodules still need a superproject, so ``init --local`` makes one that stays out of the way: its
``.gitignore`` ignores everything (``/*``), so only ``.gitmodules`` and the submodules are tracked. Whether the root
records the projects' commits is a choice of its own (:mod:`aisland.workspace.tracking`, ``init --untracked``).

The workspace is marked in this machine's ``.git/config`` (``aisland.workspace.local``) — the root is no repository
anyone shares. :func:`settle` keeps a marked workspace committed after ``.gitmodules`` changes or a project is added,
by hand (``git submodule add -f <url>``: ``/*`` makes git refuse the path without ``-f``), by the ``active`` plugin or
by ``aisland workspace track set|unset``: ``.gitmodules`` and the new projects are committed — only those paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from aisland import gitcmd, machine_config
from aisland.workspace import gitmodules

#: This machine's mark of a local workspace, in the root's ``.git/config``.
MARKER = "aisland.workspace.local"

#: The commit message of a settle, the projects' paths appended.
COMMIT_MESSAGE = "chore: the workspace's projects"


def is_local(root: Path) -> bool:
    """Whether ``root`` is a repository marked local on this machine."""
    if not (root / ".git").is_dir():
        return False
    return machine_config.get_bool(root, MARKER) is True


def mark(root: Path) -> None:
    """Mark the repository ``root`` local on this machine."""
    machine_config.put(root, MARKER, True, check=True)


def submodule_names(root: Path) -> Dict[str, str]:
    """``{path: name}`` of every submodule ``.gitmodules`` declares, in its order."""
    return {submodule.path: submodule.name for submodule in gitmodules.read(root)}


@dataclass
class Settled:
    """What :func:`settle` committed (or tried to), and git's refusal when it refused."""

    committed: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def lines(self) -> List[str]:
        """One line for setup's report, or none when nothing was to commit."""
        if not self.committed:
            return []
        if self.error is None:
            return [f"committed {', '.join(self.committed)}"]
        return [f"not committed ({', '.join(self.committed)}): {self.error}"]


def settle(root: Path) -> Settled:
    """In a local workspace, commit ``.gitmodules`` and the projects that changed, nothing else staged; anywhere else,
    nothing. A commit git refuses (no identity) is said, never raised."""
    done = Settled()
    if not is_local(root):
        return done
    submodules = submodule_names(root)
    if not submodules:
        return done
    gitcmd.git(root, "add", "--", ".gitmodules")
    # Staged against HEAD (or, before the first commit, against nothing): `.gitmodules` and new or moved projects —
    # `--ignore-submodules=none`, or an untracked project (`ignore = all`) is hidden from the diff too.
    staged = set(
        gitcmd.git(
            root, "diff", "--cached", "--name-only", "--no-renames", "--ignore-submodules=none"
        ).stdout.splitlines()
    )
    done.committed = [path for path in [".gitmodules", *submodules] if path in staged]
    if not done.committed:
        return done
    message = f"{COMMIT_MESSAGE} ({', '.join(p for p in done.committed if p != '.gitmodules') or '.gitmodules'})"
    result = gitcmd.git(root, "commit", "-q", "-m", message, "--", *done.committed)
    if result.returncode != 0:
        done.error = (result.stderr.strip().splitlines() or ["git commit failed"])[-1]
    return done
