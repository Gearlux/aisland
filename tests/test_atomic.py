"""A state file is written whole or not at all (`aisland/atomic.py`).

`workspace.toml`, `.env`, the supervisor's document, the completion file and the harness's record go through
`atomic.write_text`: an interrupted write leaves the old file as it was, a file keeps its mode and a link its target,
and a new `.env` is readable by its owner alone.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from aisland import atomic, envfile, git_exclude, locations
from aisland.setup import editor_files
from aisland.wheels import files as wheels_files
from tests.records import project_metadata


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_an_interrupted_write_leaves_the_old_file_and_no_scratch_file(tmp_path: Path) -> None:
    target = tmp_path / "workspace.toml"
    target.write_text("old\n")
    with patch.object(atomic.os, "replace", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        atomic.write_text(target, "new\n")
    assert target.read_text() == "old\n"
    assert [path.name for path in tmp_path.iterdir()] == ["workspace.toml"]


def test_a_file_keeps_its_mode_and_a_new_one_gets_the_mode_asked_for(tmp_path: Path) -> None:
    kept = tmp_path / "kept"
    kept.write_text("x")
    kept.chmod(0o640)
    atomic.write_text(kept, "y", new_mode=atomic.PRIVATE)
    assert (kept.read_text(), _mode(kept)) == ("y", 0o640)
    fresh = tmp_path / "fresh"
    atomic.write_text(fresh, "z", new_mode=atomic.PRIVATE)
    assert _mode(fresh) == 0o600


def test_a_link_stays_a_link_and_its_target_is_written(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "bashrc"
    real.parent.mkdir()
    real.write_text("# mine\n")
    link = tmp_path / ".bashrc"
    link.symlink_to(real)
    atomic.write_text(link, "# mine\n# completion\n")
    assert link.is_symlink()
    assert real.read_text() == "# mine\n# completion\n"


def test_a_new_env_is_readable_by_its_owner_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    env = envfile.write({"TOKEN": "secret"})
    assert env.read_text() == "TOKEN=secret\n"
    assert _mode(env) == 0o600


def test_the_new_text_and_the_folder_entry_reach_the_disk_before_and_after_the_move(tmp_path: Path) -> None:
    """The scratch file is fsynced before ``os.replace`` and its folder after, so a power cut leaves either file."""
    target = tmp_path / "workspace.toml"
    target.write_text("old\n")
    events: list[str] = []
    real_fsync, real_replace = atomic.os.fsync, atomic.os.replace

    def fsync(handle: int) -> None:
        events.append("folder" if stat.S_ISDIR(atomic.os.fstat(handle).st_mode) else "file")
        real_fsync(handle)

    def replace(source: str, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    with patch.object(atomic.os, "fsync", fsync), patch.object(atomic.os, "replace", replace):
        atomic.write_text(target, "new\n")
    assert events == ["file", "replace", "folder"]
    assert target.read_text() == "new\n"


def test_the_files_setup_and_the_wheels_run_rewrite_are_written_whole_or_not_at_all(tmp_path: Path) -> None:
    """The root `mypy.ini` (committed), a repository's `info/exclude` and the wheels folder's files were written in
    place: an interruption half way left each one cut short."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "mypy.ini").write_text("[mypy]\nmypy_path = old\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    exclude = root / ".git" / "info" / "exclude"
    exclude.write_text("# mine\n")
    wheels = root / "wheels"
    wheels.mkdir()
    (wheels / "README.md").write_text("old\n")
    (root / "app").mkdir()
    writers = [
        lambda: editor_files.sync_mypy_path(root, [project_metadata("app", root / "app")]),
        lambda: git_exclude.exclude(root, ["*.egg-info/"]),
        lambda: wheels_files.write_wheels(wheels, [], {"README.md": "new\n"}),
    ]
    for write in writers:
        with patch.object(atomic.os, "replace", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
            write()
    assert (root / "mypy.ini").read_text() == "[mypy]\nmypy_path = old\n"
    assert exclude.read_text() == "# mine\n"
    assert (wheels / "README.md").read_text() == "old\n"
