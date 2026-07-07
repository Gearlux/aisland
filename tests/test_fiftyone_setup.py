"""Tests for ``FiftyOneService.setup`` and supervisor registration.

``setup`` is the lazy-install hook that pulls ``fiftyone`` into the venv on the
first launch. We only want pip to run when fiftyone is genuinely missing —
re-installing on every ``aisland fiftyone start`` would block startup for
~30s while pip resolves a fully-installed package.

Process identity/liveness is owned by the process-compose supervisor
(:mod:`aisland.supervisor`); what ``launch()`` must guarantee is that the
service registers the right command, environment, and readiness probe.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from aisland.services.fiftyone import FiftyOneService


def test_setup_installs_when_fiftyone_missing() -> None:
    svc = FiftyOneService()
    with patch("importlib.util.find_spec", return_value=None) as find_spec_mock:
        with patch("subprocess.run") as run_mock:
            svc.setup()
    find_spec_mock.assert_called_once_with("fiftyone")
    run_mock.assert_called_once()
    # The first positional arg is the command list; assert it ends with pip install fiftyone.
    cmd = run_mock.call_args.args[0]
    assert cmd[-3:] == ["-m", "pip", "install"] or cmd[-1] == "fiftyone"


def test_setup_skips_install_when_fiftyone_present() -> None:
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
        patch("aisland.supervisor.register_process") as register_mock,
        patch("aisland.supervisor.apply") as apply_mock,
        patch(
            "aisland.supervisor.wait_for",
            return_value={"is_running": True, "pid": 4242, "status": "Running"},
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
