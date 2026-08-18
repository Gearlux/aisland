"""Tests for ``IQEngineService`` — the IQEngine SigMF viewer over a local recordings directory.

The service is one ``docker run`` of a DIGEST-pinned image with the recordings directory bound
read-only at the path upstream documents. What must hold, and why (all measured 2026-08-17):

- the image reference carries the version AND the digest — ``:latest`` on ghcr is a 2023 build
  and ``:pre`` moves, so a tag alone would silently change what a start runs;
- the mount is read-only — IQEngine never writes to its local backend (its ``upload_blob`` is a
  no-op for ``local``; annotation edits land in the in-memory DB and vanish on restart);
- two silent-empty cases are DIAGNOSED instead of showing an empty table: a Colima VM that does
  not share the directory (the container sees 0 sidecars while the host has N — the mount came
  up empty, no error anywhere), and sidecars with no ``.sigmf-data`` beside them (IQEngine's
  local sync skips those: ``[SYNC] Data file … does not exist for metadata file``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Optional
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from aisland.services import iqengine as iqe
from aisland.services.iqengine import (
    CONTAINER_NAME,
    CONTAINER_RECORDINGS,
    DEFAULT_PORT,
    IQENGINE_DIGEST,
    IQENGINE_IMAGE,
    IQENGINE_PLATFORM,
    IQENGINE_VERSION,
    RECORDINGS_ENV_KEY,
    IQEngineService,
    image_ref,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """A fake workspace root: the ``.env`` the service publishes to lives here."""
    root = tmp_path / "ws"
    root.mkdir()
    with patch("aisland.supervisor._workspace_root", return_value=root):
        with patch("aisland.manager.get_workspace_root", return_value=root):
            yield root


@pytest.fixture
def service(tmp_path: Path, workspace: Path) -> IQEngineService:
    svc = IQEngineService()
    svc.service_data_dir = tmp_path / "iqengine"
    svc.service_data_dir.mkdir()
    return svc


def _sidecars(directory: Path, names: List[str], with_data: List[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.sigmf-meta").write_text("{}")
    for name in with_data:
        (directory / f"{name}.sigmf-data").write_bytes(b"\0" * 8)


# --------------------------------------------------------------------------- #
# the pin
# --------------------------------------------------------------------------- #


def test_the_image_is_pinned_by_version_and_digest() -> None:
    """A tag can move (`:pre` does; `:latest` is years stale) — the digest is what makes a start reproducible."""
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", IQENGINE_DIGEST)
    assert IQENGINE_VERSION and IQENGINE_VERSION not in ("latest", "pre")
    assert image_ref() == f"{IQENGINE_IMAGE}:{IQENGINE_VERSION}@{IQENGINE_DIGEST}"


# --------------------------------------------------------------------------- #
# the docker run
# --------------------------------------------------------------------------- #


def test_run_argv_binds_the_recordings_read_only_at_upstreams_path(service: IQEngineService, tmp_path: Path) -> None:
    recordings = tmp_path / "recs"
    argv = service._run_argv(recordings, port=4321)
    assert argv[:5] == ["docker", "run", "-d", "--name", CONTAINER_NAME]
    assert argv[argv.index("--platform") + 1] == IQENGINE_PLATFORM  # upstream builds one platform; say so
    assert "-p" in argv and f"4321:{DEFAULT_PORT}" in argv  # host port maps onto IQEngine's own
    assert "IN_MEMORY_DB=1" in argv  # no Mongo — a viewer, not a server
    assert f"IQENGINE_BACKEND_LOCAL_FILEPATH={CONTAINER_RECORDINGS}" in argv
    assert f"{recordings}:{CONTAINER_RECORDINGS}:ro" in argv
    assert argv[-1] == image_ref()


# --------------------------------------------------------------------------- #
# which directory
# --------------------------------------------------------------------------- #


def test_the_default_recordings_dir_is_inside_the_service_data_dir_and_gets_created(service: IQEngineService) -> None:
    resolved = service.resolve_recordings(None)
    assert resolved == service.service_data_dir / "recordings"
    assert resolved.is_dir()


def test_an_env_entry_overrides_the_default(service: IQEngineService, workspace: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (workspace / ".env").write_text(f"{RECORDINGS_ENV_KEY}={elsewhere}\n")
    assert service.resolve_recordings(None) == elsewhere.resolve()


def test_an_explicit_dir_wins_over_the_env(service: IQEngineService, workspace: Path, tmp_path: Path) -> None:
    (workspace / ".env").write_text(f"{RECORDINGS_ENV_KEY}={tmp_path / 'from-env'}\n")
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    assert service.resolve_recordings(str(explicit)) == explicit.resolve()


def test_an_explicit_missing_dir_is_refused_by_name(service: IQEngineService, tmp_path: Path) -> None:
    """Only the DEFAULT is created for you; a directory the user named must already exist."""
    missing = tmp_path / "nope"
    with pytest.raises(click.ClickException, match=re.escape(str(missing))):
        service.resolve_recordings(str(missing))


def test_a_tilde_and_a_symlink_are_resolved(
    service: IQEngineService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The VM resolves a shared symlink against ITS tree, so the service reasons about the real path."""
    home = tmp_path / "home"
    real = tmp_path / "real"
    home.mkdir()
    real.mkdir()
    (home / "link").symlink_to(real)
    monkeypatch.setenv("HOME", str(home))
    assert service.resolve_recordings("~/link") == real.resolve()


# --------------------------------------------------------------------------- #
# launch
# --------------------------------------------------------------------------- #


def _launch(service: IQEngineService, *, container_sidecars: int, recordings: Optional[str] = None) -> MagicMock:
    """Drive ``launch`` with Docker mocked out (the HOST count comes from real files); returns the ``check_call`` mock."""
    with patch.object(IQEngineService, "setup"):
        with patch.object(IQEngineService, "_container_state", return_value=None):
            with patch.object(IQEngineService, "_wait_for_api", return_value=True):
                with patch.object(IQEngineService, "_container_sidecar_count", return_value=container_sidecars):
                    with patch("aisland.supervisor.port_in_use", return_value=None):
                        with patch("subprocess.check_call") as call_mock:
                            service.launch(recordings=recordings)
    return call_mock


def test_launch_runs_the_pinned_image_and_publishes_the_directory(service: IQEngineService, workspace: Path) -> None:
    recordings = service.service_data_dir / "recordings"
    _sidecars(recordings, ["a", "b"], with_data=["a", "b"])
    call_mock = _launch(service, container_sidecars=2)
    argv = call_mock.call_args.args[0]
    assert argv[-1] == image_ref()
    assert f"{recordings}:{CONTAINER_RECORDINGS}:ro" in argv
    assert f"{RECORDINGS_ENV_KEY}={recordings}" in (workspace / ".env").read_text()


def test_launch_diagnoses_a_vm_that_does_not_share_the_directory(service: IQEngineService, tmp_path: Path) -> None:
    """Host has sidecars, container sees none: the bind mount came up EMPTY (Colima shares only $HOME by default)."""
    recordings = tmp_path / "vol" / "recs"
    _sidecars(recordings, ["a"], with_data=["a"])
    with pytest.raises(click.ClickException) as excinfo:
        _launch(service, container_sidecars=0, recordings=str(recordings))
    message = str(excinfo.value)
    assert str(recordings) in message
    assert f"aisland docker restart --mount {recordings}:r" in message


def test_launch_reports_sidecars_that_have_no_data_file(
    service: IQEngineService, capsys: pytest.CaptureFixture[str]
) -> None:
    """IQEngine's local sync skips a sidecar without `<name>.sigmf-data`; say so instead of showing an empty table."""
    recordings = service.service_data_dir / "recordings"
    _sidecars(recordings, ["a", "b", "c"], with_data=["a"])
    _launch(service, container_sidecars=3)
    out = capsys.readouterr().out
    assert "2 of 3" in out and ".sigmf-data" in out


def test_launch_is_quiet_when_every_sidecar_has_its_data(
    service: IQEngineService, capsys: pytest.CaptureFixture[str]
) -> None:
    recordings = service.service_data_dir / "recordings"
    _sidecars(recordings, ["a", "b"], with_data=["a", "b"])
    _launch(service, container_sidecars=2)
    out = capsys.readouterr().out
    assert "no .sigmf-data" not in out
    assert "2 recording(s)" in out


def test_launch_says_so_when_the_directory_is_empty(
    service: IQEngineService, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty directory is not the VM-share failure — do not send the user to `docker restart`."""
    _launch(service, container_sidecars=0)
    out = capsys.readouterr().out
    assert "no .sigmf-meta" in out
    assert "docker restart" not in out


def test_launch_does_not_start_a_second_container(service: IQEngineService) -> None:
    with patch.object(IQEngineService, "setup"):
        with patch.object(IQEngineService, "_container_state", return_value="running"):
            with patch("subprocess.check_call") as call_mock:
                service.launch()
    call_mock.assert_not_called()


def test_launch_replaces_a_stopped_leftover_container(service: IQEngineService) -> None:
    """A stopped container of the same name would make `docker run --name` fail; remove it first."""
    recordings = service.service_data_dir / "recordings"
    _sidecars(recordings, ["a"], with_data=["a"])
    with patch.object(IQEngineService, "setup"):
        with patch.object(IQEngineService, "_container_state", return_value="exited"):
            with patch.object(IQEngineService, "_wait_for_api", return_value=True):
                with patch.object(IQEngineService, "_container_sidecar_count", return_value=1):
                    with patch("aisland.supervisor.port_in_use", return_value=None):
                        with patch("subprocess.run") as run_mock:
                            with patch("subprocess.check_call"):
                                service.launch()
    removed = [c.args[0] for c in run_mock.call_args_list if c.args and c.args[0][:2] == ["docker", "rm"]]
    assert removed and CONTAINER_NAME in removed[0]


def test_launch_preflights_the_port(service: IQEngineService) -> None:
    with patch.object(IQEngineService, "setup"):
        with patch.object(IQEngineService, "_container_state", return_value=None):
            with patch("aisland.supervisor.port_in_use", return_value=4242):
                with patch("subprocess.check_call") as call_mock:
                    with pytest.raises(click.ClickException, match=str(DEFAULT_PORT)):
                        service.launch()
    call_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# setup / stop / status / reset
# --------------------------------------------------------------------------- #


def test_setup_needs_a_docker_cli_and_a_reachable_daemon() -> None:
    with patch("shutil.which", return_value=None):
        with pytest.raises(click.ClickException, match="docker"):
            IQEngineService().setup()
    with patch("shutil.which", return_value="/usr/bin/docker"):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            with pytest.raises(click.ClickException, match="aisland docker start"):
                IQEngineService().setup()


def test_stop_removes_the_container(service: IQEngineService) -> None:
    with patch.object(IQEngineService, "_container_state", return_value="running"):
        with patch("subprocess.run") as run_mock:
            service.stop()
    assert run_mock.call_args.args[0] == ["docker", "rm", "-f", CONTAINER_NAME]


def test_stop_is_a_no_op_without_a_container(service: IQEngineService) -> None:
    with patch.object(IQEngineService, "_container_state", return_value=None):
        with patch("subprocess.run") as run_mock:
            service.stop()
    run_mock.assert_not_called()


def test_container_state_reads_docker_ps(service: IQEngineService) -> None:
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="running\n")):
        assert service._container_state() == "running"
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
        assert service._container_state() is None


def test_reset_removes_the_container_and_image_but_never_the_recordings(service: IQEngineService) -> None:
    recordings = service.service_data_dir / "recordings"
    _sidecars(recordings, ["keep"], with_data=["keep"])
    with patch.object(IQEngineService, "_container_state", return_value="running"):
        with patch("click.confirm", return_value=True):
            with patch("subprocess.run") as run_mock:
                service.reset()
    argvs = [c.args[0] for c in run_mock.call_args_list]
    assert ["docker", "rm", "-f", CONTAINER_NAME] in argvs
    assert ["docker", "image", "rm", image_ref()] in argvs
    assert (recordings / "keep.sigmf-meta").exists()


def test_reset_does_nothing_when_declined(service: IQEngineService) -> None:
    with patch("click.confirm", return_value=False):
        with patch("subprocess.run") as run_mock:
            service.reset()
    run_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_the_cli_group_is_wired() -> None:
    from aisland.cli import main

    seen: List[dict] = []
    with patch.object(IQEngineService, "launch", side_effect=lambda **kw: seen.append(kw)):
        with patch.object(IQEngineService, "stop") as stop_mock:
            with patch.object(IQEngineService, "status") as status_mock:
                runner = CliRunner()
                result = runner.invoke(main, ["iqengine", "start", "--recordings", "/r", "--port", "3300"])
                assert result.exit_code == 0, result.output
                assert runner.invoke(main, ["iqengine", "stop"]).exit_code == 0
                assert runner.invoke(main, ["iqengine", "status"]).exit_code == 0
    assert seen[0]["recordings"] == "/r" and seen[0]["port"] == 3300
    stop_mock.assert_called_once()
    status_mock.assert_called_once()


def test_the_module_names_the_service_it_keeps_state_for(service: IQEngineService) -> None:
    assert iqe.IQEngineService().name == "iqengine"
