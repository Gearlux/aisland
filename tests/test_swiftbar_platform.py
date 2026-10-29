"""Tests for the SwiftBar macOS-only platform guard.

SwiftBar is a macOS menu bar app; every action shells out to macOS-only
tools (``open``, ``defaults``, ``pkill``, ``pgrep``). The guard must key on
``platform.system() == "Darwin"`` — the old ``os.name != "posix"`` check let
Linux through (Linux is also posix) and then failed on ``open -a SwiftBar``,
while on Windows ``status()`` crashed with ``FileNotFoundError`` because
``pgrep`` does not exist there.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional
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


@pytest.mark.parametrize(
    "origin, repository",
    [
        ("git@github.com:acme/models.git", "acme/models"),
        ("https://github.com/acme/models.git", "acme/models"),
        ("https://user@github.com/acme/models.git", "acme/models"),
        ("https://bitbucket.example.com/scm/acme/models.git", None),
    ],
)
def test_the_menu_names_a_projects_github_repository_from_every_spelling_of_its_origin(
    tmp_path: Path, origin: str, repository: Optional[str]
) -> None:
    """An https remote with a user before the host is no ssh remote: `https://user@github.com/acme/models` is
    `acme/models`, never `//user@github.com/acme/models`."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", origin], check=True)
    assert SwiftBarService()._get_github_repo(tmp_path) == repository


def test_a_folder_without_a_repository_or_an_origin_names_none(tmp_path: Path) -> None:
    assert SwiftBarService()._get_github_repo(tmp_path) is None
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert SwiftBarService()._get_github_repo(tmp_path) is None
