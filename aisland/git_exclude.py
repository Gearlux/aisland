"""Keep a file aisland writes out of git, on this machine only: the repository's ``.git/info/exclude``.

aisland writes files into repositories it does not own — a project's ``pyrightconfig.json``, the agent harness's
``.claude/commands`` link — and the editable install leaves a ``<name>.egg-info/`` folder. A line in the project's
tracked ``.gitignore`` would change every project (a file to commit, in every one); ``info/exclude`` is git's own
per-clone list, never committed, and leaves ``git status`` as it was.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


def _git_path(folder: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(folder), "rev-parse", *args], capture_output=True, text=True).stdout.strip()


def exclude(folder: Path, patterns: Sequence[str]) -> None:
    """Add each of ``patterns`` (gitignore syntax, relative to ``folder``) to the exclude list of the repository that
    holds ``folder``, anchored at ``folder``; a line already there is not written again. ``folder`` in no repository:
    nothing could commit the files, and nothing is written."""
    top = _git_path(folder, "--show-toplevel")
    target = _git_path(folder, "--path-format=absolute", "--git-path", "info/exclude")
    if not top or not target:
        return
    # The folder resolved, never a link inside it: resolving `.claude/commands` would follow it to the workflows.
    relative = folder.resolve().relative_to(Path(top).resolve()).as_posix()
    prefix = "/" if relative == "." else f"/{relative}/"
    path = Path(target)
    content = path.read_text() if path.is_file() else ""
    present = {entry.strip() for entry in content.splitlines()}
    missing = [line for line in (prefix + pattern for pattern in patterns) if line not in present]
    if not missing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if (not content or content.endswith("\n")) else "\n"
    path.write_text(content + sep + "".join(line + "\n" for line in missing))
