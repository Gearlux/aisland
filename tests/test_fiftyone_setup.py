"""Tests for ``FiftyOneService.setup`` and supervisor registration.

``setup`` checks that ``fiftyone`` is importable before a launch and, when it is not,
stops with the install command — it never installs anything itself (a plain ``pip``
install ignores the workspace ``uv.toml`` overrides).

Process identity/liveness is owned by the process-compose supervisor
(:mod:`aisland.supervisor`); what ``launch()`` must guarantee is that the
service registers the right command, environment, and readiness probe.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import click
import pytest

from aisland.services.fiftyone import FiftyOneService


def test_setup_stops_with_the_uv_install_command_when_fiftyone_is_missing() -> None:
    svc = FiftyOneService()
    with patch("importlib.util.find_spec", return_value=None) as find_spec_mock:
        with patch("subprocess.run") as run_mock:
            with pytest.raises(click.ClickException) as excinfo:
                svc.setup()
    find_spec_mock.assert_called_once_with("fiftyone")
    run_mock.assert_not_called()
    assert "uv pip install fiftyone" in excinfo.value.message


def test_setup_passes_when_fiftyone_present() -> None:
    svc = FiftyOneService()
    with patch("importlib.util.find_spec", return_value=MagicMock()):
        with patch("subprocess.run") as run_mock:
            svc.setup()
    run_mock.assert_not_called()


def test_launch_registers_supervised_process() -> None:
    """``launch`` must hand the supervisor the full FiftyOne process spec.

    The supervisor owns liveness and crash recovery, so a wrong command,
    missing database-dir env, or missing readiness probe here means the
    service either won't start or won't be observable via ``aisland status``.
    """
    svc = FiftyOneService()
    with (
        patch.object(FiftyOneService, "setup"),
        patch.object(FiftyOneService, "_update_env_file"),
        patch("aisland.supervisor.get_process", return_value=None),
        # `manager.start` pre-flights the readiness port against the REAL OS. Unstubbed,
        # this test passes or fails depending on what happens to hold 5151 on the
        # developer's machine — it failed here because VS Code's plugin helper had it,
        # and `start` returned before ever reaching register_process. The guard itself is
        # covered by the companion test below.
        patch("aisland.supervisor.port_in_use", return_value=None),
        patch("aisland.supervisor.register_process") as register_mock,
        patch("aisland.supervisor.apply") as apply_mock,
        # `manager.start` calls wait_for_outcome (NOT wait_for) and unpacks an
        # (outcome, state) tuple. Stubbing the wrong name still "succeeds" — both exist —
        # so the real 45s-timeout poll ran on every pass until this was corrected.
        patch(
            "aisland.supervisor.wait_for_outcome",
            return_value=("running", {"pid": 4242, "status": "Running"}),
        ),
    ):
        svc.launch(port=5151, background=True)

    register_mock.assert_called_once()
    _args, kwargs = register_mock.call_args
    name, cmd = _args
    assert name == "fiftyone"
    assert cmd[-6:] == ["fiftyone", "app", "launch", "--port", "5151", "--remote"]
    assert kwargs["readiness"] == {"port": 5151, "path": "/"}
    assert kwargs["env"]["FIFTYONE_DATABASE_DIR"] == str(svc.database_dir)
    assert kwargs["enabled"] is True
    apply_mock.assert_called_once()


def test_launch_refuses_a_port_held_by_an_unmanaged_process() -> None:
    """An occupied readiness port must abort BEFORE the process is registered.

    A service registered onto a taken port crash-loops through its whole restart budget
    with only a log-side error. This is the guard the test above stubs out, pinned here so
    stubbing it can never quietly disable it.
    """
    svc = FiftyOneService()
    with (
        patch.object(FiftyOneService, "setup"),
        patch.object(FiftyOneService, "_update_env_file"),
        patch("aisland.supervisor.get_process", return_value=None),
        patch("aisland.supervisor.port_in_use", return_value=23713),
        patch("aisland.supervisor.register_process") as register_mock,
        patch("aisland.supervisor.apply") as apply_mock,
    ):
        svc.launch(port=5151, background=True)

    register_mock.assert_not_called()
    apply_mock.assert_not_called()
