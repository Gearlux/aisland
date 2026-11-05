"""Reclaim a project's disk space by moving its git-ignored content into a dated backup.

Workspace projects accumulate large untracked artifacts — `.venv/`, `build/`, `coverage-*`,
`mypy_cache`, run directories, downloaded checkpoints. They are all git-ignored, so
`git status` stays clean while the checkout grows to gigabytes, and the obvious tool for
removing them (`git clean -Xdf`) deletes them *irreversibly*. In a workspace where an
ignored path can be load-bearing (a provisioned `bin/`, a service's `data/`, a `.env`),
irreversible is the wrong default: the mistake is only discovered after the deletion.

So this module MOVES rather than deletes. Everything git ignores in one project is renamed
into ``aisland/data/backups/<project>/<stamp>/files/``, preserving its path relative to the
project, and a ``manifest.json`` records what landed there. A rename inside the same
filesystem is effectively free regardless of directory size, so a 2 GiB `.venv` costs the
same as an empty one — and the space is genuinely reclaimed on the NEXT run, when the
retention sweep drops backups older than ``--keep-days`` (7 by default).

Two scope decisions are deliberate and load-bearing:

* **One project per invocation.** There is no `--all`. The blast radius of "delete every
  ignored file in the workspace" is the entire shared `.venv`, every service's data
  directory and the workspace `.env` — all in one keystroke, from a command whose name
  sounds routine. Cleaning N projects is N invocations, and that is the point.
* **The workspace ROOT repository is refused outright.** Its ignored set includes the shared
  `.venv` every project is installed into, the `.env` that every YAML config reads
  ``$DATA_ROOT`` & friends from, and the generated `uv.toml` / `pyrightconfig.json`.

The backup store, its retention sweep and the restore are :mod:`aisland.backups`.
"""

from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import click

from aisland import console, gitcmd
from aisland.backups import (
    DEFAULT_KEEP_DAYS,
    MANIFEST_NAME,
    PAYLOAD_DIRNAME,
    STAMP_FORMAT,
    Backup,
    backup_root,
    human,
    measure,
    prune_backups,
    report_pruned,
    resolve_project_dir,
)
from aisland.errors import AislandError
from aisland.workspace.projects import get_workspace_root


@dataclass(frozen=True)
class Entry:
    """One git-ignored path scheduled to move out of the project.

    Args:
        path: Absolute path on disk.
        rel: Path relative to the project directory — also its path inside the backup.
        bytes: Total size below ``path`` (the file's own size when it is not a directory).
        files: Number of files below ``path`` (1 when it is not a directory).
        is_dir: True for a real directory; False for a file or a symlink to one.
    """

    path: Path
    rel: str
    bytes: int
    files: int
    is_dir: bool


# --- discovery ----------------------------------------------------------------------------


def ignored_paths(project_dir: Path) -> List[Path]:
    """Absolute paths of everything git ignores in ``project_dir``.

    Uses ``git status --porcelain --ignored -z``, which collapses a fully-ignored directory
    into a single entry (``.venv/``) instead of listing its contents — so a 30k-file virtualenv
    is one path to move, not 30k. ``-z`` is what makes the output unambiguous: paths are
    NUL-separated and never quoted or escaped, so a filename containing a space, a quote or a
    newline round-trips exactly.

    Raises:
        AislandError: if git fails (e.g. the directory is not a repository) — one ``Error:`` line naming the folder.
    """
    result = gitcmd.git(project_dir, "status", "--porcelain", "--ignored", "-z")
    if result.returncode != 0:
        cause = result.stderr.strip() or "git status failed"
        raise AislandError(f"{project_dir}: git cannot list what it ignores there: {cause}")

    paths: List[Path] = []
    for record in result.stdout.split("\0"):
        # Porcelain v1 records are "XY <path>"; ignored entries are always "!! ".
        if record.startswith("!! "):
            paths.append(project_dir / record[3:])
    return paths


def _split_around(entry: Path, protected: Path) -> Iterator[Path]:
    """Yield the movable paths for ``entry``, holding ``protected`` and its ancestors back.

    This exists for exactly one case, and it is not hypothetical: cleaning the ``aisland``
    project itself. Its `.gitignore` ignores ``data/`` wholesale, and the backup root lives
    at ``data/backups`` — so the single collapsed entry git reports CONTAINS the destination.
    Moving it would either fail (a directory cannot be renamed into itself) or, worse,
    swallow every previous backup. Splitting the entry into its children and recursing keeps
    everything else movable while the backup root stays where it is.
    """
    if entry == protected:
        return
    if entry.is_dir() and protected.is_relative_to(entry):
        for child in sorted(entry.iterdir()):
            yield from _split_around(child, protected)
        return
    yield entry


def plan_entries(project_dir: Path, protected: Path) -> List[Entry]:
    """Everything that would move out of ``project_dir``, sorted by path."""
    entries: List[Entry] = []
    for raw in ignored_paths(project_dir):
        for path in _split_around(raw, protected):
            size, files, is_dir = measure(path)
            rel = str(path.relative_to(project_dir))
            entries.append(Entry(path=path, rel=rel, bytes=size, files=files, is_dir=is_dir))
    return sorted(entries, key=lambda entry: entry.rel)


# --- the command --------------------------------------------------------------------------


def clean_project(
    project: str,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    dry_run: bool = False,
    assume_yes: bool = False,
    workspace_root: Optional[Path] = None,
    data_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """Move ``project``'s git-ignored content into a dated backup, then sweep old backups.

    Args:
        project: Workspace project name (as listed by `aisland workspace configure source`).
        keep_days: Backups older than this many days are deleted on every run.
        dry_run: Report the plan and the expiring backups; change nothing on disk.
        assume_yes: Skip the confirmation prompt.
        workspace_root: Workspace root; discovered when omitted.
        data_dir: aisland's data directory — the backup root's parent.
        now: Reference time for the retention window and the backup's name; defaults to now.

    Returns:
        Process exit code: 0 on success (including a declined confirmation), 1 if any entry
        could not be moved.
    """
    root = workspace_root or get_workspace_root()
    now = now or datetime.now()
    project_dir = resolve_project_dir(project, root)

    store = backup_root(data_dir)
    destination = store / project / now.strftime(STAMP_FORMAT)
    entries = plan_entries(project_dir, store.resolve())
    _show_entries(project, project_dir, entries, store)
    expired = prune_backups(store, keep_days, now=now, dry_run=True)

    if dry_run:
        console.echo(f"\nWould move the above into {destination}")
        report_pruned(expired, keep_days, dry_run=True)
        console.echo("\nDry run — nothing was moved or deleted.")
        return 0

    if (entries or expired) and not assume_yes and not _agreed(project, destination, keep_days, entries, expired):
        console.echo("Nothing was moved or deleted.")
        return 0

    # Sweep first: on a machine that is out of disk — the reason to run this at all — the
    # expiring backups are space the move itself may need.
    report_pruned(prune_backups(store, keep_days, now=now), keep_days, dry_run=False)

    moved, failed = _move_entries(entries, destination)
    if not moved and not failed:
        return 0
    _write_manifest(destination, project, project_dir, now, moved, failed)
    _report_moved(project, project_dir, destination, moved, failed)
    return 1 if failed else 0


def _agreed(project: str, destination: Path, keep_days: int, entries: List[Entry], expired: List[Backup]) -> bool:
    """Ask before the move AND the sweep: the expired backups may be any project's, so they are listed and named in the
    question too — a clean with nothing to move still asks before it deletes."""
    report_pruned(expired, keep_days, dry_run=True)
    actions = []
    if entries:
        console.echo(f"\nEverything above moves into {destination} and is deleted after {keep_days} days.")
        actions.append(f"move {len(entries)} entries ({human(sum(entry.bytes for entry in entries))}) out of {project}")
    if expired:
        actions.append(f"delete {len(expired)} expired backup(s) ({human(sum(backup.bytes for backup in expired))})")
    question = " and ".join(actions)
    return console.confirm(question[0].upper() + question[1:] + "?")


def _show_entries(project: str, project_dir: Path, entries: List[Entry], store: Path) -> None:
    """List what a clean of ``project`` would move, with each entry's files and size and the totals."""
    console.echo(click.style(f"Git-ignored content in {project} ({project_dir})", bold=True))
    if not entries:
        console.echo("  (nothing ignored — the checkout is already clean)")
    for entry in entries:
        label = entry.rel + ("/" if entry.is_dir else "")
        console.echo(f"  {label:<48} {entry.files:>8} files  {human(entry.bytes):>10}")
    if entries:
        total_bytes = sum(entry.bytes for entry in entries)
        total_files = sum(entry.files for entry in entries)
        console.echo(f"  {'':<48} {total_files:>8} files  {human(total_bytes):>10}  in {len(entries)} entries")
    if store.resolve().is_relative_to(project_dir.resolve()):
        console.echo(f"\nNote: the backup store ({store}) lives inside this project and is preserved.")


def _move_entries(entries: List[Entry], destination: Path) -> Tuple[List[Entry], List[Tuple[Entry, str]]]:
    """Move each entry under ``destination``'s payload: the entries moved, and ``(entry, reason)`` of the others."""
    moved: List[Entry] = []
    failed: List[Tuple[Entry, str]] = []
    for entry in entries:
        target = destination / PAYLOAD_DIRNAME / entry.rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry.path), str(target))
        except (OSError, shutil.Error) as exc:
            failed.append((entry, str(exc)))
            console.echo(click.style(f"  ! could not move {entry.rel}: {exc}", fg="red"), err=True)
        else:
            moved.append(entry)
    return moved, failed


def _report_moved(
    project: str, project_dir: Path, destination: Path, moved: List[Entry], failed: List[Tuple[Entry, str]]
) -> None:
    """Say what moved where, how to put it back, and what could not be moved."""
    console.echo(f"\nMoved {len(moved)} entries ({human(sum(entry.bytes for entry in moved))}) into {destination}")
    # Both forms are printed on purpose: the command is the one to reach for, and the `cp` is
    # the one that still works when this code is the thing that broke.
    console.echo(f"Restore with:  aisland backups restore {project}")
    console.echo(
        f"          or:  cp -R {shlex.quote(f'{destination / PAYLOAD_DIRNAME}/.')} {shlex.quote(f'{project_dir}/')}"
    )
    if failed:
        console.echo(click.style(f"{len(failed)} entries could not be moved (left in place).", fg="red"), err=True)


def _write_manifest(
    destination: Path,
    project: str,
    project_dir: Path,
    created: datetime,
    moved: List[Entry],
    failed: List[Tuple[Entry, str]],
) -> None:
    """Record what landed in this backup, beside the payload.

    Written after the moves so it describes what actually arrived. `bytes` is what the
    retention sweep later reports as freed, and `entries[].path` is what a restore puts back
    — see :func:`aisland.backups.read_manifest`.
    """
    manifest = {
        "project": project,
        "project_dir": str(project_dir),
        "created": created.isoformat(timespec="seconds"),
        "bytes": sum(entry.bytes for entry in moved),
        "files": sum(entry.files for entry in moved),
        "entries": [{"path": entry.rel, "bytes": entry.bytes, "files": entry.files} for entry in moved],
        "failed": [{"path": entry.rel, "error": error} for entry, error in failed],
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
