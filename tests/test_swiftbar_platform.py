"""Tests for the SwiftBar macOS-only platform guard.

SwiftBar is a macOS menu bar app; every action shells out to macOS-only
tools (``open``, ``defaults``, ``pkill``, ``pgrep``). The guard must key on
``platform.system() == "Darwin"`` — the old ``os.name != "posix"`` check let
Linux through (Linux is also posix) and then failed on ``open -a SwiftBar``,
while on Windows ``status()`` crashed with ``FileNotFoundError`` because
``pgrep`` does not exist there.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from aisland.services.swiftbar import SwiftBarService

GUARDED_ACTIONS = ["launch", "stop", "restart", "remove", "reset", "init_scripts"]


@pytest.mark.parametrize("system", ["Linux", "Windows"])
@pytest.mark.parametrize("action", GUARDED_ACTIONS)
def test_actions_skip_on_non_macos(system: str, action: str) -> None:
    """Every mutating action must no-op (no subprocess) off macOS."""
    svc = SwiftBarService()
    with (
        patch("aisland.services.swiftbar.platform.system", return_value=system),
        patch("aisland.services.swiftbar.subprocess.run") as run_mock,
        patch("aisland.services.swiftbar.subprocess.check_output") as check_output_mock,
    ):
        getattr(svc, action)()
    run_mock.assert_not_called()
    check_output_mock.assert_not_called()


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_status_reports_unsupported_on_non_macos(system: str, capsys: pytest.CaptureFixture) -> None:
    """``aisland services`` aggregates this line — it must not probe ``pgrep``."""
    svc = SwiftBarService()
    with (
        patch("aisland.services.swiftbar.platform.system", return_value=system),
        patch("aisland.services.swiftbar.subprocess.check_output") as check_output_mock,
    ):
        svc.status()
    check_output_mock.assert_not_called()
    assert "Not supported (macOS only)" in capsys.readouterr().out


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_info_reports_unsupported_on_non_macos(system: str, capsys: pytest.CaptureFixture) -> None:
    svc = SwiftBarService()
    with (
        patch("aisland.services.swiftbar.platform.system", return_value=system),
        patch("aisland.services.swiftbar.subprocess.check_output") as check_output_mock,
    ):
        svc.info()
    check_output_mock.assert_not_called()
    assert "Not supported (macOS only)" in capsys.readouterr().out


def test_launch_runs_on_macos() -> None:
    svc = SwiftBarService()
    with (
        patch("aisland.services.swiftbar.platform.system", return_value="Darwin"),
        patch("aisland.services.swiftbar.subprocess.run") as run_mock,
    ):
        svc.launch()
    run_mock.assert_called_once_with(["open", "-a", "SwiftBar"], check=True)


def test_stop_runs_on_macos() -> None:
    svc = SwiftBarService()
    with (
        patch("aisland.services.swiftbar.platform.system", return_value="Darwin"),
        patch("aisland.services.swiftbar.subprocess.run") as run_mock,
    ):
        svc.stop()
    run_mock.assert_called_once_with(["pkill", "SwiftBar"], check=False)
