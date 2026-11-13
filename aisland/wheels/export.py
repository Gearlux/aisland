"""What a wheel is built from: a project's last commit (:func:`export_head`), or under ``--force`` its files as they
are, those git ignores left out (:func:`export_worktree`) — never its bare folder, where a setuptools ``build/lib``
left in a checkout puts files the project deleted long ago into the wheel (measured 2026-10-08). Each returns the last
commit's time, which the wheel carries, so unchanged sources rebuild byte for byte.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

from aisland import gitcmd
from aisland.errors import AislandError


def export_head(project_dir: Path, dest: Path) -> int:
    """Write ``project_dir``'s last commit into ``dest`` — what a wheel is built from.

    Returns the commit's time (seconds since the epoch), the timestamp its wheel carries.
    """
    archive = subprocess.run(["git", "-C", str(project_dir), "archive", "--format=tar", "HEAD"], capture_output=True)
    if archive.returncode != 0:
        reason = archive.stderr.decode(errors="replace").strip()
        raise AislandError(f"{project_dir.name}: cannot export its last commit: {reason}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(dest, filter="data")
    return int(gitcmd.git(project_dir, "log", "-1", "--format=%ct", "HEAD").stdout.strip())


def export_worktree(project_dir: Path, dest: Path) -> int:
    """Write ``project_dir``'s files as they are into ``dest`` — what ``--force`` builds a dirty project from.

    The files are the ones git tracks and the untracked ones it does not ignore: a deleted file is gone,
    a new one is in, and an ignored setuptools ``build/lib`` stays out, as it does from a commit. Returns the
    last commit's time, so a rerun over unchanged files builds the same wheel.
    """
    listed = gitcmd.git(project_dir, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise AislandError(f"{project_dir.name}: cannot list its files: {listed.stderr.strip()}")
    for relative in sorted(set(filter(None, listed.stdout.split("\0")))):
        source = project_dir / relative
        # A deleted file is still listed (it is in the index), and a nested repository is a folder: neither is
        # copied, as `git archive` carries neither.
        if not source.is_file() and not source.is_symlink():
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    return int(gitcmd.git(project_dir, "log", "-1", "--format=%ct", "HEAD").stdout.strip())
