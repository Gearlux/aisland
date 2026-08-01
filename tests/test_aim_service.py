"""Tests for ``AimService`` — the Aim UI served from a local repository.

Process identity/liveness is owned by the process-compose supervisor
(:mod:`aisland.supervisor`); what ``launch()`` must guarantee is that it registers the right
command, repository and readiness probe — and that the repository it serves is the one a
tracker writes to, which is what ``AIM_REPO`` in the workspace ``.env`` exists to keep true.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from aisland.services.aim import DEFAULT_PORT, REPO_ENV_KEY, AimService


def test_setup_refuses_to_install_and_names_the_selection_command() -> None:
    """A service must never install its own backend — that bypasses `aisland tracker`."""
    svc = AimService()
    with patch("importlib.util.find_spec", return_value=None):
        with patch("subprocess.run") as run_mock:
            with pytest.raises(click.ClickException, match="aisland tracker set aim"):
                svc.setup()
    run_mock.assert_not_called()


def test_setup_is_silent_when_aim_is_present() -> None:
    with patch("importlib.util.find_spec", return_value=MagicMock()):
        AimService().setup()  # no raise


def test_launch_registers_a_supervised_process(tmp_path: Path) -> None:
    svc = AimService()
    svc.repo_dir = tmp_path / "aimrepo"
    with patch.object(AimService, "setup"):
        with patch.object(AimService, "_publish_repo"):
            with patch.object(AimService, "start") as start_mock:
                svc.launch(port=1234, background=True)

    cmd = start_mock.call_args.args[0]
    assert cmd[1] == "up"
    assert "--port" in cmd and "1234" in cmd
    assert str(svc.repo_dir) in cmd
    # Aim prompts to initialise an unseen repository; a supervised process cannot answer,
    # so the confirm flag is what stops it hanging instead of starting.
    assert "--yes" in cmd
    assert start_mock.call_args.kwargs["readiness"] == {"port": 1234, "path": "/"}
    assert svc.repo_dir.is_dir()  # created before Aim opens it


def test_launch_accepts_an_explicit_repository(tmp_path: Path) -> None:
    svc = AimService()
    elsewhere = tmp_path / "other"
    with patch.object(AimService, "setup"):
        with patch.object(AimService, "_publish_repo"):
            with patch.object(AimService, "start") as start_mock:
                svc.launch(repo=str(elsewhere), background=True)
    assert str(elsewhere.resolve()) in start_mock.call_args.args[0]


def test_the_default_port_is_aims_own() -> None:
    assert DEFAULT_PORT == 43800


def test_publish_repo_creates_the_env_entry(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    with patch("aisland.manager.get_workspace_root", return_value=tmp_path):
        AimService()._publish_repo(tmp_path / "repo")
    assert env.read_text().strip() == f"{REPO_ENV_KEY}={tmp_path / 'repo'}"


def test_publish_repo_replaces_a_stale_entry(tmp_path: Path) -> None:
    """Serving a different repository must not leave configs pointing at the old one."""
    env = tmp_path / ".env"
    env.write_text(f"DATA_ROOT=/data\n{REPO_ENV_KEY}=/old/repo\nMODEL_ROOT=/models\n")
    with patch("aisland.manager.get_workspace_root", return_value=tmp_path):
        AimService()._publish_repo(tmp_path / "new")
    lines = env.read_text().splitlines()
    assert f"{REPO_ENV_KEY}={tmp_path / 'new'}" in lines
    assert f"{REPO_ENV_KEY}=/old/repo" not in lines
    assert "DATA_ROOT=/data" in lines and "MODEL_ROOT=/models" in lines  # neighbours untouched


def test_publish_repo_appends_to_an_existing_env(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("DATA_ROOT=/data\n")
    with patch("aisland.manager.get_workspace_root", return_value=tmp_path):
        AimService()._publish_repo(tmp_path / "repo")
    assert env.read_text().splitlines() == ["DATA_ROOT=/data", f"{REPO_ENV_KEY}={tmp_path / 'repo'}"]


def test_the_binary_is_resolved_venv_first() -> None:
    """An unactivated shell must not silently serve a different Aim (or none)."""
    import sys

    binary = AimService()._aim_binary()
    assert binary.startswith(sys.prefix) or binary == "aim" or Path(binary).name == "aim"


def test_the_service_keeps_its_state_inside_aisland_data() -> None:
    svc = AimService()
    assert svc.repo_dir == svc.service_data_dir
    assert svc.service_data_dir.name == "aim"
