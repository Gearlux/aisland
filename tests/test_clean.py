"""Tests for `aisland clean` (aisland/clean.py).

What is worth pinning here is everything that makes a DESTRUCTIVE command safe to run:

- the plan comes from git, and a fully-ignored directory stays ONE entry (a 30k-file
  virtualenv must not be walked file by file);
- tracked content is never touched, and what moves is recoverable from the backup;
- the backup store survives being inside the project being cleaned (`aisland clean aisland`
  — its `.gitignore` ignores `data/`, and the store lives at `data/backups`);
- the retention sweep deletes only what this tool created, and runs on EVERY invocation;
- every refusal a foot-gun deserves: the workspace root, an unknown name, a directory that
  is not a repository, and one that is not its repository's ROOT (where `git status` would
  report the enclosing repo's ignored set).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import click
import pytest
from click.testing import CliRunner

from aisland import clean
from aisland.cli import main
from aisland.cli import toplevel as cli_toplevel
from aisland.errors import AislandError

NOW = datetime(2026, 8, 4, 12, 0, 0)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _make_project(workspace: Path, name: str = "proj", *, gitignore: str = "build/\n*.log\n") -> Path:
    """A workspace project that is its own git repository, with tracked and ignored content."""
    project = workspace / name
    (project / "build" / "nested").mkdir(parents=True)
    project.joinpath("pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    project.joinpath(".gitignore").write_text(gitignore)
    project.joinpath("keep.py").write_text("# tracked\n")
    project.joinpath("run.log").write_text("noise\n")
    project.joinpath("build", "a.txt").write_text("a\n")
    project.joinpath("build", "nested", "b.txt").write_text("bb\n")

    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "add", ".")
    _git(project, "commit", "-qm", "initial")
    return project


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A workspace root holding one project and an aisland-style data directory."""
    _make_project(tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


def _run(workspace: Path, project: str = "proj", **kwargs: object) -> int:
    """`clean_project` bound to the test workspace, with a frozen clock."""
    kwargs.setdefault("now", NOW)
    return clean.clean_project(
        project,
        workspace_root=workspace,
        data_dir=workspace / "data",
        assume_yes=True,
        **kwargs,  # type: ignore[arg-type]
    )


def _stamp(offset_days: float) -> str:
    return (NOW - timedelta(days=offset_days)).strftime(clean.STAMP_FORMAT)


def _seed_backup(workspace: Path, project: str, offset_days: float, *, size: int = 1024) -> Path:
    """A pre-existing backup, `offset_days` old, with a manifest recording `size` bytes."""
    path = workspace / "data" / clean.BACKUP_DIRNAME / project / _stamp(offset_days)
    (path / clean.PAYLOAD_DIRNAME).mkdir(parents=True)
    (path / clean.MANIFEST_NAME).write_text(json.dumps({"bytes": size}))
    return path


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_ignored_paths_collapses_a_directory_into_one_entry(workspace: Path) -> None:
    """`build/` is one path, not three — the property that makes a huge .venv cheap to move."""
    found = {path.name for path in clean.ignored_paths(workspace / "proj")}
    assert found == {"build", "run.log"}


def test_ignored_paths_rejects_a_non_repository_with_one_error_line(tmp_path: Path) -> None:
    """aisland's own refusal (one `Error:` line, exit 1), never a builtin that prints a traceback."""
    with pytest.raises(AislandError, match=f"^{re.escape(str(tmp_path))}: git cannot list what it ignores there: "):
        clean.ignored_paths(tmp_path)


def test_plan_reports_sizes_and_file_counts(workspace: Path) -> None:
    entries = {entry.rel: entry for entry in clean.plan_entries(workspace / "proj", tmp_store(workspace))}

    assert set(entries) == {"build", "run.log"}
    assert entries["build"].files == 2  # a.txt + nested/b.txt
    assert entries["build"].bytes == len("a\n") + len("bb\n")
    assert entries["run.log"].files == 1


def tmp_store(workspace: Path) -> Path:
    return clean.backup_root(workspace / "data").resolve()


def test_an_unstattable_entry_costs_a_size_not_the_run(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable parent must not abort the plan with a traceback."""

    def _refuse(self: Path) -> object:
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "lstat", _refuse)
    entries = {entry.rel: entry for entry in clean.plan_entries(workspace / "proj", tmp_store(workspace))}

    assert set(entries) == {"build", "run.log"}
    assert (entries["run.log"].files, entries["run.log"].bytes, entries["run.log"].is_dir) == (1, 0, False)


def test_a_symlink_is_measured_never_followed(workspace: Path) -> None:
    """A link into $DATA_ROOT must not be reported as the tree it points at."""
    project = workspace / "proj"
    big = workspace / "outside"
    big.mkdir()
    big.joinpath("huge.bin").write_bytes(b"x" * 5000)
    project.joinpath("link.log").symlink_to(big)

    entry = next(item for item in clean.plan_entries(project, tmp_store(workspace)) if item.rel == "link.log")
    assert entry.files == 1
    assert entry.bytes < 5000


# --------------------------------------------------------------------------- #
# moving
# --------------------------------------------------------------------------- #


def test_ignored_content_moves_and_tracked_content_stays(workspace: Path) -> None:
    assert _run(workspace) == 0
    project = workspace / "proj"

    assert not (project / "build").exists()
    assert not (project / "run.log").exists()
    assert (project / "keep.py").read_text() == "# tracked\n"
    assert (project / ".gitignore").exists()

    payload = tmp_store(workspace) / "proj" / NOW.strftime(clean.STAMP_FORMAT) / clean.PAYLOAD_DIRNAME
    assert payload.joinpath("build", "nested", "b.txt").read_text() == "bb\n"
    assert payload.joinpath("run.log").read_text() == "noise\n"


def test_the_backup_restores_with_the_command_the_output_prints(workspace: Path) -> None:
    """The recovery path is the whole reason this moves instead of deleting."""
    _run(workspace)
    project = workspace / "proj"
    payload = tmp_store(workspace) / "proj" / NOW.strftime(clean.STAMP_FORMAT) / clean.PAYLOAD_DIRNAME

    for item in payload.iterdir():  # what `cp -R <payload>/. <project>/` does
        shutil.move(str(item), str(project / item.name))

    assert (project / "build" / "nested" / "b.txt").read_text() == "bb\n"
    assert {path.name for path in clean.ignored_paths(project)} == {"build", "run.log"}


def test_manifest_records_every_entry(workspace: Path) -> None:
    _run(workspace)
    manifest = json.loads(
        (tmp_store(workspace) / "proj" / NOW.strftime(clean.STAMP_FORMAT) / clean.MANIFEST_NAME).read_text()
    )

    assert manifest["project"] == "proj"
    assert manifest["created"] == NOW.isoformat(timespec="seconds")
    assert manifest["failed"] == []
    assert {entry["path"] for entry in manifest["entries"]} == {"build", "run.log"}
    assert manifest["bytes"] == sum(entry["bytes"] for entry in manifest["entries"])


def test_dry_run_changes_nothing(workspace: Path) -> None:
    assert _run(workspace, dry_run=True) == 0
    assert (workspace / "proj" / "build").exists()
    assert not tmp_store(workspace).exists()


def test_a_project_with_nothing_ignored_creates_no_backup(workspace: Path) -> None:
    project = workspace / "proj"
    shutil.rmtree(project / "build")
    (project / "run.log").unlink()

    assert _run(workspace) == 0
    assert not tmp_store(workspace).exists()


def test_declining_the_confirmation_moves_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clean.click, "confirm", lambda *args, **kwargs: False)

    assert clean.clean_project("proj", workspace_root=workspace, data_dir=workspace / "data", now=NOW) == 0
    assert (workspace / "proj" / "build").exists()
    assert not tmp_store(workspace).exists()


def test_an_entry_that_cannot_move_is_reported_and_left_in_place(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket or a permission error must not abort the rest of the run."""
    real_move = shutil.move

    def _selective(src: str, dst: str) -> object:
        if src.endswith("run.log"):
            raise OSError("device or resource busy")
        return real_move(src, dst)

    monkeypatch.setattr(clean.shutil, "move", _selective)

    assert _run(workspace) == 1
    assert (workspace / "proj" / "run.log").exists()  # left in place
    assert not (workspace / "proj" / "build").exists()  # the rest still moved

    manifest = json.loads(
        (tmp_store(workspace) / "proj" / NOW.strftime(clean.STAMP_FORMAT) / clean.MANIFEST_NAME).read_text()
    )
    assert [entry["path"] for entry in manifest["failed"]] == ["run.log"]
    assert [entry["path"] for entry in manifest["entries"]] == ["build"]


# --------------------------------------------------------------------------- #
# the store living inside the project it is cleaning
# --------------------------------------------------------------------------- #


def test_the_backup_store_survives_cleaning_the_project_it_lives_in(tmp_path: Path) -> None:
    """`aisland clean aisland`: `data/` is ignored wholesale and the store sits inside it."""
    project = _make_project(tmp_path, "aisland", gitignore="data/\n")
    (project / "data" / "jenkins").mkdir(parents=True)
    (project / "data" / "jenkins" / "war").write_text("big\n")
    store = project / "data" / clean.BACKUP_DIRNAME
    (store / "proj" / _stamp(1) / clean.PAYLOAD_DIRNAME).mkdir(parents=True)

    exit_code = clean.clean_project(
        "aisland",
        workspace_root=tmp_path,
        data_dir=project / "data",
        assume_yes=True,
        now=NOW,
    )

    assert exit_code == 0
    assert store.is_dir() and (store / "proj" / _stamp(1)).is_dir()  # the store, untouched
    assert not (project / "data" / "jenkins").exists()  # its sibling, moved
    payload = store / "aisland" / NOW.strftime(clean.STAMP_FORMAT) / clean.PAYLOAD_DIRNAME
    assert payload.joinpath("data", "jenkins", "war").read_text() == "big\n"


def test_split_around_leaves_an_unrelated_entry_whole(tmp_path: Path) -> None:
    entry = tmp_path / "build"
    entry.mkdir()
    assert list(clean._split_around(entry, tmp_path / "elsewhere" / "backups")) == [entry]


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #


def test_prune_deletes_expired_backups_and_keeps_the_rest(workspace: Path) -> None:
    old = _seed_backup(workspace, "proj", 8, size=2048)
    fresh = _seed_backup(workspace, "proj", 2)

    deleted = clean.prune_backups(tmp_store(workspace), clean.DEFAULT_KEEP_DAYS, now=NOW)

    assert [backup.path for backup in deleted] == [old]
    assert deleted[0].bytes == 2048  # read from the manifest, not re-walked
    assert not old.exists()
    assert fresh.exists()


def test_prune_never_deletes_what_this_tool_did_not_create(workspace: Path) -> None:
    stranger = tmp_store(workspace) / "proj" / "my-notes"
    stranger.mkdir(parents=True)
    _seed_backup(workspace, "proj", 30)

    clean.prune_backups(tmp_store(workspace), 7, now=NOW)

    assert stranger.exists()
    assert clean.read_backups(tmp_store(workspace)) == []


def test_prune_removes_a_project_directory_it_emptied(workspace: Path) -> None:
    _seed_backup(workspace, "proj", 9)

    clean.prune_backups(tmp_store(workspace), 7, now=NOW)

    assert not (tmp_store(workspace) / "proj").exists()


def test_prune_is_a_no_op_before_the_first_run(workspace: Path) -> None:
    assert clean.prune_backups(tmp_store(workspace), 7, now=NOW) == []


def test_prune_dry_run_reports_without_deleting(workspace: Path) -> None:
    old = _seed_backup(workspace, "proj", 8)

    assert [backup.path for backup in clean.prune_backups(tmp_store(workspace), 7, now=NOW, dry_run=True)] == [old]
    assert old.exists()


def test_a_backup_without_a_manifest_still_expires(workspace: Path) -> None:
    """An interrupted run leaves no manifest; the DIRECTORY NAME is what retention reads."""
    orphan = tmp_store(workspace) / "proj" / _stamp(9)
    orphan.mkdir(parents=True)

    deleted = clean.prune_backups(tmp_store(workspace), 7, now=NOW)

    assert [backup.path for backup in deleted] == [orphan]
    assert deleted[0].bytes == 0


def test_every_run_sweeps_expired_backups(workspace: Path) -> None:
    """The retention window is applied by the clean itself — not a separate command."""
    old = _seed_backup(workspace, "other", 8)
    fresh = _seed_backup(workspace, "other", 1)

    _run(workspace)

    assert not old.exists()
    assert fresh.exists()


def test_keep_days_zero_sweeps_everything_older_than_the_current_run(workspace: Path) -> None:
    recent = _seed_backup(workspace, "proj", 0.5)

    _run(workspace, keep_days=0)

    assert not recent.exists()
    assert (tmp_store(workspace) / "proj" / NOW.strftime(clean.STAMP_FORMAT)).is_dir()


def test_a_dry_run_sweeps_nothing(workspace: Path) -> None:
    old = _seed_backup(workspace, "proj", 30)

    _run(workspace, dry_run=True)

    assert old.exists()


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #


def test_the_workspace_root_is_refused_by_name(workspace: Path) -> None:
    with pytest.raises(click.ClickException, match="Refusing to clean the workspace root"):
        _run(workspace, project=workspace.name)


def test_a_path_resolving_to_the_workspace_root_is_refused(workspace: Path) -> None:
    with pytest.raises(click.ClickException, match="Refusing to clean the workspace root"):
        _run(workspace, project=".")


def test_an_unknown_project_lists_the_known_ones(workspace: Path) -> None:
    with pytest.raises(click.ClickException, match="Unknown project 'nope'. Known projects: proj"):
        _run(workspace, project="nope")


def test_a_project_without_a_checkout_is_refused(workspace: Path) -> None:
    """A `github` / `pypi` source deinits the submodule: the directory survives, empty."""
    detached = workspace / "detached"
    detached.mkdir()
    detached.joinpath("pyproject.toml").write_text('[project]\nname = "detached"\n')

    with pytest.raises(click.ClickException, match="not a git repository"):
        _run(workspace, project="detached")


def test_a_directory_that_is_not_its_repositorys_root_is_refused(workspace: Path) -> None:
    """Otherwise `git status` would report the ENCLOSING repository's ignored set."""
    _git(workspace / "proj", "config", "core.worktree", "../..")

    with pytest.raises(click.ClickException, match="not the root of its git repository"):
        _run(workspace)


def test_a_broken_git_pointer_is_refused(workspace: Path) -> None:
    shutil.rmtree(workspace / "proj" / ".git")
    (workspace / "proj" / ".git").write_text("gitdir: /nonexistent\n")

    with pytest.raises(click.ClickException, match="not the root of its git repository"):
        _run(workspace)


# --------------------------------------------------------------------------- #
# formatting + CLI wiring
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KiB"), (5 * 1024**2, "5.0 MiB"), (3 * 1024**4, "3.0 TiB")],
)
def test_human_readable_sizes(size: int, expected: str) -> None:
    assert clean._human(size) == expected


def test_the_cli_forwards_its_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[dict] = []

    def _record(project: str, **kwargs: object) -> int:
        seen.append({"project": project, **kwargs})
        return 0

    monkeypatch.setattr(cli_toplevel, "clean_project", _record)
    result = CliRunner().invoke(main, ["clean", "proj", "--keep-days", "3", "--dry-run", "-y"])

    assert result.exit_code == 0
    assert seen == [{"project": "proj", "keep_days": 3, "dry_run": True, "assume_yes": True}]


def test_the_cli_defaults_to_the_documented_retention_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[dict] = []

    def _record(project: str, **kwargs: object) -> int:
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(cli_toplevel, "clean_project", _record)
    CliRunner().invoke(main, ["clean", "proj"])

    assert seen[0]["keep_days"] == clean.DEFAULT_KEEP_DAYS == 7


def test_the_cli_rejects_a_negative_retention_window() -> None:
    result = CliRunner().invoke(main, ["clean", "proj", "--keep-days", "-1"])

    assert result.exit_code == 2


def test_a_refusal_reaches_the_shell_as_exit_1(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clean, "get_workspace_root", lambda: workspace)
    monkeypatch.setattr(clean, "DATA_DIR", workspace / "data")

    result = CliRunner().invoke(main, ["clean", "nope"])

    assert result.exit_code == 1
    assert "Unknown project 'nope'" in result.output


# --------------------------------------------------------------------------- #
# the store: selecting a backup
# --------------------------------------------------------------------------- #


def _restore(workspace: Path, project: str = "proj", **kwargs: object) -> int:
    """`restore_backup` bound to the test workspace."""
    return clean.restore_backup(
        project,
        workspace_root=workspace,
        data_dir=workspace / "data",
        assume_yes=True,
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_newest_backup_is_the_one_a_restore_defaults_to(workspace: Path) -> None:
    _seed_backup(workspace, "proj", 3)
    newest = _seed_backup(workspace, "proj", 1)

    assert clean.select_backup(tmp_store(workspace), "proj").path == newest


def test_a_named_stamp_wins_over_the_newest(workspace: Path) -> None:
    older = _seed_backup(workspace, "proj", 3)
    _seed_backup(workspace, "proj", 1)

    assert clean.select_backup(tmp_store(workspace), "proj", older.name).path == older


def test_an_unknown_stamp_lists_what_is_there(workspace: Path) -> None:
    _seed_backup(workspace, "proj", 3)

    with pytest.raises(click.ClickException, match=f"No backup 'nope' for 'proj'. Available: {_stamp(3)}"):
        clean.select_backup(tmp_store(workspace), "proj", "nope")


def test_a_project_with_no_backups_is_told_how_to_take_one(workspace: Path) -> None:
    with pytest.raises(click.ClickException, match="Take one with: aisland clean proj"):
        clean.select_backup(tmp_store(workspace), "proj")


def test_restorable_entries_come_from_the_manifest(workspace: Path) -> None:
    """The manifest's granularity is what moved — `data/jenkins`, not `data`."""
    _run(workspace)
    backup = clean.select_backup(tmp_store(workspace), "proj")

    assert clean.restorable_entries(backup) == ["build", "run.log"]


def test_restorable_entries_fall_back_to_the_payload(workspace: Path) -> None:
    """An interrupted clean leaves no manifest; the payload is still restorable."""
    backup_dir = _seed_backup(workspace, "proj", 1)
    (backup_dir / clean.MANIFEST_NAME).unlink()
    (backup_dir / clean.PAYLOAD_DIRNAME / "build").mkdir()
    (backup_dir / clean.PAYLOAD_DIRNAME / "run.log").write_text("noise\n")

    backup = clean.select_backup(tmp_store(workspace), "proj")
    assert clean.restorable_entries(backup) == ["build", "run.log"]


def test_a_backup_with_no_payload_directory_has_nothing_to_restore(workspace: Path) -> None:
    backup_dir = _seed_backup(workspace, "proj", 1)
    shutil.rmtree(backup_dir / clean.PAYLOAD_DIRNAME)

    backup = clean.select_backup(tmp_store(workspace), "proj")
    assert clean.restorable_entries(backup) == []
    assert _restore(workspace) == 0


def test_a_manifest_less_backup_restores_entry_by_entry(workspace: Path) -> None:
    """The payload fallback must survive a PARTIAL restore — there is no manifest to rewrite."""
    backup_dir = _seed_backup(workspace, "proj", 1)
    (backup_dir / clean.MANIFEST_NAME).unlink()
    (backup_dir / clean.PAYLOAD_DIRNAME / "run.log").write_text("noise\n")
    (backup_dir / clean.PAYLOAD_DIRNAME / "other.log").write_text("more\n")
    (workspace / "proj" / "run.log").unlink()

    assert _restore(workspace, only=["run.log"]) == 0
    assert (workspace / "proj" / "run.log").read_text() == "noise\n"
    assert clean.restorable_entries(clean.select_backup(tmp_store(workspace), "proj")) == ["other.log"]


def test_a_manifest_that_is_not_a_mapping_reads_as_empty(workspace: Path) -> None:
    backup_dir = _seed_backup(workspace, "proj", 1)
    (backup_dir / clean.MANIFEST_NAME).write_text("[1, 2, 3]")

    assert clean.read_manifest(backup_dir) == {}


def test_malformed_manifest_numbers_and_entries_are_ignored(workspace: Path) -> None:
    backup_dir = _seed_backup(workspace, "proj", 1)
    (backup_dir / clean.MANIFEST_NAME).write_text(
        json.dumps({"bytes": "lots", "files": None, "entries": ["build", {"no_path": 1}, {"path": "run.log"}]})
    )

    backup = clean.select_backup(tmp_store(workspace), "proj")
    assert (backup.bytes, backup.files, backup.entries) == (0, 0, ("run.log",))


# --------------------------------------------------------------------------- #
# the store: restoring
# --------------------------------------------------------------------------- #


def test_a_restore_puts_everything_back_and_consumes_the_backup(workspace: Path) -> None:
    _run(workspace)
    project = workspace / "proj"

    assert _restore(workspace) == 0
    assert (project / "build" / "nested" / "b.txt").read_text() == "bb\n"
    assert (project / "run.log").read_text() == "noise\n"
    assert clean.read_backups(tmp_store(workspace)) == []  # emptied, so removed
    assert not (tmp_store(workspace) / "proj").exists()


def test_a_restore_is_the_inverse_of_a_clean(workspace: Path) -> None:
    project = workspace / "proj"
    before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*") if ".git" not in path.parts)

    _run(workspace)
    _restore(workspace)

    after = sorted(path.relative_to(project).as_posix() for path in project.rglob("*") if ".git" not in path.parts)
    assert after == before


def test_restoring_one_entry_leaves_the_rest_in_the_backup(workspace: Path) -> None:
    _run(workspace)

    assert _restore(workspace, only=["run.log"]) == 0
    assert (workspace / "proj" / "run.log").exists()
    assert not (workspace / "proj" / "build").exists()

    backup = clean.select_backup(tmp_store(workspace), "proj")
    assert backup.entries == ("build",)  # the manifest no longer advertises what left
    assert backup.bytes == len("a\n") + len("bb\n")
    assert (backup.path / clean.PAYLOAD_DIRNAME / "build").is_dir()


def test_restoring_an_entry_the_backup_does_not_hold_is_refused(workspace: Path) -> None:
    _run(workspace)

    with pytest.raises(click.ClickException, match="holds no entry 'nope'"):
        _restore(workspace, only=["nope"])


def test_a_path_the_project_already_has_is_skipped_not_clobbered(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")

    assert _restore(workspace) == 1  # a partial restore must be visible to a script
    assert (workspace / "proj" / "run.log").read_text() == "rebuilt\n"
    assert (workspace / "proj" / "build").exists()  # the rest still came back
    assert clean.select_backup(tmp_store(workspace), "proj").entries == ("run.log",)


def test_force_replaces_what_the_project_already_has(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")

    assert _restore(workspace, force=True) == 0
    assert (workspace / "proj" / "run.log").read_text() == "noise\n"


def test_force_replaces_a_directory_too(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "build").mkdir()
    (workspace / "proj" / "build" / "stale.txt").write_text("stale\n")

    assert _restore(workspace, force=True) == 0
    assert not (workspace / "proj" / "build" / "stale.txt").exists()
    assert (workspace / "proj" / "build" / "nested" / "b.txt").read_text() == "bb\n"


def test_declining_the_force_confirmation_restores_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")
    monkeypatch.setattr(clean.click, "confirm", lambda *args, **kwargs: False)

    exit_code = clean.restore_backup("proj", force=True, workspace_root=workspace, data_dir=workspace / "data")

    assert exit_code == 1
    assert (workspace / "proj" / "run.log").read_text() == "rebuilt\n"
    assert not (workspace / "proj" / "build").exists()


def test_a_nested_entry_merges_into_a_directory_that_came_back(tmp_path: Path) -> None:
    """`data/jenkins` restores into a `data/` the project re-created — the manifest's point."""
    project = _make_project(tmp_path, "aisland", gitignore="data/\n")
    (project / "data" / "jenkins").mkdir(parents=True)
    (project / "data" / "jenkins" / "war").write_text("big\n")
    store = project / "data" / clean.BACKUP_DIRNAME
    store.mkdir(parents=True)

    clean.clean_project("aisland", workspace_root=tmp_path, data_dir=project / "data", assume_yes=True, now=NOW)
    (project / "data" / "mlflow").mkdir(parents=True)  # the project rebuilt a sibling meanwhile

    exit_code = clean.restore_backup("aisland", workspace_root=tmp_path, data_dir=project / "data", assume_yes=True)

    assert exit_code == 0
    assert (project / "data" / "jenkins" / "war").read_text() == "big\n"
    assert (project / "data" / "mlflow").is_dir()  # untouched


def test_an_entry_missing_from_the_payload_is_reported(workspace: Path) -> None:
    _run(workspace)
    backup = clean.select_backup(tmp_store(workspace), "proj")
    shutil.rmtree(backup.path / clean.PAYLOAD_DIRNAME / "build")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "run.log").exists()


def test_a_dry_run_restores_nothing(workspace: Path) -> None:
    _run(workspace)

    assert _restore(workspace, dry_run=True) == 0
    assert not (workspace / "proj" / "build").exists()
    assert clean.select_backup(tmp_store(workspace), "proj").entries == ("build", "run.log")


def test_a_dry_run_reports_a_collision_as_exit_1(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("rebuilt\n")

    assert _restore(workspace, dry_run=True) == 1


def test_an_entry_that_cannot_be_restored_is_reported(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run(workspace)
    real_move = shutil.move

    def _selective(src: str, dst: str) -> object:
        if src.endswith("run.log"):
            raise OSError("device or resource busy")
        return real_move(src, dst)

    monkeypatch.setattr(clean.shutil, "move", _selective)

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build").exists()
    assert not (workspace / "proj" / "run.log").exists()


@pytest.mark.parametrize("rel", ["../outside", "/etc/passwd", "build/../../outside"])
def test_a_manifest_path_that_escapes_the_project_is_refused(workspace: Path, rel: str) -> None:
    """The manifest is a file on disk this code did not necessarily write."""
    _run(workspace)
    backup = clean.select_backup(tmp_store(workspace), "proj")
    manifest = json.loads((backup.path / clean.MANIFEST_NAME).read_text())
    manifest["entries"] = [{"path": rel, "bytes": 1, "files": 1}]
    (backup.path / clean.MANIFEST_NAME).write_text(json.dumps(manifest))

    with pytest.raises(click.ClickException, match="escapes the project directory"):
        _restore(workspace)


def test_restoring_refuses_the_workspace_root(workspace: Path) -> None:
    with pytest.raises(click.ClickException, match="Refusing to restore the workspace root"):
        _restore(workspace, project=workspace.name)


# --------------------------------------------------------------------------- #
# the store: listing
# --------------------------------------------------------------------------- #


def _list(workspace: Path, capsys: pytest.CaptureFixture[str], project: Optional[str] = None, **kwargs: object) -> str:
    clean.list_store(project, data_dir=workspace / "data", now=NOW, **kwargs)  # type: ignore[arg-type]
    return str(capsys.readouterr().out)


def test_listing_an_empty_store_says_how_to_fill_it(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert clean.list_store(data_dir=workspace / "data", now=NOW) == 0
    capsys.readouterr()

    assert "aisland clean <project>" in _list(workspace, capsys)
    assert "aisland clean proj" in _list(workspace, capsys, "proj")


def test_listing_shows_the_newest_first(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_backup(workspace, "proj", 5)
    _seed_backup(workspace, "proj", 1)

    rows = [line for line in _list(workspace, capsys).splitlines() if "proj " in line]
    assert _stamp(1) in rows[0] and _stamp(5) in rows[1]


def test_listing_filters_by_project(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_backup(workspace, "proj", 1)
    _seed_backup(workspace, "other", 1)

    assert "other" not in _list(workspace, capsys, "proj")


def test_listing_shows_when_a_backup_expires(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed_backup(workspace, "proj", 2)
    _seed_backup(workspace, "other", 9)

    output = _list(workspace, capsys)
    assert "in 5d" in output  # 7 - 2
    assert "next run" in output  # already past the window


def test_listing_marks_an_unknown_entry_count(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A manifest-less backup has a size of 0 and no entry list — say so, don't invent 0."""
    orphan = tmp_store(workspace) / "proj" / _stamp(1)
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

    assert seen == [("proj", {"keep_days": 3}), (None, {"keep_days": clean.DEFAULT_KEEP_DAYS})]


def test_the_clean_output_names_the_restore_command(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The two verbs must stay discoverable from each other."""
    _run(workspace)
    output = capsys.readouterr().out

    assert "aisland backups restore proj" in output
    assert "cp -R" in output  # the fallback that works when this code is what broke


# --------------------------------------------------------------------------- #
# the store: an identical copy is not a collision
# --------------------------------------------------------------------------- #


def test_an_identical_file_is_not_a_collision(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")  # exactly what the backup holds

    assert _restore(workspace) == 0
    assert (workspace / "proj" / "run.log").read_text() == "noise\n"
    assert clean.read_backups(tmp_store(workspace)) == []  # nothing left worth keeping


def test_a_hand_copied_tree_is_recognised_despite_new_mtimes(workspace: Path) -> None:
    """The case this exists for: the operator already did `cp -R`, which restamps every file."""
    _run(workspace)
    backup = clean.select_backup(tmp_store(workspace), "proj")
    shutil.copytree(
        backup.path / clean.PAYLOAD_DIRNAME / "build",
        workspace / "proj" / "build",
        copy_function=shutil.copy,  # like `cp -R`: contents, not timestamps
    )
    assert (workspace / "proj" / "build" / "a.txt").stat().st_mtime != (
        backup.path / clean.PAYLOAD_DIRNAME / "build" / "a.txt"
    ).stat().st_mtime

    assert _restore(workspace) == 0
    assert (workspace / "proj" / "build" / "nested" / "b.txt").read_text() == "bb\n"


def test_the_same_size_with_different_bytes_is_still_a_collision(workspace: Path) -> None:
    """The size pre-check is an optimisation, never the answer."""
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("NOISE\n")  # same length, different content

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "run.log").read_text() == "NOISE\n"
    assert clean.select_backup(tmp_store(workspace), "proj").entries == ("run.log",)


def test_a_tree_with_an_extra_file_is_a_collision(workspace: Path) -> None:
    _run(workspace)
    backup = clean.select_backup(tmp_store(workspace), "proj")
    shutil.copytree(backup.path / clean.PAYLOAD_DIRNAME / "build", workspace / "proj" / "build")
    (workspace / "proj" / "build" / "extra.txt").write_text("x\n")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build" / "extra.txt").exists()


def test_a_renamed_file_is_a_collision_even_at_the_same_size(workspace: Path) -> None:
    """Same byte total and same file count, different names — only the path set catches it."""
    _run(workspace)
    backup = clean.select_backup(tmp_store(workspace), "proj")
    shutil.copytree(backup.path / clean.PAYLOAD_DIRNAME / "build", workspace / "proj" / "build")
    (workspace / "proj" / "build" / "nested" / "b.txt").rename(workspace / "proj" / "build" / "nested" / "z.txt")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build" / "nested" / "z.txt").exists()


def test_a_file_where_the_backup_holds_a_directory_is_a_collision(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "build").write_text("not a directory\n")

    assert _restore(workspace) == 1
    assert (workspace / "proj" / "build").is_file()


def test_an_identical_symlink_is_not_a_collision(workspace: Path) -> None:
    project = workspace / "proj"
    project.joinpath("link.log").symlink_to("keep.py")
    _run(workspace)
    project.joinpath("link.log").symlink_to("keep.py")

    assert _restore(workspace) == 0


def test_a_symlink_to_a_different_target_is_a_collision(workspace: Path) -> None:
    project = workspace / "proj"
    project.joinpath("link.log").symlink_to("keep.py")
    _run(workspace)
    project.joinpath("link.log").symlink_to("elsewhere.py")

    assert _restore(workspace) == 1
    assert project.joinpath("link.log").readlink().name == "elsewhere.py"


def test_an_unreadable_file_counts_as_a_collision(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A comparison that cannot finish must keep the backup, not drop it."""
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")

    def _refuse(*args: object, **kwargs: object) -> bool:
        raise OSError("unreadable")

    monkeypatch.setattr(clean.filecmp, "cmp", _refuse)

    assert _restore(workspace) == 1
    assert clean.select_backup(tmp_store(workspace), "proj").entries == ("run.log",)


def test_force_does_not_bother_moving_an_identical_copy(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")
    before = (workspace / "proj" / "run.log").stat().st_mtime_ns

    assert _restore(workspace, force=True) == 0
    assert (workspace / "proj" / "run.log").stat().st_mtime_ns == before  # untouched


def test_a_duplicate_leaves_the_manifest_consistent(workspace: Path) -> None:
    _run(workspace)
    (workspace / "proj" / "run.log").write_text("noise\n")

    assert _restore(workspace, only=["run.log"]) == 0
    backup = clean.select_backup(tmp_store(workspace), "proj")
    assert backup.entries == ("build",)
    assert not (backup.path / clean.PAYLOAD_DIRNAME / "run.log").exists()


# --------------------------------------------------------------------------- #
# the store: pruning on its own
# --------------------------------------------------------------------------- #


def _prune(workspace: Path, **kwargs: object) -> int:
    kwargs.setdefault("assume_yes", True)
    return clean.prune_store(data_dir=workspace / "data", now=NOW, **kwargs)  # type: ignore[arg-type]


def test_pruning_an_unexpired_store_deletes_nothing(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fresh = _seed_backup(workspace, "proj", 1)

    assert _prune(workspace) == 0
    assert "Nothing older than 7 days" in capsys.readouterr().out
    assert fresh.exists()


def test_pruning_deletes_what_the_window_expired(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = _seed_backup(workspace, "proj", 8, size=2048)
    fresh = _seed_backup(workspace, "proj", 1)

    assert _prune(workspace) == 0
    assert not old.exists()
    assert fresh.exists()
    assert "freed 2.0 KiB" in capsys.readouterr().out


def test_pruning_honours_a_tighter_window(workspace: Path) -> None:
    recent = _seed_backup(workspace, "proj", 1)

    assert _prune(workspace, keep_days=0) == 0
    assert not recent.exists()


def test_a_prune_dry_run_deletes_nothing(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = _seed_backup(workspace, "proj", 8)

    assert _prune(workspace, dry_run=True) == 0
    assert old.exists()
    assert "Dry run" in capsys.readouterr().out


def test_declining_the_prune_confirmation_deletes_nothing(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _seed_backup(workspace, "proj", 8)
    monkeypatch.setattr(clean.click, "confirm", lambda *args, **kwargs: False)

    assert clean.prune_store(data_dir=workspace / "data", now=NOW) == 0
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
        {"keep_days": clean.DEFAULT_KEEP_DAYS, "dry_run": False, "assume_yes": False},
    ]
