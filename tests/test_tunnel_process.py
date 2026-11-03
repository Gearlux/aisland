"""The tunnel's pidfile and binary hold only what is really there.

A pidfile outlives a reboot, and the number in it may then belong to any other process of the user: `aisland tunnel
stop` signals a pid only when its command line is this machine's VS Code CLI, and `start` never takes another process
for a running tunnel. An interrupted install of that CLI leaves no binary a later run would take for an installed one.

Unlike `tests/test_tunnel.py`, these tests start real processes (`sleep`, a script standing in for the CLI).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from aisland import tunnel
from tests.test_tunnel import _cli_archive


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv(tunnel.TUNNEL_HOME_ENV, str(tmp_path / "tunnel"))
    (tmp_path / "tunnel").mkdir()
    return tmp_path / "tunnel"


@pytest.fixture
def stranger() -> Iterator["subprocess.Popen[bytes]"]:
    """A live process of this user that is not the tunnel."""
    process = subprocess.Popen(["sleep", "30"])
    yield process
    process.kill()
    process.wait()


def test_stop_never_signals_a_process_that_is_not_the_tunnel(stranger: "subprocess.Popen[bytes]") -> None:
    tunnel.pid_path().write_text(f"{stranger.pid}\n")
    assert tunnel.running() is False
    assert tunnel.stop() == {"running": False, "stopped": False}
    time.sleep(0.2)
    assert stranger.poll() is None  # still running
    assert not tunnel.pid_path().exists()


def test_a_process_of_the_cli_is_the_tunnel(state: Path) -> None:
    tunnel.cli_path().write_text("#!/bin/sh\nsleep 30\n")
    tunnel.cli_path().chmod(0o755)
    process = subprocess.Popen([str(tunnel.cli_path()), "tunnel"])
    try:
        time.sleep(0.2)
        tunnel.pid_path().write_text(f"{process.pid}\n")
        assert tunnel.running() is True
        assert tunnel.stop()["stopped"] is True
        assert process.wait(timeout=10) != 0  # ended by the signal
    finally:
        process.kill()


def test_an_interrupted_install_leaves_no_binary(state: Path, tmp_path: Path) -> None:
    archive = _cli_archive(tmp_path)
    with patch.object(tunnel.shutil, "copyfileobj", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        tunnel.install_cli(tarball=archive)
    assert sorted(path.name for path in state.iterdir()) == []
    installed = tunnel.install_cli(tarball=archive)
    assert installed.read_bytes().startswith(b"#!/bin/sh")
    assert installed.stat().st_mode & 0o111
