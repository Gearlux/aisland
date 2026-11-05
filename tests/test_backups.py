"""Tests for `aisland backups` (aisland/backups.py): the store `aisland clean` fills.

- the retention sweep deletes only what this tool created, reading sizes from the manifest;
- a restore puts a backup back and consumes it: a different copy in the project is skipped
  (exit 1, `--force` replaces it), an identical one — byte for byte, never by size and mtime —
  is dropped from the store;
- a manifest is a file on disk this code did not necessarily write: its paths are validated.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import List, Optional

import click
import pytest
from click.testing import CliRunner

from aisland import backups, clean, console
from aisland.cli import main
from aisland.cli import toplevel as cli_toplevel
from tests.backup_store import NOW, make_project, make_workspace, run_clean, seed_backup, stamp, tmp_store


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A workspace root holding one project and an aisland-style data directory."""
    return make_workspace(tmp_path)


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #


def test_prune_deletes_expired_backups_and_keeps_the_rest(workspace: Path) -> None:
    old = seed_backup(workspace, "proj", 8, size=2048)
    fresh = seed_backup(workspace, "proj", 2)

    deleted = backups.prune_backups(tmp_store(workspace), backups.DEFAULT_KEEP_DAYS, now=NOW)

    assert [backup.path for backup in deleted] == [old]
    assert deleted[0].bytes == 2048  # read from the manifest, not re-walked
    assert not old.exists()
    assert fresh.exists()


def test_prune_never_deletes_what_this_tool_did_not_create(workspace: Path) -> None:
    stranger = tmp_store(workspace) / "proj" / "my-notes"
    stranger.mkdir(parents=True)
    seed_backup(workspace, "proj", 30)

    backups.prune_backups(tmp_store(workspace), 7, now=NOW)

    assert stranger.exists()
    assert backups.read_backups(tmp_store(workspace)) == []


def test_prune_removes_a_project_directory_it_emptied(workspace: Path) -> None:
    seed_backup(workspace, "proj", 9)

    backups.prune_backups(tmp_store(workspace), 7, now=NOW)

    assert not (tmp_store(workspace) / "proj").exists()


def test_prune_is_a_no_op_before_the_first_run(workspace: Path) -> None:
    assert backups.prune_backups(tmp_store(workspace), 7, now=NOW) == []


def test_prune_dry_run_reports_without_deleting(workspace: Path) -> None:
    old = seed_backup(workspace, "proj", 8)

    assert [backup.path for backup in backups.prune_backups(tmp_store(workspace), 7, now=NOW, dry_run=True)] == [old]
    assert old.exists()


def test_a_backup_without_a_manifest_still_expires(workspace: Path) -> None:
    """An interrupted run leaves no manifest; the DIRECTORY NAME is what retention reads."""
    orphan = tmp_store(workspace) / "proj" / stamp(9)
    orphan.mkdir(parents=True)

    deleted = backups.prune_backups(tmp_store(workspace), 7, now=NOW)

    assert [backup.path for backup in deleted] == [orphan]
    assert deleted[0].bytes == 0


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KiB"), (5 * 1024**2, "5.0 MiB"), (3 * 1024**4, "3.0 TiB")],
)
def test_human_readable_sizes(size: int, expected: str) -> None:
    assert backups.human(size) == expected


# --------------------------------------------------------------------------- #
# the store: selecting a backup
# --------------------------------------------------------------------------- #


def _restore(workspace: Path, project: str = "proj", **kwargs: object) -> int:
    """`restore_backup` bound to the test workspace."""
    return backups.restore_backup(
        project,
        workspace_root=workspace,
        data_dir=workspace / "data",
        assume_yes=True,
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_newest_backup_is_the_one_a_restore_defaults_to(workspace: Path) -> None:
    seed_backup(workspace, "proj", 3)
    newest = seed_backup(workspace, "proj", 1)

    assert backups.select_backup(tmp_store(workspace), "proj").path == newest


def test_a_named_stamp_wins_over_the_newest(workspace: Path) -> None:
    older = seed_backup(workspace, "proj", 3)
    seed_backup(workspace, "proj", 1)

    assert backups.select_backup(tmp_store(workspace), "proj", older.name).path == older


def test_an_unknown_stamp_lists_what_is_there(workspace: Path) -> None:
    seed_backup(workspace, "proj", 3)

    with pytest.raises(click.ClickException, match=f"No backup 'nope' for 'proj'. Available: {stamp(3)}"):
        backups.select_backup(tmp_store(workspace), "proj", "nope")


def test_a_project_with_no_backups_is_told_how_to_take_one(workspace: Path) -> None:
    with pytest.raises(click.ClickException, match="Take one with: aisland clean proj"):
        backups.select_backup(tmp_store(workspace), "proj")


def test_restorable_entries_come_from_the_manifest(workspace: Path) -> None:
    """The manifest's granularity is what moved — `data/jenkins`, not `data`."""
    run_clean(workspace)
    backup = backups.select_backup(tmp_store(workspace), "proj")

    assert backups.restorable_entries(backup) == ["build", "run.log"]


def test_restorable_entries_fall_back_to_the_payload(workspace: Path) -> None:
    """An interrupted clean leaves no manifest; the payload is still restorable."""
    backup_dir = seed_backup(workspace, "proj", 1)
    (backup_dir / backups.MANIFEST_NAME).unlink()
    (backup_dir / backups.PAYLOAD_DIRNAME / "build").mkdir()
    (backup_dir / backups.PAYLOAD_DIRNAME / "run.log").write_text("noise\n")

    backup = backups.select_backup(tmp_store(workspace), "proj")
    assert backups.restorable_entries(backup) == ["build", "run.log"]


def test_a_backup_with_no_payload_directory_has_nothing_to_restore(workspace: Path) -> None:
    backup_dir = seed_backup(workspace, "proj", 1)
    shutil.rmtree(backup_dir / backups.PAYLOAD_DIRNAME)

    backup = backups.select_backup(tmp_store(workspace), "proj")
    assert backups.restorable_entries(backup) == []
    assert _restore(workspace) == 0


def test_a_manifest_less_backup_restores_entry_by_entry(workspace: Path) -> None:
    """The payload fallback must survive a PARTIAL restore — there is no manifest to rewrite."""
    backup_dir = seed_backup(workspace, "proj", 1)
    (backup_dir / backups.MANIFEST_NAME).unlink()
    (backup_dir / backups.PAYLOAD_DIRNAME / "run.log").write_text("noise\n")
    (backup_dir / backups.PAYLOAD_DIRNAME / "other.log").write_text("more\n")
    (workspace / "proj" / "run.log").unlink()

    assert _restore(workspace, only=["run.log"]) == 0
    assert (workspace / "proj" / "run.log").read_text() == "noise\n"
    assert backups.restorable_entries(backups.select_backup(tmp_store(workspace), "proj")) == ["other.log"]


def test_a_manifest_that_is_not_a_mapping_reads_as_empty(workspace: Path) -> None:
    backup_dir = seed_backup(workspace, "proj", 1)
    (backup_dir / backups.MANIFEST_NAME).write_text("[1, 2, 3]")

    assert backups.read_manifest(backup_dir) == {}


def test_malformed_manifest_numbers_and_entries_are_ignored(workspace: Path) -> None:
    backup_dir = seed_backup(workspace, "proj", 1)
    (backup_dir / backups.MANIFEST_NAME).write_text(
        json.dumps({"bytes": "lots", "files": None, "entries": ["build", {"no_path": 1}, {"path": "run.log"}]})
    )

    backup = backups.select_backup(tmp_store(workspace), "proj")
    assert (backup.bytes, backup.files, backup.entries) == (0, 0, ("run.log",))


# --------------------------------------------------------------------------- #
# the store: restoring
# --------------------------------------------------------------------------- #


def test_a_restore_puts_everything_back_and_consumes_the_backup(workspace: Path) -> None:
    run_clean(workspace)
    project = workspace / "proj"

    assert _restore(workspace) == 0
    assert (project / "build" / "nested" / "b.txt").read_text() == "bb\n"
    assert (project / "run.log").read_text() == "noise\n"
    assert backups.read_backups(tmp_store(workspace)) == []  # emptied, so removed
    assert not (tmp_store(workspace) / "proj").exists()


def test_a_restore_is_the_inverse_of_a_clean(workspace: Path) -> None:
    project = workspace / "proj"
    before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*") if ".git" not in path.parts)

    run_clean(workspace)
    _restore(workspace)

    after = sorted(path.relative_to(project).as_posix() for path in project.rglob("*") if ".git" not in path.parts)
    assert after == before


def test_restoring_one_entry_leaves_the_rest_in_the_backup(workspace: Path) -> None:
    run_clean(workspace)

    assert _restore(workspace, only=["run.log"]) == 0
    assert (workspace / "proj" / "run.log").exists()
    assert not (workspace / "proj" / "build").exists()

    backup = backups.select_backup(tmp_store(workspace), "proj")
    assert backup.entries == ("build",)  # the manifest no longer advertises what left
    assert backup.bytes == len("a\n") + len("bb\n")
    assert (backup.path / backups.PAYLOAD_DIRNAME / "build").is_dir()


def test_restoring_an_entry_the_backup_does_not_hold_is_refused(workspace: Path) -> None:
    run_clean(workspace)

    with pytest.raises(click.ClickException, match="holds no entry 'nope'"):
        _restore(workspace, only=["nope"])


def test_a_path_the_project_already_has_is_skipped_not_clobbered(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")

    assert _restore(workspace) == 1  # a partial restore must be visible to a script
    assert (workspace / "proj" / "run.log").read_text() == "rebuilt\n"
    assert (workspace / "proj" / "build").exists()  # the rest still came back
    assert backups.select_backup(tmp_store(workspace), "proj").entries == ("run.log",)


def test_force_replaces_what_the_project_already_has(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")

    assert _restore(workspace, force=True) == 0
    assert (workspace / "proj" / "run.log").read_text() == "noise\n"


def test_force_replaces_a_directory_too(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "build").mkdir()
    (workspace / "proj" / "build" / "stale.txt").write_text("stale\n")

    assert _restore(workspace, force=True) == 0
    assert not (workspace / "proj" / "build" / "stale.txt").exists()
    assert (workspace / "proj" / "build" / "nested" / "b.txt").read_text() == "bb\n"


def test_declining_the_force_confirmation_restores_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")
    monkeypatch.setattr(console, "confirm", lambda *args, **kwargs: False)

    exit_code = backups.restore_backup("proj", force=True, workspace_root=workspace, data_dir=workspace / "data")

    assert exit_code == 1
    assert (workspace / "proj" / "run.log").read_text() == "rebuilt\n"
    assert not (workspace / "proj" / "build").exists()


def test_a_nested_entry_merges_into_a_directory_that_came_back(tmp_path: Path) -> None:
    """`data/jenkins` restores into a `data/` the project re-created — the manifest's point."""
    project = make_project(tmp_path, "aisland", gitignore="data/\n")
    (project / "data" / "jenkins").mkdir(parents=True)
    (project / "data" / "jenkins" / "war").write_text("big\n")
    store = project / "data" / backups.BACKUP_DIRNAME
    store.mkdir(parents=True)

    clean.clean_project("aisland", workspace_root=tmp_path, data_dir=project / "data", assume_yes=True, now=NOW)
    (project / "data" / "mlflow").mkdir(parents=True)  # the project rebuilt a sibling meanwhile

    exit_code = backups.restore_backup("aisland", workspace_root=tmp_path, data_dir=project / "data", assume_yes=True)

    assert exit_code == 0
    assert (project / "data" / "jenkins" / "war").read_text() == "big\n"
    assert (project / "data" / "mlflow").is_dir()  # untouched


def test_an_entry_missing_from_the_payload_is_reported(workspace: Path) -> None:
    run_clean(workspace)
    backup = backups.select_backup(tmp_store(workspace), "proj")
    shutil.rmtree(backup.path / backups.PAYLOAD_DIRNAME / "build")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "run.log").exists()


def test_a_dry_run_restores_nothing(workspace: Path) -> None:
    run_clean(workspace)

    assert _restore(workspace, dry_run=True) == 0
    assert not (workspace / "proj" / "build").exists()
    assert backups.select_backup(tmp_store(workspace), "proj").entries == ("build", "run.log")


def test_a_dry_run_reports_a_collision_as_exit_1(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")

    assert _restore(workspace, dry_run=True) == 1


def test_an_entry_that_cannot_be_restored_is_reported(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_clean(workspace)
    real_move = shutil.move

    def _selective(src: str, dst: str) -> object:
        if src.endswith("run.log"):
            raise OSError("device or resource busy")
        return real_move(src, dst)

    monkeypatch.setattr(backups.shutil, "move", _selective)

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build").exists()
    assert not (workspace / "proj" / "run.log").exists()


@pytest.mark.parametrize("rel", ["../outside", "/etc/passwd", "build/../../outside"])
def test_a_manifest_path_that_escapes_the_project_is_refused(workspace: Path, rel: str) -> None:
    """The manifest is a file on disk this code did not necessarily write."""
    run_clean(workspace)
    backup = backups.select_backup(tmp_store(workspace), "proj")
    manifest = json.loads((backup.path / backups.MANIFEST_NAME).read_text())
    manifest["entries"] = [{"path": rel, "bytes": 1, "files": 1}]
    (backup.path / backups.MANIFEST_NAME).write_text(json.dumps(manifest))

    with pytest.raises(click.ClickException, match="escapes the project directory"):
        _restore(workspace)


def test_restoring_refuses_the_workspace_root(workspace: Path) -> None:
    with pytest.raises(click.ClickException, match="Refusing to restore the workspace root"):
        _restore(workspace, project=workspace.name)


# --------------------------------------------------------------------------- #
# the store: listing
# --------------------------------------------------------------------------- #


def _list(workspace: Path, capsys: pytest.CaptureFixture[str], project: Optional[str] = None, **kwargs: object) -> str:
    backups.list_store(project, data_dir=workspace / "data", now=NOW, **kwargs)  # type: ignore[arg-type]
    return str(capsys.readouterr().out)


def test_listing_an_empty_store_says_how_to_fill_it(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert backups.list_store(data_dir=workspace / "data", now=NOW) == 0
    capsys.readouterr()

    assert "aisland clean <project>" in _list(workspace, capsys)
    assert "aisland clean proj" in _list(workspace, capsys, "proj")


def test_listing_shows_the_newest_first(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_backup(workspace, "proj", 5)
    seed_backup(workspace, "proj", 1)

    rows = [line for line in _list(workspace, capsys).splitlines() if "proj " in line]
    assert stamp(1) in rows[0] and stamp(5) in rows[1]


def test_listing_filters_by_project(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_backup(workspace, "proj", 1)
    seed_backup(workspace, "other", 1)

    assert "other" not in _list(workspace, capsys, "proj")


def test_listing_shows_when_a_backup_expires(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_backup(workspace, "proj", 2)
    seed_backup(workspace, "other", 9)

    output = _list(workspace, capsys)
    assert "in 5d" in output  # 7 - 2
    assert "next run" in output  # already past the window


def test_listing_marks_an_unknown_entry_count(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A manifest-less backup has a size of 0 and no entry list — say so, don't invent 0."""
    orphan = tmp_store(workspace) / "proj" / stamp(1)
    orphan.mkdir(parents=True)

    assert "?" in _list(workspace, capsys)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_the_restore_cli_forwards_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[dict] = []

    def _record(project: str, **kwargs: object) -> int:
        seen.append({"project": project, **kwargs})
        return 0

    monkeypatch.setattr(cli_toplevel, "restore_backup", _record)
    result = CliRunner().invoke(
        main,
        ["backups", "restore", "proj", "--stamp", "20260804-120000", "--entry", "build", "--force", "--dry-run", "-y"],
    )

    assert result.exit_code == 0
    assert seen == [
        {
            "project": "proj",
            "stamp": "20260804-120000",
            "only": ("build",),
            "force": True,
            "dry_run": True,
            "assume_yes": True,
        }
    ]


def test_the_list_cli_forwards_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[tuple] = []

    def _record(project: object, **kwargs: object) -> int:
        seen.append((project, kwargs))
        return 0

    monkeypatch.setattr(cli_toplevel, "list_store", _record)
    CliRunner().invoke(main, ["backups", "list", "proj", "--keep-days", "3"])
    CliRunner().invoke(main, ["backups", "list"])

    assert seen == [("proj", {"keep_days": 3}), (None, {"keep_days": backups.DEFAULT_KEEP_DAYS})]


# --------------------------------------------------------------------------- #
# the store: an identical copy is not a collision
# --------------------------------------------------------------------------- #


def test_an_identical_file_is_not_a_collision(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")  # exactly what the backup holds

    assert _restore(workspace) == 0
    assert (workspace / "proj" / "run.log").read_text() == "noise\n"
    assert backups.read_backups(tmp_store(workspace)) == []  # nothing left worth keeping


def test_a_hand_copied_tree_is_recognised_despite_new_mtimes(workspace: Path) -> None:
    """The case this exists for: the operator already did `cp -R`, which restamps every file."""
    run_clean(workspace)
    backup = backups.select_backup(tmp_store(workspace), "proj")
    shutil.copytree(
        backup.path / backups.PAYLOAD_DIRNAME / "build",
        workspace / "proj" / "build",
        copy_function=shutil.copy,  # like `cp -R`: contents, not timestamps
    )
    assert (workspace / "proj" / "build" / "a.txt").stat().st_mtime != (
        backup.path / backups.PAYLOAD_DIRNAME / "build" / "a.txt"
    ).stat().st_mtime

    assert _restore(workspace) == 0
    assert (workspace / "proj" / "build" / "nested" / "b.txt").read_text() == "bb\n"


def test_the_same_size_with_different_bytes_is_still_a_collision(workspace: Path) -> None:
    """The size pre-check is an optimisation, never the answer."""
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("NOISE\n")  # same length, different content

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "run.log").read_text() == "NOISE\n"
    assert backups.select_backup(tmp_store(workspace), "proj").entries == ("run.log",)


def test_a_tree_with_an_extra_file_is_a_collision(workspace: Path) -> None:
    run_clean(workspace)
    backup = backups.select_backup(tmp_store(workspace), "proj")
    shutil.copytree(backup.path / backups.PAYLOAD_DIRNAME / "build", workspace / "proj" / "build")
    (workspace / "proj" / "build" / "extra.txt").write_text("x\n")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build" / "extra.txt").exists()


def test_a_renamed_file_is_a_collision_even_at_the_same_size(workspace: Path) -> None:
    """Same byte total and same file count, different names — only the path set catches it."""
    run_clean(workspace)
    backup = backups.select_backup(tmp_store(workspace), "proj")
    shutil.copytree(backup.path / backups.PAYLOAD_DIRNAME / "build", workspace / "proj" / "build")
    (workspace / "proj" / "build" / "nested" / "b.txt").rename(workspace / "proj" / "build" / "nested" / "z.txt")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build" / "nested" / "z.txt").exists()


def test_a_file_where_the_backup_holds_a_directory_is_a_collision(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "build").write_text("not a directory\n")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build").is_file()


def test_an_identical_symlink_is_not_a_collision(workspace: Path) -> None:
    project = workspace / "proj"
    project.joinpath("link.log").symlink_to("keep.py")
    run_clean(workspace)
    project.joinpath("link.log").symlink_to("keep.py")

    assert _restore(workspace) == 0


def test_a_symlink_to_a_different_target_is_a_collision(workspace: Path) -> None:
    project = workspace / "proj"
    project.joinpath("link.log").symlink_to("keep.py")
    run_clean(workspace)
    project.joinpath("link.log").symlink_to("elsewhere.py")

    assert _restore(workspace) == 1
    assert project.joinpath("link.log").readlink().name == "elsewhere.py"


def test_an_unreadable_file_counts_as_a_collision(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A comparison that cannot finish must keep the backup, not drop it."""
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")

    def _refuse(*args: object, **kwargs: object) -> bool:
        raise OSError("unreadable")

    monkeypatch.setattr(backups.filecmp, "cmp", _refuse)

    assert _restore(workspace) == 1
    assert backups.select_backup(tmp_store(workspace), "proj").entries == ("run.log",)


def test_force_does_not_bother_moving_an_identical_copy(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")
    before = (workspace / "proj" / "run.log").stat().st_mtime_ns

    assert _restore(workspace, force=True) == 0
    assert (workspace / "proj" / "run.log").stat().st_mtime_ns == before  # untouched


def test_a_duplicate_leaves_the_manifest_consistent(workspace: Path) -> None:
    run_clean(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")

    assert _restore(workspace, only=["run.log"]) == 0
    backup = backups.select_backup(tmp_store(workspace), "proj")
    assert backup.entries == ("build",)
    assert not (backup.path / backups.PAYLOAD_DIRNAME / "run.log").exists()


# --------------------------------------------------------------------------- #
# the store: pruning on its own
# --------------------------------------------------------------------------- #


def _prune(workspace: Path, **kwargs: object) -> int:
    kwargs.setdefault("assume_yes", True)
    return backups.prune_store(data_dir=workspace / "data", now=NOW, **kwargs)  # type: ignore[arg-type]


def test_pruning_an_unexpired_store_deletes_nothing(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fresh = seed_backup(workspace, "proj", 1)

    assert _prune(workspace) == 0
    assert "Nothing older than 7 days" in capsys.readouterr().out
    assert fresh.exists()


def test_pruning_deletes_what_the_window_expired(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = seed_backup(workspace, "proj", 8, size=2048)
    fresh = seed_backup(workspace, "proj", 1)

    assert _prune(workspace) == 0
    assert not old.exists()
    assert fresh.exists()
    assert "freed 2.0 KiB" in capsys.readouterr().out


def test_pruning_honours_a_tighter_window(workspace: Path) -> None:
    recent = seed_backup(workspace, "proj", 1)

    assert _prune(workspace, keep_days=0) == 0
    assert not recent.exists()


def test_a_prune_dry_run_deletes_nothing(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = seed_backup(workspace, "proj", 8)

    assert _prune(workspace, dry_run=True) == 0
    assert old.exists()
    assert "Dry run" in capsys.readouterr().out


def test_declining_the_prune_confirmation_deletes_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = seed_backup(workspace, "proj", 8)
    monkeypatch.setattr(console, "confirm", lambda *args, **kwargs: False)

    assert backups.prune_store(data_dir=workspace / "data", now=NOW) == 0
    assert old.exists()


def test_the_prune_cli_forwards_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[dict] = []

    def _record(**kwargs: object) -> int:
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(cli_toplevel, "prune_store", _record)
    CliRunner().invoke(main, ["backups", "prune", "--keep-days", "2", "--dry-run", "-y"])
    CliRunner().invoke(main, ["backups", "prune"])

    assert seen == [
        {"keep_days": 2, "dry_run": True, "assume_yes": True},
        {"keep_days": backups.DEFAULT_KEEP_DAYS, "dry_run": False, "assume_yes": False},
    ]
