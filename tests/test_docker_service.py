"""Tests for ``DockerService`` — the Colima engine and its VM mounts.

The property under test: a bind mount into a container can only see what the Colima VM
shares, and Colima shares ONLY ``$HOME`` by default (measured 2026-08-17 on colima 0.10.3:
``colima ssh -- mount | grep virtiofs`` → ``/Users/<me>`` and nothing else). Worse, passing
ANY ``--mount`` REPLACES that default rather than adding to it — ``colima start --mount
/Volumes/Store:r`` left the VM with ``/Volumes/Store`` and no ``$HOME``, silently breaking every
bind mount under the home directory. ``aisland docker start --mount`` therefore always keeps
``$HOME:w`` in the argv it hands to colima, and refuses to change mounts under a running VM
(they apply at boot) rather than restarting the engine — and every container on it — unasked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from aisland import probe
from aisland.services import docker as docker_mod
from aisland.services.docker import DockerService, Mount, parse_mount


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``$HOME`` carrying a colima profile with the DEFAULT (empty) mounts list.

    Set through the environment (not a ``Path.home`` patch) so ``~`` expansion agrees with it.
    """
    fake_home = tmp_path / "home"
    profile = fake_home / ".colima" / "default"
    profile.mkdir(parents=True)
    (profile / "colima.yaml").write_text("mounts: []\n")
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


# --------------------------------------------------------------------------- #
# mount specs
# --------------------------------------------------------------------------- #


def test_a_bare_directory_is_read_only_and_a_w_suffix_makes_it_writable(tmp_path: Path) -> None:
    """Colima's own syntax: ``DIR`` (read-only), ``DIR:w`` (writable); ``DIR:r`` is accepted too."""
    assert parse_mount(str(tmp_path)) == Mount(tmp_path.resolve(), writable=False)
    assert parse_mount(f"{tmp_path}:r") == Mount(tmp_path.resolve(), writable=False)
    assert parse_mount(f"{tmp_path}:w") == Mount(tmp_path.resolve(), writable=True)


def test_a_tilde_is_expanded(home: Path) -> None:
    (home / "data").mkdir()
    assert parse_mount("~/data:w").location == (home / "data").resolve()


def test_a_missing_directory_is_refused_by_name(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(click.ClickException, match=str(missing)):
        parse_mount(str(missing))


def test_a_mount_renders_back_to_colimas_syntax(tmp_path: Path) -> None:
    assert Mount(tmp_path, writable=False).spec == f"{tmp_path}:r"
    assert Mount(tmp_path, writable=True).spec == f"{tmp_path}:w"


# --------------------------------------------------------------------------- #
# the argv handed to colima
# --------------------------------------------------------------------------- #


def test_no_mounts_means_the_historical_bare_start(home: Path) -> None:
    """Byte-identical to what `aisland docker start` always ran — the change is invisible without the flag."""
    assert DockerService()._start_argv([]) == ["colima", "start"]


def test_home_is_always_kept_writable_ahead_of_the_requested_mounts(home: Path, tmp_path: Path) -> None:
    """Measured: any ``--mount`` REPLACES colima's default ``$HOME`` mount, so it must be re-stated."""
    data = tmp_path / "store"
    data.mkdir()
    argv = DockerService()._start_argv([Mount(data, writable=False)])
    assert argv == ["colima", "start", "--mount", f"{home}:w", "--mount", f"{data}:r"]


def test_home_listed_by_the_user_is_not_duplicated(home: Path, tmp_path: Path) -> None:
    data = tmp_path / "store"
    data.mkdir()
    argv = DockerService()._start_argv([Mount(data, writable=False), Mount(home, writable=True)])
    assert argv.count(f"{home}:w") == 1
    assert argv == ["colima", "start", "--mount", f"{home}:w", "--mount", f"{data}:r"]


def test_a_read_only_home_from_the_user_is_honoured(home: Path) -> None:
    """The user asked for ``$HOME:r`` explicitly — do not silently promote it back to writable."""
    argv = DockerService()._start_argv([Mount(home, writable=False)])
    assert argv == ["colima", "start", "--mount", f"{home}:r"]


# --------------------------------------------------------------------------- #
# what the profile currently mounts
# --------------------------------------------------------------------------- #


def test_an_empty_mounts_list_is_colimas_default_home_writable(home: Path) -> None:
    assert DockerService().configured_mounts() == [Mount(home, writable=True)]


def test_a_missing_profile_reads_as_the_default_too(home: Path) -> None:
    (home / ".colima" / "default" / "colima.yaml").unlink()
    assert DockerService().configured_mounts() == [Mount(home, writable=True)]


def test_configured_mounts_are_read_from_the_profile(home: Path, tmp_path: Path) -> None:
    (home / ".colima" / "default" / "colima.yaml").write_text(
        "mounts:\n" f"  - location: {home}\n    writable: true\n" f"  - location: {tmp_path}\n    writable: false\n"
    )
    assert DockerService().configured_mounts() == [Mount(home, writable=True), Mount(tmp_path, writable=False)]


def test_a_writable_or_enclosing_mount_covers_a_read_only_request(home: Path, tmp_path: Path) -> None:
    configured = [Mount(home, writable=True), Mount(tmp_path, writable=False)]
    inside = tmp_path / "deeper"
    assert docker_mod.covers(configured, Mount(inside, writable=False))  # below a configured mount
    assert docker_mod.covers(configured, Mount(home / "x", writable=True))  # writable parent
    assert not docker_mod.covers(configured, Mount(tmp_path, writable=True))  # wants w, has r
    assert not docker_mod.covers(configured, Mount(tmp_path.parent, writable=False))  # above, not below


# --------------------------------------------------------------------------- #
# liveness
# --------------------------------------------------------------------------- #


def _colima_status(returncode: int, stderr: str) -> MagicMock:
    """colima 0.10.3 puts every status line on STDERR and nothing on stdout (measured)."""
    return MagicMock(returncode=returncode, stdout="", stderr=stderr)


def test_is_running_is_the_exit_code_not_a_stdout_grep() -> None:
    """The old ``"status: running" in stdout`` was always False: colima never prints that, and not on stdout."""
    running = _colima_status(0, 'time="…" level=info msg="colima is running using macOS Virtualization.Framework"\n')
    stopped = _colima_status(1, 'time="…" level=fatal msg="colima is not running"\n')
    with patch("subprocess.run", return_value=running):
        assert DockerService().is_running() is True
    with patch("subprocess.run", return_value=stopped):
        assert DockerService().is_running() is False


def test_is_running_is_false_without_a_colima_binary() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert DockerService().is_running() is False


def test_a_colima_status_that_never_answers_is_given_up_as_not_running() -> None:
    """A stuck VM's `colima status` must not hang `aisland services status`, the doctor or an MCP tool."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["colima", "status"], 30)) as run:
        assert DockerService().is_running() is False
    assert run.call_args.kwargs["timeout"] == probe.PROBE_SECONDS


# --------------------------------------------------------------------------- #
# launch
# --------------------------------------------------------------------------- #


def test_launch_starts_colima_with_the_mounts_when_it_is_stopped(home: Path, tmp_path: Path) -> None:
    data = tmp_path / "store"
    data.mkdir()
    svc = DockerService()
    with patch.object(DockerService, "is_running", return_value=False):
        with patch.object(DockerService, "configure_socket"):
            with patch("subprocess.check_call") as call_mock:
                svc.launch(mounts=[f"{data}:r"])
    call_mock.assert_called_once_with(["colima", "start", "--mount", f"{home}:w", "--mount", f"{data}:r"])


def test_launch_refuses_to_change_mounts_under_a_running_engine(home: Path, tmp_path: Path) -> None:
    """Mounts apply at VM boot; restarting the engine kills every container — that is the user's call."""
    data = tmp_path / "store"
    data.mkdir()
    svc = DockerService()
    with patch.object(DockerService, "is_running", return_value=True):
        with patch("subprocess.check_call") as call_mock:
            with pytest.raises(click.ClickException) as excinfo:
                svc.launch(mounts=[f"{data}:r"])
    call_mock.assert_not_called()
    assert str(data) in str(excinfo.value)
    assert f"aisland docker restart --mount {data}:r" in str(excinfo.value)


def test_launch_is_a_no_op_when_a_running_engine_already_has_the_mounts(home: Path, tmp_path: Path) -> None:
    (home / ".colima" / "default" / "colima.yaml").write_text(
        "mounts:\n" f"  - location: {home}\n    writable: true\n" f"  - location: {tmp_path}\n    writable: false\n"
    )
    svc = DockerService()
    with patch.object(DockerService, "is_running", return_value=True):
        with patch.object(DockerService, "configure_socket") as socket_mock:
            with patch("subprocess.check_call") as call_mock:
                svc.launch(mounts=[f"{tmp_path}:r"])
    call_mock.assert_not_called()
    socket_mock.assert_called_once()


def test_launch_without_mounts_on_a_running_engine_is_unchanged(home: Path) -> None:
    svc = DockerService()
    with patch.object(DockerService, "is_running", return_value=True):
        with patch.object(DockerService, "configure_socket") as socket_mock:
            with patch("subprocess.check_call") as call_mock:
                svc.launch()
    call_mock.assert_not_called()
    socket_mock.assert_called_once()


def test_launch_validates_every_mount_before_touching_colima(home: Path, tmp_path: Path) -> None:
    with patch.object(DockerService, "is_running", return_value=False):
        with patch("subprocess.check_call") as call_mock:
            with pytest.raises(click.ClickException):
                DockerService().launch(mounts=[str(tmp_path / "missing")])
    call_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_the_cli_passes_repeated_mount_flags_to_start_and_restart(tmp_path: Path) -> None:
    from aisland.cli import main

    seen: List[dict] = []
    with patch.object(DockerService, "launch", side_effect=lambda **kw: seen.append(kw)):
        with patch.object(DockerService, "stop"):
            runner = CliRunner()
            result = runner.invoke(main, ["docker", "start", "--mount", "/a:r", "--mount", "/b:w"])
            assert result.exit_code == 0, result.output
            result = runner.invoke(main, ["docker", "restart", "--mount", "/c"])
            assert result.exit_code == 0, result.output
    assert seen[0]["mounts"] == ("/a:r", "/b:w")
    assert seen[1]["mounts"] == ("/c",)


# --------------------------------------------------------------------------- #
# Linux: the system's own Docker Engine, checked, never managed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("system, kind", [("Linux", docker_mod.LinuxDockerService), ("Darwin", DockerService)])
def test_each_platform_gets_its_engine(monkeypatch: pytest.MonkeyPatch, system: str, kind: type) -> None:
    monkeypatch.setattr(docker_mod.platform, "system", lambda: system)
    service = docker_mod.docker_service()
    assert type(service) is kind and service.name == "docker"


def _linux(monkeypatch: pytest.MonkeyPatch, installed: bool, running: bool) -> List[List[str]]:
    """A Linux machine whose `docker` is (not) on PATH and whose engine does (not) answer `docker info`."""
    ran: List[List[str]] = []
    monkeypatch.setattr(docker_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(docker_mod.shutil, "which", lambda tool: f"/usr/bin/{tool}" if installed else None)

    def run(argv: List[str], **kwargs: object) -> MagicMock:
        ran.append(argv)
        if not installed:
            raise FileNotFoundError(argv[0])
        return MagicMock(returncode=0 if running else 1)

    monkeypatch.setattr(docker_mod.subprocess, "run", run)
    return ran


@pytest.mark.parametrize(
    "installed, running, message",
    [
        (
            False,
            False,
            "Error: Docker is not installed. Install Docker Engine: https://docs.docker.com/engine/install/",
        ),
        (True, False, "Error: the Docker engine is not running. Start it: sudo systemctl start docker"),
    ],
)
def test_on_linux_start_says_how_to_get_the_engine_and_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, installed: bool, running: bool, message: str
) -> None:
    from aisland.cli import main

    ran = _linux(monkeypatch, installed, running)
    result = CliRunner().invoke(main, ["docker", "start"])
    assert result.exit_code == 1
    assert message in result.output
    assert all(argv[:2] == ["docker", "info"] for argv in ran)  # never colima, never an install


def test_on_linux_a_running_engine_is_reported_and_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisland.cli import main

    ran = _linux(monkeypatch, installed=True, running=True)
    runner = CliRunner()
    result = runner.invoke(main, ["docker", "start"])
    assert result.exit_code == 0 and "Docker engine is running." in result.output
    result = runner.invoke(main, ["docker", "stop"])
    assert result.exit_code == 0 and "Docker engine left running" in result.output
    assert ran == [["docker", "info"]]


def test_on_linux_mount_and_reset_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisland.cli import main

    _linux(monkeypatch, installed=True, running=True)
    result = CliRunner().invoke(main, ["docker", "start", "--mount", "/data"])
    assert result.exit_code == 1 and "--mount shares a folder with Colima's VM on macOS" in result.output
    with pytest.raises(click.ClickException, match="aisland does not wipe the system's Docker engine"):
        docker_mod.LinuxDockerService().reset()


@pytest.mark.parametrize("installed, running, word", [(False, False, "Not Installed"), (True, False, "Stopped")])
def test_on_linux_status_and_info_read_the_engine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, installed: bool, running: bool, word: str
) -> None:
    _linux(monkeypatch, installed, running)
    service = docker_mod.LinuxDockerService()
    service.status()
    service.info()
    out = capsys.readouterr().out
    assert f"Docker (engine)  : {word}" in out and f"Status        : {word}" in out
