"""``gitcmd.git``: output captured as text, ``network`` and ``check`` behave as documented."""

import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest

from aisland import gitcmd, mirrors
from aisland.errors import AislandError
from aisland.wheels import prune as wheels_prune


def test_git_captures_text_output_in_the_repository(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    done = gitcmd.git(tmp_path, "rev-parse", "--is-inside-work-tree")
    assert (done.returncode, done.stdout.strip()) == (0, "true")


def test_a_failing_command_returns_its_code_unless_checked(tmp_path: Path) -> None:
    assert gitcmd.git(tmp_path, "rev-parse", "--is-inside-work-tree").returncode != 0
    with pytest.raises(AislandError, match="git rev-parse --is-inside-work-tree failed"):
        gitcmd.git(tmp_path, "rev-parse", "--is-inside-work-tree", check=True)


def test_read_answers_what_git_prints_and_nothing_when_git_cannot(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert gitcmd.read(tmp_path, "rev-parse", "--is-inside-work-tree") == "true"
    assert gitcmd.read(tmp_path, "rev-parse", "HEAD") is None  # no commit yet
    assert gitcmd.read(tmp_path / "missing", "rev-parse", "HEAD") is None  # no such folder


def test_read_answers_nothing_when_git_is_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert gitcmd.read(tmp_path, "rev-parse", "HEAD") is None


def test_remote_url_is_the_address_of_a_remote_or_nothing(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert gitcmd.remote_url(tmp_path) is None
    subprocess.run(["git", "remote", "add", "origin", "https://example.org/acme/lib.git"], cwd=tmp_path, check=True)
    assert gitcmd.remote_url(tmp_path) == "https://example.org/acme/lib.git"
    assert gitcmd.remote_url(tmp_path, "mirror") is None


def test_network_never_prompts_and_has_a_default_time_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, Any] = {}

    def fake_run(cmd: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    gitcmd.git(tmp_path, "fetch", network=True)
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["timeout"] == gitcmd.NETWORK_TIMEOUT
    gitcmd.git(tmp_path, "fetch", network=True, timeout=5)
    assert seen["timeout"] == 5
    gitcmd.git(tmp_path, "status")
    assert (seen["env"], seen["timeout"]) == (None, None)


# --- a remote that stops answering ------------------------------------------------------------------------------


@pytest.fixture
def silent_remote() -> Iterator[str]:
    """An HTTP remote that accepts every connection and never sends a byte: without a guard, git waits forever."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: List[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:  # the fixture closed the socket
                return
            held.append(connection)

    threading.Thread(target=accept, daemon=True).start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/repo.git"
    server.close()
    for connection in held:
        connection.close()


def _gitcmd_git(repo: Path, url: str) -> "subprocess.CompletedProcess[str]":
    return gitcmd.git(repo, "ls-remote", url, network=True)


def _gitcmd_fetch(repo: Path, url: str) -> "subprocess.CompletedProcess[str]":
    return gitcmd.fetch(repo, "--quiet", url)


def _mirrors_git(repo: Path, url: str) -> "subprocess.CompletedProcess[str]":
    return mirrors._git(repo, "ls-remote", url, network=True)


def _wheels_prune_run(repo: Path, url: str) -> "subprocess.CompletedProcess[str]":
    return wheels_prune._run(repo, "ls-remote", url, network=True)


@pytest.mark.parametrize("call", [_gitcmd_git, _gitcmd_fetch, _mirrors_git, _wheels_prune_run])
def test_every_network_call_gives_up_on_a_remote_that_stops_sending(
    tmp_path: Path, silent_remote: str, monkeypatch: pytest.MonkeyPatch, call: Callable[[Path, str], Any]
) -> None:
    """A transfer may take long (no overall limit on a push or a fetch); a remote that sends nothing for
    `STALL_SECONDS` is given up."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(gitcmd, "STALL_SECONDS", 2)
    started = time.monotonic()
    done = call(tmp_path, silent_remote)
    assert time.monotonic() - started < 30
    assert done.returncode != 0
    assert "too slow" in done.stderr


def test_a_fetch_never_prompts_and_has_no_overall_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, Any] = {}

    def fake_run(cmd: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        seen.update(kwargs, cmd=cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    gitcmd.fetch(tmp_path, "-q", "origin")
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen.get("timeout") is None
    assert seen["cmd"][-3:] == ["fetch", "-q", "origin"]
