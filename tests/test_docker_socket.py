"""Another engine's `/var/run/docker.sock` is replaced only once the user says so.

`aisland docker start` links the standard socket to Colima's. One that is already there and is NOT that link —
Docker Desktop's socket, a link to another engine — belongs to something else: it is replaced (with `sudo`) only
after a "yes" in a terminal, the default being no. Without one, or on a "no", it is left as it is and a warning names
the two commands that switch it. A missing one is linked (with `sudo`) only after a "yes" too, the default being
yes; without a terminal no one is asked, `sudo` never runs, and the warning names the command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from aisland import paths
from aisland.services import docker
from aisland.services.docker import DockerService
from aisland.setup import provision


@pytest.fixture
def sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Colima's socket under a scratch HOME, the standard one a link to another engine's; every `sudo` recorded."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(docker.Path, "home", lambda: tmp_path)
    (tmp_path / docker.COLIMA_PROFILE_DIR).mkdir(parents=True)
    (tmp_path / docker.COLIMA_PROFILE_DIR / "docker.sock").touch()
    standard = tmp_path / "run" / "docker.sock"
    standard.parent.mkdir()
    standard.symlink_to(tmp_path / "desktop.sock")
    monkeypatch.setattr(docker, "STANDARD_SOCKET", standard)
    calls: List[List[str]] = []

    def run(argv: List[str], **kwargs: object) -> "subprocess.CompletedProcess[str]":
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(docker.subprocess, "run", run)
    return calls


def test_without_a_yes_the_other_engines_socket_is_left_and_the_fix_named(
    sockets: List[List[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: List[bool] = []

    def refuse(question: str, default: bool = True) -> bool:
        asked.append(default)
        return False

    monkeypatch.setattr(provision, "agree", refuse)
    DockerService().configure_socket()
    assert sockets == []  # no sudo at all
    assert asked == [False]  # asked, with "no" the default
    err = capsys.readouterr().err
    assert "left as it is" in err and "sudo rm" in err and "sudo ln -s" in err


def test_after_a_yes_the_socket_is_linked_to_colimas(sockets: List[List[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision, "agree", lambda question, default=True: True)
    DockerService().configure_socket()
    assert [argv[:2] for argv in sockets] == [["sudo", "rm"], ["sudo", "ln"]]


@pytest.mark.parametrize("answer", [False, True])
def test_a_missing_socket_is_linked_only_after_a_yes(
    sockets: List[List[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], answer: bool
) -> None:
    docker.STANDARD_SOCKET.unlink()
    asked: List[bool] = []

    def reply(question: str, default: bool = True) -> bool:
        asked.append(default)
        return answer

    monkeypatch.setattr(provision, "agree", reply)
    DockerService().configure_socket()
    assert asked == [True]  # asked, with "yes" the default
    if answer:
        assert [argv[:2] for argv in sockets] == [["sudo", "ln"]]
    else:
        assert sockets == []
        assert (
            f"sudo ln -s {Path(docker.Path.home()) / docker.COLIMA_PROFILE_DIR / 'docker.sock'}"
            in capsys.readouterr().err
        )


def test_without_a_terminal_a_missing_socket_is_left_and_sudo_never_runs(
    sockets: List[List[str]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What `aisland-mcp`'s `service_start docker` meets: stdin is no terminal, so no question and no `sudo`."""
    docker.STANDARD_SOCKET.unlink()
    monkeypatch.setattr(provision, "_can_ask", lambda: False)
    DockerService().configure_socket()
    assert sockets == []
    assert "is missing" in capsys.readouterr().err
