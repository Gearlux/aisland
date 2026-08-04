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

The retention sweep only ever removes directories whose name parses as :data:`STAMP_FORMAT`
— i.e. directories this tool created. Anything else a human dropped under the backup root is
left alone rather than swept up by a tool the user invoked to clean a *project*.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import click

from aisland.manager import DATA_DIR, get_workspace_root
from aisland.sources import known_projects, project_path

#: Backup store, under aisland's own data directory (git-ignored via `data/`, and NOT touched
#: by `aisland reset` — that command wipes each *service's* subdirectory, not the whole tree).
BACKUP_DIRNAME = "backups"

#: Subdirectory of a single backup that mirrors the project tree. Keeping the payload one
#: level down leaves room for `manifest.json` beside it without it ever colliding with a
#: project path called "manifest.json".
PAYLOAD_DIRNAME = "files"

MANIFEST_NAME = "manifest.json"

#: Directory-name format for one backup, and the ONLY thing the retention sweep parses.
#: The name is the record of when the backup was taken: a manifest can be missing (an
#: interrupted run) and an mtime changes whenever anything is written underneath, but the
#: name is fixed at creation and is what makes "older than N days" answerable offline.
STAMP_FORMAT = "%Y%m%d-%H%M%S"

#: Retention window, in days, applied on every run.
DEFAULT_KEEP_DAYS = 7


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


@dataclass(frozen=True)
class Backup:
    """One dated backup directory under the backup root.

    Args:
        path: The ``<backup root>/<project>/<stamp>`` directory.
        created: Timestamp parsed from the directory name.
        bytes: Payload size as recorded in the manifest (0 when it cannot be read).
    """

    path: Path
    created: datetime
    bytes: int


def backup_root(data_dir: Path = DATA_DIR) -> Path:
    """Directory holding every backup, for every project."""
    return data_dir / BACKUP_DIRNAME


# --- discovery ----------------------------------------------------------------------------


def ignored_paths(project_dir: Path) -> List[Path]:
    """Absolute paths of everything git ignores in ``project_dir``.

    Uses ``git status --porcelain --ignored -z``, which collapses a fully-ignored directory
    into a single entry (``.venv/``) instead of listing its contents — so a 30k-file virtualenv
    is one path to move, not 30k. ``-z`` is what makes the output unambiguous: paths are
    NUL-separated and never quoted or escaped, so a filename containing a space, a quote or a
    newline round-trips exactly.

    Raises:
        RuntimeError: if git fails (e.g. the directory is not a repository).
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "--ignored", "-z"],
        cwd=str(project_dir),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git status failed in {project_dir}")

    paths: List[Path] = []
    for record in result.stdout.split("\0"):
        # Porcelain v1 records are "XY <path>"; ignored entries are always "!! ".
        if record.startswith("!! "):
            paths.append(project_dir / record[3:])
    return paths


def _measure(path: Path) -> Tuple[int, int, bool]:
    """``(bytes, files, is_dir)`` for ``path``.

    One ``lstat`` decides both the size and the shape, which is what keeps this total: the
    obvious ``path.is_symlink() or not path.is_dir()`` spelling stats twice AND propagates
    ``PermissionError`` out of the plan (``Path.is_symlink`` swallows only ENOENT-ish errors),
    so an unreadable parent would abort the whole run with a traceback instead of costing one
    entry its size. Symlinks are measured, never followed — ``S_ISDIR`` is False for a link,
    so a link into `$DATA_ROOT` counts as the one entry it is, not the tree it points at.
    """
    try:
        info = path.lstat()
    except OSError:
        return 0, 1, False
    if not stat.S_ISDIR(info.st_mode):
        return info.st_size, 1, False

    total = 0
    files = 0
    for root, _dirs, names in os.walk(path):  # followlinks=False by default
        for name in names:
            files += 1
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:  # pragma: no cover - racing removal
                pass
    return total, files, True


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
            size, files, is_dir = _measure(path)
            rel = str(path.relative_to(project_dir))
            entries.append(Entry(path=path, rel=rel, bytes=size, files=files, is_dir=is_dir))
    return sorted(entries, key=lambda entry: entry.rel)


# --- retention ----------------------------------------------------------------------------


def read_backups(root: Path) -> List[Backup]:
    """Every backup under ``root``, oldest first.

    Directories whose name does not parse as :data:`STAMP_FORMAT` are skipped — this tool
    only ever reports (and only ever deletes) what it created.
    """
    backups: List[Backup] = []
    if not root.is_dir():
        return backups

    for project_dir in sorted(child for child in root.iterdir() if child.is_dir()):
        for stamp_dir in sorted(child for child in project_dir.iterdir() if child.is_dir()):
            try:
                created = datetime.strptime(stamp_dir.name, STAMP_FORMAT)
            except ValueError:
                continue
            backups.append(Backup(path=stamp_dir, created=created, bytes=_manifest_bytes(stamp_dir)))
    return sorted(backups, key=lambda backup: backup.created)


def _manifest_bytes(stamp_dir: Path) -> int:
    """Payload size recorded in a backup's manifest; 0 when it is missing or unreadable.

    Read from the manifest rather than re-walked from disk: the number was already computed
    when the backup was taken, and a sweep that stat-walks every expiring tree just to print
    a "freed" line would cost more than the deletion it reports.
    """
    try:
        with open(stamp_dir / MANIFEST_NAME, "rb") as handle:
            data = json.load(handle)
        return int(data.get("bytes", 0))
    except (OSError, ValueError, TypeError):
        return 0


def prune_backups(root: Path, keep_days: int, *, now: datetime, dry_run: bool = False) -> List[Backup]:
    """Delete backups older than ``keep_days``; return what was (or would be) deleted."""
    cutoff = now - timedelta(days=keep_days)
    expired = [backup for backup in read_backups(root) if backup.created <= cutoff]
    if dry_run:
        return expired

    for backup in expired:
        shutil.rmtree(backup.path)
        parent = backup.path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()  # the project has no backups left; don't leave an empty shell
    return expired


# --- the command --------------------------------------------------------------------------


def _human(size: int) -> str:
    """Byte count as a short human-readable string."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover - unreachable, the loop returns at TiB


def _resolve_project_dir(project: str, workspace_root: Path) -> Path:
    """Validated project directory, or raise ``click.ClickException`` explaining why not.

    The equality check against ``git rev-parse --show-toplevel`` is the guard that matters:
    every workspace project is a submodule (a `.git` FILE, not a directory), and `git status`
    run in a directory that is merely *inside* a repository reports that repository's ignored
    set. Without this check, a name that resolved to a plain subdirectory would quietly clean
    the whole enclosing repo.
    """
    # The root is refused by NAME as well as by path: `project_path` never returns the root
    # for a plain name, so `aisland clean acme` would otherwise fall through to the generic
    # "unknown project" list and leave the operator guessing whether it was a typo.
    if project == workspace_root.name or project_path(workspace_root, project).resolve() == workspace_root.resolve():
        raise click.ClickException(
            f"Refusing to clean the workspace root ({workspace_root}).\n"
            "Its ignored set is the shared .venv, the .env every config reads $DATA_ROOT from, "
            "and the generated uv.toml / pyrightconfig.json.\n"
            "Clean one project at a time instead: aisland clean <project>"
        )

    known = known_projects(workspace_root)
    if project not in known:
        raise click.ClickException(f"Unknown project '{project}'. Known projects: {', '.join(known)}")

    project_dir = project_path(workspace_root, project)
    if not (project_dir / ".git").exists():
        raise click.ClickException(
            f"{project_dir} is not a git repository — nothing to clean.\n"
            "A project on a `github` or `pypi` source has no checkout; see: aisland source list"
        )

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(project_dir),
        text=True,
        capture_output=True,
    )
    toplevel = Path(result.stdout.strip()) if result.returncode == 0 else None
    if toplevel is None or toplevel.resolve() != project_dir.resolve():
        raise click.ClickException(
            f"{project_dir} is not the root of its git repository"
            + (f" (that is {toplevel})" if toplevel else "")
            + " — refusing to clean, since git would report the enclosing repository's ignored files."
        )
    return project_dir


def clean_project(
    project: str,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    dry_run: bool = False,
    assume_yes: bool = False,
    workspace_root: Optional[Path] = None,
    data_dir: Path = DATA_DIR,
    now: Optional[datetime] = None,
) -> int:
    """Move ``project``'s git-ignored content into a dated backup, then sweep old backups.

    Args:
        project: Workspace project name (as listed by `aisland source list`).
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
    project_dir = _resolve_project_dir(project, root)

    store = backup_root(data_dir)
    destination = store / project / now.strftime(STAMP_FORMAT)
    entries = plan_entries(project_dir, store.resolve())

    click.echo(click.style(f"Git-ignored content in {project} ({project_dir})", bold=True))
    if not entries:
        click.echo("  (nothing ignored — the checkout is already clean)")
    for entry in entries:
        label = entry.rel + ("/" if entry.is_dir else "")
        click.echo(f"  {label:<48} {entry.files:>8} files  {_human(entry.bytes):>10}")

    total_bytes = sum(entry.bytes for entry in entries)
    total_files = sum(entry.files for entry in entries)
    if entries:
        click.echo(f"  {'':<48} {total_files:>8} files  {_human(total_bytes):>10}  in {len(entries)} entries")
    if store.resolve().is_relative_to(project_dir.resolve()):
        click.echo(f"\nNote: the backup store ({store}) lives inside this project and is preserved.")

    expired = prune_backups(store, keep_days, now=now, dry_run=True)

    if dry_run:
        click.echo(f"\nWould move the above into {destination}")
        _report_pruned(expired, keep_days, dry_run=True)
        click.echo("\nDry run — nothing was moved or deleted.")
        return 0

    if entries and not assume_yes:
        click.echo(f"\nEverything above moves into {destination} and is deleted after {keep_days} days.")
        if not click.confirm(f"Move {len(entries)} entries ({_human(total_bytes)}) out of {project}?"):
            click.echo("Nothing was moved.")
            return 0

    # Sweep first: on a machine that is out of disk — the reason to run this at all — the
    # expiring backups are space the move itself may need.
    _report_pruned(prune_backups(store, keep_days, now=now), keep_days, dry_run=False)

    moved: List[Entry] = []
    failed: List[Tuple[Entry, str]] = []
    for entry in entries:
        target = destination / PAYLOAD_DIRNAME / entry.rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry.path), str(target))
        except (OSError, shutil.Error) as exc:
            failed.append((entry, str(exc)))
            click.echo(click.style(f"  ! could not move {entry.rel}: {exc}", fg="red"), err=True)
        else:
            moved.append(entry)

    if not moved and not failed:
        return 0

    _write_manifest(destination, project, project_dir, now, moved, failed)
    click.echo(f"\nMoved {len(moved)} entries ({_human(sum(entry.bytes for entry in moved))}) into {destination}")
    click.echo(f"Restore with:  cp -R {destination / PAYLOAD_DIRNAME}/. {project_dir}/")
    if failed:
        click.echo(click.style(f"{len(failed)} entries could not be moved (left in place).", fg="red"), err=True)
        return 1
    return 0


def _report_pruned(expired: List[Backup], keep_days: int, *, dry_run: bool) -> None:
    """Print the retention sweep's outcome."""
    if not expired:
        return
    verb = "Would delete" if dry_run else "Deleted"
    freed = sum(backup.bytes for backup in expired)
    click.echo(f"\n{verb} {len(expired)} backup(s) older than {keep_days} days ({_human(freed)}):")
    for backup in expired:
        click.echo(f"  {backup.path}")


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
    retention sweep later reports as freed — see :func:`_manifest_bytes`.
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
