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

import filecmp
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import click

from aisland.manager import DATA_DIR, get_workspace_root
from aisland.sources import known_projects, project_path

#: Backup store, under aisland's own data directory (git-ignored via `data/`, and NOT touched
#: by `aisland services reset` — that command wipes each *service's* subdirectory, not the whole tree).
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
        files: File count as recorded in the manifest (0 when it cannot be read).
        entries: Project-relative paths the manifest says are in the payload.
    """

    path: Path
    created: datetime
    bytes: int
    files: int
    entries: Tuple[str, ...]

    @property
    def project(self) -> str:
        """The project this backup was taken from — the directory the stamp sits in."""
        return self.path.parent.name


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
            manifest = read_manifest(stamp_dir)
            backups.append(
                Backup(
                    path=stamp_dir,
                    created=created,
                    bytes=_as_int(manifest.get("bytes")),
                    files=_as_int(manifest.get("files")),
                    entries=tuple(_manifest_paths(manifest)),
                )
            )
    return sorted(backups, key=lambda backup: backup.created)


def read_manifest(stamp_dir: Path) -> Dict[str, Any]:
    """A backup's manifest, or an empty mapping when it is missing or unreadable.

    Everything about a backup is answered from here rather than re-walked from disk: the
    sizes were already computed when it was taken, and a sweep that stat-walks every expiring
    tree just to print a "freed" line would cost more than the deletion it reports. A missing
    manifest is NOT an error — an interrupted clean leaves one that way, and both the sweep
    (which reads the directory NAME) and a restore (which falls back to the payload's own
    layout) still work.
    """
    try:
        with open(stamp_dir / MANIFEST_NAME, "rb") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_int(value: Any) -> int:
    """A manifest number, or 0 for anything that is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _manifest_paths(manifest: Dict[str, Any]) -> List[str]:
    """The ``entries[].path`` values of a manifest, skipping anything malformed."""
    raw = manifest.get("entries")
    if not isinstance(raw, list):
        return []
    return [entry["path"] for entry in raw if isinstance(entry, dict) and isinstance(entry.get("path"), str)]


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


def _resolve_project_dir(project: str, workspace_root: Path, *, action: str = "clean") -> Path:
    """Validated project directory, or raise ``click.ClickException`` explaining why not.

    Shared by the clean and the restore side, because the target of both is the same thing —
    a project working tree — and every refusal below applies to putting files back as much as
    to taking them away.

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
            f"Refusing to {action} the workspace root ({workspace_root}).\n"
            "Its ignored set is the shared .venv, the .env every config reads $DATA_ROOT from, "
            "and the generated uv.toml / pyrightconfig.json.\n"
            "Name one project instead."
        )

    known = known_projects(workspace_root)
    if project not in known:
        raise click.ClickException(f"Unknown project '{project}'. Known projects: {', '.join(known)}")

    project_dir = project_path(workspace_root, project)
    if not (project_dir / ".git").exists():
        raise click.ClickException(
            f"{project_dir} is not a git repository — nothing to {action}.\n"
            "A project on a `github` or `pypi` source has no checkout; see: aisland workspace configure source"
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
            + f" — refusing to {action}, since git would report the enclosing repository's ignored files."
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
    # Both forms are printed on purpose: the command is the one to reach for, and the `cp` is
    # the one that still works when this code is the thing that broke.
    click.echo(f"Restore with:  aisland backups restore {project}")
    click.echo(f"          or:  cp -R {destination / PAYLOAD_DIRNAME}/. {project_dir}/")
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
    retention sweep later reports as freed, and `entries[].path` is what a restore puts back
    — see :func:`read_manifest`.
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


# --- the store: listing and restoring ------------------------------------------------------


def select_backup(store: Path, project: str, stamp: Optional[str] = None) -> Backup:
    """The backup to restore from: ``stamp`` if named, else ``project``'s most recent one."""
    candidates = [backup for backup in read_backups(store) if backup.project == project]
    if not candidates:
        raise click.ClickException(f"No backups for '{project}' under {store}.\nTake one with: aisland clean {project}")
    if stamp is None:
        return candidates[-1]  # read_backups is oldest-first
    for backup in candidates:
        if backup.path.name == stamp:
            return backup
    available = ", ".join(backup.path.name for backup in candidates)
    raise click.ClickException(f"No backup '{stamp}' for '{project}'. Available: {available}")


def restorable_entries(backup: Backup) -> List[str]:
    """Project-relative paths ``backup`` can put back.

    The manifest is authoritative because it records the granularity that was MOVED: a backup
    of ``data/jenkins`` merges back into a ``data/`` the project has since re-created, where a
    naive top-level walk would try to restore ``data`` itself and collide. An interrupted
    clean leaves no manifest; the payload's own top level is then the best available answer —
    the same granularity ``cp -R files/. <project>/`` uses.
    """
    if backup.entries:
        return sorted(backup.entries)
    payload = backup.path / PAYLOAD_DIRNAME
    if not payload.is_dir():
        return []
    return sorted(child.name for child in payload.iterdir())


def _safe_rel(rel: str) -> Path:
    """A manifest path, refused if it would write outside the project.

    The manifest is a FILE on disk that this code did not necessarily write — it can be
    hand-edited or corrupt — so the paths in it are validated rather than trusted. Checking
    the relative path itself (absolute, or containing ``..``) rather than the resolved
    destination keeps a symlinked project directory from being misread as an escape.
    """
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise click.ClickException(f"Refusing to restore '{rel}': it escapes the project directory.")
    return candidate


def _same_node(left: Path, right: Path) -> bool:
    """Whether two paths are the same KIND of thing holding the same bytes."""
    if left.is_symlink() or right.is_symlink():
        return left.is_symlink() and right.is_symlink() and os.readlink(left) == os.readlink(right)
    if left.is_dir() or right.is_dir():
        return left.is_dir() and right.is_dir()
    return filecmp.cmp(str(left), str(right), shallow=False)


def _same_content(source: Path, destination: Path) -> bool:
    """Whether the project's ``destination`` already holds exactly what ``source`` does.

    Two stages, because this answer decides whether a backup copy may be dropped and so has
    to be TRUE rather than likely: a size + file-count comparison rejects the common case (a
    cache or `.venv` that rebuilt itself) without reading a byte, and only a survivor of that
    is compared byte for byte.

    ``mtime`` is deliberately NOT part of the test, tempting as the rsync-style quick check
    is. The case this exists for is a path the operator already put back by hand, and a plain
    ``cp -R`` gives every copied file a NEW mtime — the quick check would call an identical
    tree different and the redundant copy would sit in the store until it expired.

    An unreadable file anywhere means "not the same": the conservative answer keeps the
    backup copy, and a genuine collision is reported instead of silently dropped.
    """
    try:
        if _measure(source)[:2] != _measure(destination)[:2]:
            return False
        if not _same_node(source, destination):
            return False
        if not source.is_dir() or source.is_symlink():
            return True
        below_source = {child.relative_to(source) for child in source.rglob("*")}
        below_destination = {child.relative_to(destination) for child in destination.rglob("*")}
        if below_source != below_destination:
            return False
        return all(_same_node(source / rel, destination / rel) for rel in below_source)
    except OSError:
        return False


def _prune_empty_parents(path: Path, stop: Path) -> None:
    """Remove directories left empty above ``path``, up to but excluding ``stop``."""
    parent = path.parent
    while parent != stop and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _forget_restored(backup: Backup, restored: Set[str]) -> None:
    """Drop the restored entries from the manifest, or delete a backup that is now empty.

    A store that still advertises content it no longer holds would make the next
    ``aisland backups list`` lie and the next restore fail halfway, so the manifest is
    rewritten in the same breath as the move.
    """
    payload = backup.path / PAYLOAD_DIRNAME
    if not payload.is_dir() or not any(payload.iterdir()):
        shutil.rmtree(backup.path)
        parent = backup.path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
        return

    manifest = read_manifest(backup.path)
    if not manifest:
        return
    kept = [
        entry
        for entry in manifest.get("entries", [])
        if not (isinstance(entry, dict) and entry.get("path") in restored)
    ]
    manifest["entries"] = kept
    manifest["bytes"] = sum(_as_int(entry.get("bytes")) for entry in kept)
    manifest["files"] = sum(_as_int(entry.get("files")) for entry in kept)
    (backup.path / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")


def restore_backup(
    project: str,
    *,
    stamp: Optional[str] = None,
    only: Sequence[str] = (),
    force: bool = False,
    dry_run: bool = False,
    assume_yes: bool = False,
    workspace_root: Optional[Path] = None,
    data_dir: Path = DATA_DIR,
) -> int:
    """Move a backup's content back into ``project``.

    Moves rather than copies, for the same reason :func:`clean_project` does: the disk was
    full enough to run a clean, and a copy of the payload would need the space twice. The
    backup is consumed as it is restored — its manifest is rewritten, and a backup with
    nothing left in it is removed.

    Args:
        project: Workspace project name.
        stamp: Backup directory name; the most recent backup when omitted.
        only: Restore just these project-relative paths (all of them when empty).
        force: Replace a path that already exists in the project instead of skipping it.
        dry_run: Report the plan; change nothing on disk.
        assume_yes: Skip the confirmation `--force` would otherwise ask for.
        workspace_root: Workspace root; discovered when omitted.
        data_dir: aisland's data directory — the backup root's parent.

    Returns:
        Process exit code: 0 when everything asked for was restored, 1 when anything was
        skipped, missing or failed — a partial restore must be visible to a script.
    """
    root = workspace_root or get_workspace_root()
    project_dir = _resolve_project_dir(project, root, action="restore")
    backup = select_backup(backup_root(data_dir), project, stamp)

    available = restorable_entries(backup)
    if only:
        unknown = [rel for rel in only if rel not in available]
        if unknown:
            raise click.ClickException(
                f"{backup.path.name} holds no entry {', '.join(repr(rel) for rel in unknown)}.\n"
                f"It holds: {', '.join(available) or '(nothing)'}"
            )
        available = [rel for rel in available if rel in set(only)]

    payload = backup.path / PAYLOAD_DIRNAME
    pending: List[Tuple[str, Path, Path]] = []  # (rel, source, destination)
    duplicate: List[str] = []  # the project already holds this, byte for byte
    occupied: List[str] = []  # the project holds something ELSE under this path
    missing: List[str] = []
    for rel in available:
        source = payload / _safe_rel(rel)
        destination = project_dir / _safe_rel(rel)
        if not source.exists() and not source.is_symlink():
            missing.append(rel)
        elif not destination.exists() and not destination.is_symlink():
            pending.append((rel, source, destination))
        elif _same_content(source, destination):
            duplicate.append(rel)  # nothing to restore; the backup's copy is redundant
        elif force:
            pending.append((rel, source, destination))
        else:
            occupied.append(rel)

    click.echo(click.style(f"Restoring {backup.path.name} into {project} ({project_dir})", bold=True))
    for rel, _source, destination in pending:
        verb = "replace" if destination.exists() or destination.is_symlink() else "restore"
        click.echo(f"  {verb:<8} {rel}")
    for rel in duplicate:
        click.echo(f"  {'already':<8} {rel}  (identical in the project — dropping the backup's copy)")
    for rel in occupied:
        click.echo(f"  {'skip':<8} {rel}  (the project has a DIFFERENT one — use --force to replace it)")
    for rel in missing:
        click.echo(click.style(f"  {'gone':<8} {rel}  (not in the payload)", fg="red"), err=True)

    if dry_run:
        click.echo("\nDry run — nothing was restored.")
        return 1 if occupied or missing else 0

    replacing = [rel for rel, _source, destination in pending if destination.exists() or destination.is_symlink()]
    if replacing and not assume_yes:
        if not click.confirm(f"Replace {len(replacing)} existing path(s) in {project}?"):
            click.echo("Nothing was restored.")
            return 1

    restored: Set[str] = set()
    failed: List[Tuple[str, str]] = []
    for rel, source, destination in pending:
        try:
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            elif destination.exists() or destination.is_symlink():
                destination.unlink()
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        except (OSError, shutil.Error) as exc:
            failed.append((rel, str(exc)))
            click.echo(click.style(f"  ! could not restore {rel}: {exc}", fg="red"), err=True)
        else:
            restored.add(rel)
            _prune_empty_parents(source, payload)

    moved = len(restored)
    # A proven-identical copy is dropped rather than moved: the content IS in the project, so
    # keeping the duplicate would make the next `list` count it as recoverable content and
    # make every later restore re-run the comparison that proved it redundant.
    for rel in duplicate:
        redundant = payload / _safe_rel(rel)
        try:
            if redundant.is_dir() and not redundant.is_symlink():
                shutil.rmtree(redundant)
            else:
                redundant.unlink()
        except OSError as exc:  # pragma: no cover - the compare just read it successfully
            failed.append((rel, str(exc)))
        else:
            restored.add(rel)
            _prune_empty_parents(redundant, payload)

    if restored:
        _forget_restored(backup, restored)
    click.echo(f"\nRestored {moved} entries into {project_dir}")
    if duplicate:
        click.echo(f"{len(duplicate)} were already there identically; the backup's copies were dropped.")
    if occupied:
        click.echo(f"{len(occupied)} left in the backup because the project has a different one (--force replaces).")
    return 1 if occupied or missing or failed else 0


def list_store(
    project: Optional[str] = None,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    data_dir: Path = DATA_DIR,
    now: Optional[datetime] = None,
) -> int:
    """Print every backup in the store, newest first, with when it expires.

    Args:
        project: Show only this project's backups (all of them when omitted).
        keep_days: Retention window used to compute the `EXPIRES` column.
        data_dir: aisland's data directory — the backup root's parent.
        now: Reference time for ages; defaults to now.

    Returns:
        Always 0 — an empty store is a state, not a failure.
    """
    now = now or datetime.now()
    store = backup_root(data_dir)
    backups = [item for item in read_backups(store) if project is None or item.project == project]
    if not backups:
        scope = f" for '{project}'" if project else ""
        click.echo(f"No backups{scope} under {store}.")
        click.echo(f"Take one with: aisland clean {project or '<project>'}")
        return 0

    click.echo(
        click.style(f"  {'PROJECT':<22} {'BACKUP':<17} {'AGE':>5} {'SIZE':>10} {'ENTRIES':>8}  EXPIRES", bold=True)
    )
    for backup in reversed(backups):  # newest first — the one a restore defaults to is on top
        age_days = (now - backup.created).days
        remaining = keep_days - age_days
        expires = "next run" if remaining <= 0 else f"in {remaining}d"
        entries = str(len(backup.entries)) if backup.entries else "?"
        click.echo(
            f"  {backup.project:<22} {backup.path.name:<17} {str(age_days) + 'd':>5} "
            f"{_human(backup.bytes):>10} {entries:>8}  {expires}"
        )
    click.echo(f"\n  {len(backups)} backup(s), {_human(sum(item.bytes for item in backups))} under {store}")
    click.echo(f"  Restore the newest:  aisland backups restore {backups[-1].project}")
    return 0


def prune_store(
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    dry_run: bool = False,
    assume_yes: bool = False,
    data_dir: Path = DATA_DIR,
    now: Optional[datetime] = None,
) -> int:
    """Apply the retention window to the whole store, without cleaning anything.

    The same sweep :func:`clean_project` runs, reachable on its own — a workspace nobody is
    currently cleaning would otherwise hold expired backups until the next clean of ANY
    project. It confirms first, because standalone it is the only thing happening: in a clean
    the sweep is bounded by the fresh backup being taken right after it, while here a
    mistyped `--keep-days 0` would empty the store outright.

    Args:
        keep_days: Backups older than this many days are deleted.
        dry_run: Report what would go; change nothing.
        assume_yes: Skip the confirmation prompt.
        data_dir: aisland's data directory — the backup root's parent.
        now: Reference time for the retention window; defaults to now.

    Returns:
        Always 0 — an empty sweep and a declined one are both outcomes, not failures.
    """
    now = now or datetime.now()
    store = backup_root(data_dir)
    expired = prune_backups(store, keep_days, now=now, dry_run=True)
    if not expired:
        click.echo(f"Nothing older than {keep_days} days under {store}.")
        return 0

    _report_pruned(expired, keep_days, dry_run=True)
    freed = sum(backup.bytes for backup in expired)
    if dry_run:
        click.echo("\nDry run — nothing was deleted.")
        return 0
    if not assume_yes and not click.confirm(f"Delete {len(expired)} backup(s) ({_human(freed)})?"):
        click.echo("Nothing was deleted.")
        return 0

    prune_backups(store, keep_days, now=now)
    click.echo(f"Deleted {len(expired)} backup(s), freed {_human(freed)}.")
    return 0
