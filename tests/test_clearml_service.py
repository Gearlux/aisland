"""Tests for ``ClearMLService`` — upstream's compose stack, workspace-local state.

The property that drove the whole design: upstream hardcodes ``/opt/clearml`` host paths,
and aisland's rule is that a service's state lives in ``aisland/data/<service>/``. The
override that reconciles those is GENERATED from upstream's file, so these tests check the
generation rather than a hand-written path list — a version bump must not be able to leave
a mount behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
import yaml

from aisland.services.clearml import CLEARML_SERVER_VERSION, COMPOSE_SHA256, UPSTREAM_HOST_ROOT, ClearMLService

UPSTREAM = """\
services:
  apiserver:
    image: allegroai/clearml:latest
    volumes:
      - /opt/clearml/logs:/var/log/clearml
      - /opt/clearml/config:/opt/clearml/config
    ports:
      - "8008:8008"
  redis:
    image: redis:6.2
    volumes:
      - /opt/clearml/data/redis:/data
  webserver:
    image: allegroai/clearml:latest
    ports:
      - "8080:80"
"""


@pytest.fixture
def service(tmp_path: Path) -> ClearMLService:
    svc = ClearMLService()
    svc.service_data_dir = tmp_path / "clearml"
    svc.upstream_file = svc.service_data_dir / f"docker-compose-{CLEARML_SERVER_VERSION}.yml"
    svc.override_file = svc.service_data_dir / "aisland-override.yml"
    svc.state_dir = svc.service_data_dir / "state"
    svc.service_data_dir.mkdir(parents=True)
    svc.upstream_file.write_text(UPSTREAM)
    return svc


# --------------------------------------------------------------------------- #
# the override
# --------------------------------------------------------------------------- #


def test_every_upstream_host_path_is_relocated(service: ClearMLService) -> None:
    """No mount may be left pointing outside the workspace."""
    service._write_override(service.upstream_file)
    text = service.override_file.read_text()
    document = yaml.safe_load(text)

    mounts = [entry for spec in document["services"].values() for entry in spec["volumes"]]
    assert mounts, "the override produced no remaps at all"
    for entry in mounts:
        host = entry.split(":", 1)[0]
        assert not host.startswith(UPSTREAM_HOST_ROOT), entry
        assert str(service.state_dir) in host, entry


def test_the_container_targets_are_preserved(service: ClearMLService) -> None:
    """Compose merges volumes BY TARGET — a changed target would add a mount, not replace one."""
    service._write_override(service.upstream_file)
    document = yaml.safe_load(service.override_file.read_text())
    targets = {entry.split(":", 1)[1] for spec in document["services"].values() for entry in spec["volumes"]}
    assert targets == {"/var/log/clearml", "/opt/clearml/config", "/data"}


def test_services_without_bind_mounts_are_omitted(service: ClearMLService) -> None:
    """`webserver` only publishes ports; an empty override entry would be noise."""
    service._write_override(service.upstream_file)
    document = yaml.safe_load(service.override_file.read_text())
    assert set(document["services"]) == {"apiserver", "redis"}


def test_the_override_is_derived_not_hardcoded(service: ClearMLService) -> None:
    """A mount upstream adds later must be picked up without editing this project."""
    service.upstream_file.write_text(
        UPSTREAM + "  newthing:\n    image: x\n    volumes:\n      - /opt/clearml/data/newthing:/mnt/new\n"
    )
    service._write_override(service.upstream_file)
    document = yaml.safe_load(service.override_file.read_text())
    assert "newthing" in document["services"]
    assert str(service.state_dir / "data" / "newthing") in document["services"]["newthing"]["volumes"][0]


def test_the_state_directories_are_created(service: ClearMLService) -> None:
    """Docker would otherwise create them as root-owned, or fail on a read-only parent."""
    service._write_override(service.upstream_file)
    assert (service.state_dir / "logs").is_dir()
    assert (service.state_dir / "data" / "redis").is_dir()


def test_the_override_carries_generator_provenance(service: ClearMLService) -> None:
    header = service._write_override(service.upstream_file).read_text()
    assert "AUTO-GENERATED" in header
    assert "aisland clearml reset && aisland clearml start" in header


# --------------------------------------------------------------------------- #
# the pinned download
# --------------------------------------------------------------------------- #


def test_a_checksum_mismatch_aborts_and_writes_nothing(service: ClearMLService) -> None:
    """An unexpected topology must never reach disk where a later run would trust it."""
    service.upstream_file.unlink()
    response = MagicMock()
    response.read.return_value = b"services: {}\n"
    response.__enter__ = lambda s: s
    response.__exit__ = lambda *a: None
    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(click.ClickException, match="Checksum mismatch"):
            service._fetch_upstream()
    assert not service.upstream_file.exists()


def test_a_cached_file_is_not_refetched(service: ClearMLService) -> None:
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not download")):
        assert service._fetch_upstream() == service.upstream_file


def test_the_pin_is_a_release_tag_not_a_branch() -> None:
    """A digest pinned to a moving ref breaks on upstream's next commit."""
    assert CLEARML_SERVER_VERSION.startswith("v")
    assert CLEARML_SERVER_VERSION not in ("master", "main")
    assert len(COMPOSE_SHA256) == 64


# --------------------------------------------------------------------------- #
# preconditions
# --------------------------------------------------------------------------- #


def test_setup_refuses_to_install_the_backend(service: ClearMLService) -> None:
    with patch("importlib.util.find_spec", return_value=None):
        with patch("subprocess.run") as run_mock:
            with pytest.raises(click.ClickException, match="aisland tracker set clearml"):
                service.setup()
    run_mock.assert_not_called()


def test_setup_names_the_docker_start_command(service: ClearMLService) -> None:
    with patch("importlib.util.find_spec", return_value=MagicMock()):
        with patch("shutil.which", return_value="/usr/bin/docker"):
            with patch("subprocess.run", return_value=MagicMock(returncode=1)):
                with pytest.raises(click.ClickException, match="aisland docker start"):
                    service.setup()


def test_setup_reports_a_missing_docker_cli(service: ClearMLService) -> None:
    with patch("importlib.util.find_spec", return_value=MagicMock()):
        with patch("shutil.which", return_value=None):
            with pytest.raises(click.ClickException, match="brew install docker"):
                service.setup()


# --------------------------------------------------------------------------- #
# workspace wiring
# --------------------------------------------------------------------------- #


def test_the_hosts_are_published_to_the_env(service: ClearMLService, tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".env").write_text("DATA_ROOT=/data\n")
    with patch("aisland.manager.get_workspace_root", return_value=root):
        service._publish_env(9999)
    lines = (root / ".env").read_text().splitlines()
    assert "CLEARML_WEB_HOST=http://localhost:9999" in lines
    assert any(line.startswith("CLEARML_API_HOST=") for line in lines)
    assert "DATA_ROOT=/data" in lines  # neighbours untouched


def test_republishing_replaces_rather_than_appends(service: ClearMLService, tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".env").write_text("CLEARML_WEB_HOST=http://localhost:1111\n")
    with patch("aisland.manager.get_workspace_root", return_value=root):
        service._publish_env(2222)
    lines = (root / ".env").read_text().splitlines()
    assert lines.count("CLEARML_WEB_HOST=http://localhost:2222") == 1
    assert "CLEARML_WEB_HOST=http://localhost:1111" not in lines


def test_the_compose_invocation_layers_both_files(service: ClearMLService) -> None:
    argv = service._compose("up", "-d")
    assert argv[:3] == ["docker", "compose", "-f"]
    # Upstream FIRST, override second — compose applies later files over earlier ones.
    assert argv.index(str(service.upstream_file)) < argv.index(str(service.override_file))


def test_info_does_not_claim_the_supervisor_owns_it(service: ClearMLService, capsys: Any) -> None:
    """These containers are Docker's; naming process-compose sends an operator to the wrong tool."""
    service.info()
    out = capsys.readouterr().out
    assert "docker compose" in out
    assert "process-compose (" not in out
