"""git-filter-repo, run with the git config read the way git writes it for programs (``git config --list --null``).

git-filter-repo 2.47.0, the newest release, reads ``git config --list`` line by line as ``key=value``. A value
written over several lines (an alias of the user's, or one of aisland's own before 2026-10-29) and a key with no
value stop it with ``ValueError: dictionary update sequence element #N has length 1`` before it changes anything.
``aisland wheels prune`` and ``aisland wheels reset`` run it as ``python -m aisland.filter_repo …``: :func:`main`
replaces that one reader by :func:`config_settings` and hands every argument to git-filter-repo's own ``main``. The
user's config is never touched.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Dict, Optional


def config_settings(repo_working_dir: Optional[str]) -> Dict[bytes, bytes]:
    """Every setting git reads in ``repo_working_dir``, as git-filter-repo's own reader returns them.

    ``--null`` ends each entry with a NUL and separates the key from its value by a newline, so a value may hold
    newlines; a key with no value maps to ``b""``. A key set twice keeps its last value, as git-filter-repo's does.
    """
    out = subprocess.check_output(["git", "config", "--list", "--null"], cwd=repo_working_dir)
    settings: Dict[bytes, bytes] = {}
    for entry in out.split(b"\0"):
        if entry:
            key, _, value = entry.partition(b"\n")
            settings[key] = value
    return settings


def main() -> None:
    # Imported here: git-filter-repo is the `prune` extra, and `wheels_prune._filter_repo` checks it is installed.
    import git_filter_repo

    git_filter_repo.GitUtils.get_config_settings = staticmethod(config_settings)
    sys.argv[0] = "git-filter-repo"
    git_filter_repo.main()


if __name__ == "__main__":
    main()
