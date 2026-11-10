"""The backup store `aisland clean` fills, its retention and the restore: `aisland backups list|prune|restore`.

One backup is ``<store>/<project>/<stamp>/``: the paths a clean moved, under ``files/`` at their places in the
project, and a ``manifest.json`` recording what landed there (:mod:`aisland.clean` takes it). This module reads the
store, applies the retention window — the sweep every clean runs, and ``aisland backups prune`` on its own — and puts a
backup back into its project, consuming it as it goes.

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
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import click

from aisland import atomic, console, gitcmd, paths
from aisland.errors import AislandError
from aisland.workspace.model import Workspace
from aisland.workspace.projects import get_workspace_root
from aisland.workspace.sources import project_path

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


def backup_root(data_dir: Optional[Path] = None) -> Path:
    """Directory holding every backup, for every project: in ``data_dir``, else aisland's data folder."""
    return (data_dir or paths.data_dir()) / BACKUP_DIRNAME


# --- sizes --------------------------------------------------------------------------------


def measure(path: Path) -> Tuple[int, int, bool]:
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


def human(size: int) -> str:
    """Byte count as a short human-readable string."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover - unreachable, the loop returns at TiB


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


def read_manifest(stamp_dir: Path) -> Dict[str, Any]:  # Any: a backup's JSON manifest, each value checked on use
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


def _as_int(value: object) -> int:
    """A manifest number, or 0 for anything that is not one."""
    if not isinstance(value, (int, float, str)):  # the JSON values int() takes
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _manifest_paths(manifest: Mapping[str, object]) -> List[str]:
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


def report_pruned(expired: List[Backup], keep_days: int, *, dry_run: bool) -> None:
    """Print the retention sweep's outcome."""
    if not expired:
        return
    verb = "Would delete" if dry_run else "Deleted"
    freed = sum(backup.bytes for backup in expired)
    console.echo(f"\n{verb} {len(expired)} backup(s) older than {keep_days} days ({human(freed)}):")
    for backup in expired:
        console.echo(f"  {backup.path}")


# --- the project a backup is taken from ---------------------------------------------------


def resolve_project_dir(project: str, workspace_root: Path, *, action: str = "clean") -> Path:
    """Validated project directory, or raise an :class:`AislandError` explaining why not.

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
        raise AislandError(
            f"Refusing to {action} the workspace root ({workspace_root}).\n"
            "Its ignored set is the shared .venv, the .env every config reads $DATA_ROOT from, "
            "and the generated uv.toml / pyrightconfig.json.\n"
            "Name one project instead."
        )

    known = Workspace(workspace_root).known_names
    if project not in known:
        raise AislandError(f"Unknown project '{project}'. Known projects: {', '.join(known)}")

    project_dir = project_path(workspace_root, project)
    if not (project_dir / ".git").exists():
        raise AislandError(
            f"{project_dir} is not a git repository — nothing to {action}.\n"
            "A project on a `github` or `pypi` source has no checkout; see: aisland workspace configure source"
        )

    found = gitcmd.read(project_dir, "rev-parse", "--show-toplevel")
    toplevel = Path(found) if found else None
    if toplevel is None or toplevel.resolve() != project_dir.resolve():
        raise AislandError(
            f"{project_dir} is not the root of its git repository"
            + (f" (that is {toplevel})" if toplevel else "")
            + f" — refusing to {action}, since git would report the enclosing repository's ignored files."
        )
    return project_dir


# --- the store: listing and restoring -----------------------------------------------------


def select_backup(store: Path, project: str, stamp: Optional[str] = None) -> Backup:
    """The backup to restore from: ``stamp`` if named, else ``project``'s most recent one."""
    candidates = [backup for backup in read_backups(store) if backup.project == project]
    if not candidates:
        raise AislandError(f"No backups for '{project}' under {store}.\nTake one with: aisland clean {project}")
    if stamp is None:
        return candidates[-1]  # read_backups is oldest-first
    for backup in candidates:
        if backup.path.name == stamp:
            return backup
    available = ", ".join(backup.path.name for backup in candidates)
    raise AislandError(f"No backup '{stamp}' for '{project}'. Available: {available}")


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
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:  # "" or "." is the project itself
        raise AislandError(f"Refusing to restore '{rel}': it escapes the project directory.")
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
        if measure(source)[:2] != measure(destination)[:2]:
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
    atomic.write_text((backup.path / MANIFEST_NAME), json.dumps(manifest, indent=2) + "\n")


@dataclass(frozen=True)
class _RestorePlan:
    """What putting a backup back would do, entry by entry (project-relative paths)."""

    #: ``(rel, source, destination)`` to move into the project.
    pending: Tuple[Tuple[str, Path, Path], ...]
    #: The project already holds this, byte for byte: the backup's copy is dropped.
    duplicate: Tuple[str, ...]
    #: The project holds something ELSE under this path, and ``--force`` was not given.
    occupied: Tuple[str, ...]
    #: The manifest lists it, the payload no longer holds it.
    missing: Tuple[str, ...]

    @property
    def replacing(self) -> List[str]:
        """The pending entries that would replace something the project holds now."""
        return [rel for rel, _source, destination in self.pending if _exists(destination)]


def _exists(path: Path) -> bool:
    """Whether anything is at ``path`` — a dangling link included."""
    return path.exists() or path.is_symlink()


def _wanted_entries(backup: Backup, only: Sequence[str]) -> List[str]:
    """The entries to restore: all the backup holds, or the ``only`` ones, refused when it does not hold one."""
    available = restorable_entries(backup)
    if not only:
        return available
    unknown = [rel for rel in only if rel not in available]
    if unknown:
        raise AislandError(
            f"{backup.path.name} holds no entry {', '.join(repr(rel) for rel in unknown)}.\n"
            f"It holds: {', '.join(available) or '(nothing)'}"
        )
    return [rel for rel in available if rel in set(only)]


def _plan_restore(payload: Path, project_dir: Path, wanted: Sequence[str], *, force: bool) -> _RestorePlan:
    """Sort ``wanted`` into what moves, what is already there, what is in the way and what is gone."""
    pending: List[Tuple[str, Path, Path]] = []
    duplicate: List[str] = []
    occupied: List[str] = []
    missing: List[str] = []
    for rel in wanted:
        source = payload / _safe_rel(rel)
        destination = project_dir / _safe_rel(rel)
        if not _exists(source):
            missing.append(rel)
        elif not _exists(destination):
            pending.append((rel, source, destination))
        elif _same_content(source, destination):
            duplicate.append(rel)  # nothing to restore; the backup's copy is redundant
        elif force:
            pending.append((rel, source, destination))
        else:
            occupied.append(rel)
    return _RestorePlan(tuple(pending), tuple(duplicate), tuple(occupied), tuple(missing))


def _announce(plan: _RestorePlan, backup: Backup, project: str, project_dir: Path) -> None:
    """Say what the restore will do, one line per entry."""
    console.echo(click.style(f"Restoring {backup.path.name} into {project} ({project_dir})", bold=True))
    for rel, _source, destination in plan.pending:
        verb = "replace" if _exists(destination) else "restore"
        console.echo(f"  {verb:<8} {rel}")
    for rel in plan.duplicate:
        console.echo(f"  {'already':<8} {rel}  (identical in the project — dropping the backup's copy)")
    for rel in plan.occupied:
        console.echo(f"  {'skip':<8} {rel}  (the project has a DIFFERENT one — use --force to replace it)")
    for rel in plan.missing:
        console.echo(click.style(f"  {'gone':<8} {rel}  (not in the payload)", fg="red"), err=True)


def _remove(path: Path) -> None:
    """Delete what is at ``path``: a folder with all it holds, else the file or link itself."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _move_into_place(source: Path, destination: Path) -> None:
    """Move ``source`` to ``destination``. A copy already there (``--force``) is first moved aside beside it, and
    deleted only once the move has succeeded; a move that fails puts it back, so the project never loses both."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not _exists(destination):
        shutil.move(str(source), str(destination))
        return
    aside = Path(tempfile.mkdtemp(prefix=".aisland-restore-", dir=destination.parent)) / destination.name
    os.rename(destination, aside)  # one folder, one file system: whole or not at all
    try:
        shutil.move(str(source), str(destination))
    except BaseException:
        if _exists(destination):  # half a copy from another file system
            _remove(destination)
        os.rename(aside, destination)
        aside.parent.rmdir()
        raise
    _remove(aside)
    aside.parent.rmdir()


def _move_back(plan: _RestorePlan, payload: Path) -> Tuple[Set[str], List[Tuple[str, str]]]:
    """Move each pending entry into the project: the entries restored, and ``(rel, reason)`` of the ones that failed."""
    restored: Set[str] = set()
    failed: List[Tuple[str, str]] = []
    for rel, source, destination in plan.pending:
        try:
            _move_into_place(source, destination)
        except (OSError, shutil.Error) as exc:
            failed.append((rel, str(exc)))
            console.echo(click.style(f"  ! could not restore {rel}: {exc}", fg="red"), err=True)
        else:
            restored.add(rel)
            _prune_empty_parents(source, payload)
    return restored, failed


def _drop_duplicates(plan: _RestorePlan, payload: Path, restored: Set[str], failed: List[Tuple[str, str]]) -> None:
    """Delete the backup's copy of every entry the project already holds, byte for byte.

    A proven-identical copy is dropped rather than moved: the content IS in the project, so keeping the duplicate
    would make the next ``list`` count it as recoverable content and make every later restore re-run the comparison
    that proved it redundant.
    """
    for rel in plan.duplicate:
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


def restore_backup(
    project: str,
    *,
    stamp: Optional[str] = None,
    only: Sequence[str] = (),
    force: bool = False,
    dry_run: bool = False,
    assume_yes: bool = False,
    workspace_root: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> int:
    """Move a backup's content back into ``project``.

    Moves rather than copies, for the same reason :func:`aisland.clean.clean_project` does: the disk was
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
    project_dir = resolve_project_dir(project, root, action="restore")
    backup = select_backup(backup_root(data_dir), project, stamp)
    payload = backup.path / PAYLOAD_DIRNAME
    plan = _plan_restore(payload, project_dir, _wanted_entries(backup, only), force=force)

    _announce(plan, backup, project, project_dir)
    if dry_run:
        console.echo("\nDry run — nothing was restored.")
        return 1 if plan.occupied or plan.missing else 0

    replacing = plan.replacing
    if replacing and not assume_yes and not console.confirm(f"Replace {len(replacing)} existing path(s) in {project}?"):
        console.echo("Nothing was restored.")
        return 1

    restored, failed = _move_back(plan, payload)
    moved = len(restored)
    _drop_duplicates(plan, payload, restored, failed)
    if restored:
        _forget_restored(backup, restored)

    console.echo(f"\nRestored {moved} entries into {project_dir}")
    if plan.duplicate:
        console.echo(f"{len(plan.duplicate)} were already there identically; the backup's copies were dropped.")
    if plan.occupied:
        console.echo(
            f"{len(plan.occupied)} left in the backup because the project has a different one (--force replaces)."
        )
    return 1 if plan.occupied or plan.missing or failed else 0


def list_store(
    project: Optional[str] = None,
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    data_dir: Optional[Path] = None,
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
        console.echo(f"No backups{scope} under {store}.")
        console.echo(f"Take one with: aisland clean {project or '<project>'}")
        return 0

    console.echo(
        click.style(f"  {'PROJECT':<22} {'BACKUP':<17} {'AGE':>5} {'SIZE':>10} {'ENTRIES':>8}  EXPIRES", bold=True)
    )
    for backup in reversed(backups):  # newest first — the one a restore defaults to is on top
        age_days = (now - backup.created).days
        remaining = keep_days - age_days
        expires = "next run" if remaining <= 0 else f"in {remaining}d"
        entries = str(len(backup.entries)) if backup.entries else "?"
        console.echo(
            f"  {backup.project:<22} {backup.path.name:<17} {str(age_days) + 'd':>5} "
            f"{human(backup.bytes):>10} {entries:>8}  {expires}"
        )
    console.echo(f"\n  {len(backups)} backup(s), {human(sum(item.bytes for item in backups))} under {store}")
    console.echo(f"  Restore the newest:  aisland backups restore {backups[-1].project}")
    return 0


def prune_store(
    *,
    keep_days: int = DEFAULT_KEEP_DAYS,
    dry_run: bool = False,
    assume_yes: bool = False,
    data_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> int:
    """Apply the retention window to the whole store, without cleaning anything.

    The same sweep :func:`aisland.clean.clean_project` runs, reachable on its own — a workspace nobody is
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
        console.echo(f"Nothing older than {keep_days} days under {store}.")
        return 0

    report_pruned(expired, keep_days, dry_run=True)
    freed = sum(backup.bytes for backup in expired)
    if dry_run:
        console.echo("\nDry run — nothing was deleted.")
        return 0
    if not assume_yes and not console.confirm(f"Delete {len(expired)} backup(s) ({human(freed)})?"):
        console.echo("Nothing was deleted.")
        return 0

    prune_backups(store, keep_days, now=now)
    console.echo(f"Deleted {len(expired)} backup(s), freed {human(freed)}.")
    return 0
