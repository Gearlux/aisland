"""A state file is written whole or not at all (`aisland/atomic.py`).

`workspace.toml`, `.env`, the supervisor's document, the completion file and the harness's record go through
`atomic.write_text`: an interrupted write leaves the old file as it was, a file keeps its mode and a link its target,
and a new `.env` is readable by its owner alone.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from aisland import atomic, envfile, locations


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
