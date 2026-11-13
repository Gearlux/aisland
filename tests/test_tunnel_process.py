"""The tunnel's pidfile and binary hold only what is really there.

A pidfile outlives a reboot, and the number in it may then belong to any other process of the user: `aisland tunnel
stop` signals a pid only when its command line is this machine's VS Code CLI, and `start` never takes another process
for a running tunnel. An interrupted install of that CLI leaves no binary a later run would take for an installed one.

Unlike `tests/test_tunnel.py`, these tests start real processes (`sleep`, a script standing in for the CLI).
"""

from __future__ import annotations

import signal
import subprocess
import time
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from aisland.errors import AislandError
from aisland.tunnel import binary as tunnel_binary
from aisland.tunnel import host as tunnel
from aisland.tunnel import state as tunnel_state
from tests.test_tunnel import _cli_archive


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv(tunnel_state.TUNNEL_HOME_ENV, str(tmp_path / "tunnel"))
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
    tunnel_binary.cli_path().write_text("#!/bin/sh\nsleep 30\n")
    tunnel_binary.cli_path().chmod(0o755)
    process = subprocess.Popen([str(tunnel_binary.cli_path()), "tunnel"])
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
    with (
        patch.object(tunnel_binary.shutil, "copyfileobj", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        tunnel_binary.install_cli(tarball=archive)
    assert sorted(path.name for path in state.iterdir()) == []
    installed = tunnel_binary.install_cli(tarball=archive)
    assert installed.read_bytes().startswith(b"#!/bin/sh")
    assert installed.stat().st_mode & 0o111


def _fake_cli(body: str) -> "subprocess.Popen[bytes]":
    """Run a script standing in for the CLI, recorded in the pidfile as `start` records the tunnel."""
    ready = tunnel_binary.cli_path().with_name("ready")
    tunnel_binary.cli_path().write_text(f"#!/bin/sh\n{body}: > '{ready}'\nwhile :; do sleep 0.1; done\n")
    tunnel_binary.cli_path().chmod(0o755)
    process = subprocess.Popen([str(tunnel_binary.cli_path()), "tunnel"])
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:  # the shell has set its trap
        time.sleep(0.05)
    tunnel.pid_path().write_text(f"{process.pid}\n")
    return process


def test_stop_returns_only_once_the_tunnel_has_exited(state: Path) -> None:
    """A tunnel takes a moment to shut down: a pidfile removed before it has gone let an immediate `start` bring up
    a second tunnel beside the first."""
    process = _fake_cli("trap 'sleep 1; exit 0' TERM\n")
    try:
        assert tunnel.stop()["stopped"] is True
        # Exited through its own shutdown, already: its 1 s shutdown would outlast this wait. (On Linux a process's
        # command line goes empty a moment before it can be reaped, so an instant poll() can still see it.)
        assert process.wait(timeout=0.5) == 0
        assert not tunnel.pid_path().exists()
        assert tunnel.running() is False
    finally:
        process.kill()
        process.wait()


def test_a_tunnel_that_ignores_the_stop_is_killed(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "STOP_GRACE_S", 0.5)
    process = _fake_cli("trap '' TERM\n")
    try:
        assert tunnel.stop()["stopped"] is True
        assert process.wait(timeout=0.5) == -signal.SIGKILL  # killed, and reaped within a moment of stop's return
        assert not tunnel.pid_path().exists()
    finally:
        process.kill()
        process.wait()


def test_a_tunnel_that_outlives_the_kill_keeps_its_pidfile(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "STOP_GRACE_S", 0.2)
    tunnel.pid_path().write_text("4711\n")
    with (
        patch.object(tunnel, "_is_tunnel", return_value=True),
        patch.object(tunnel, "_signal", return_value=True) as sent,
        pytest.raises(AislandError, match=r"^The tunnel \(pid 4711\) is still running 0.4 s after it was stopped"),
    ):
        tunnel.stop()
    assert [args[0] for args in sent.call_args_list] == [(4711, signal.SIGTERM), (4711, signal.SIGKILL)]
    assert tunnel.pid_path().read_text() == "4711\n"  # so `start` reports it instead of starting a second one


def test_a_pid_whose_command_line_cannot_be_read_is_never_signalled(
    stranger: "subprocess.Popen[bytes]", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A box without `ps` (and, here, without `/proc`): the pid is not identified, so it is not the tunnel."""
    real_read_bytes = Path.read_bytes

    def no_proc(self: Path) -> bytes:
        if str(self).startswith("/proc/"):
            raise FileNotFoundError(str(self))
        return real_read_bytes(self)

    def no_ps(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("ps")

    monkeypatch.setattr(tunnel.Path, "read_bytes", no_proc)
    monkeypatch.setattr(tunnel.subprocess, "run", no_ps)
    tunnel.pid_path().write_text(f"{stranger.pid}\n")
    assert tunnel.running() is False
    assert tunnel.stop() == {"running": False, "stopped": False}
    assert stranger.poll() is None


def test_on_linux_the_command_line_is_read_from_proc_without_ps(
    stranger: "subprocess.Popen[bytes]", monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read_bytes = Path.read_bytes
    cli = str(tunnel_binary.cli_path()).encode()

    def read_bytes(self: Path) -> bytes:
        if str(self) == f"/proc/{stranger.pid}/cmdline":
            return cli + b"\0tunnel\0"
        return real_read_bytes(self)

    monkeypatch.setattr(tunnel.Path, "read_bytes", read_bytes)
    monkeypatch.setattr(tunnel.subprocess, "run", lambda *a, **k: pytest.fail("ps was asked"))
    assert tunnel._is_tunnel(stranger.pid) is True
