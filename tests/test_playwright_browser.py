"""A `playwright` dev dependency is the declaration that its browser must be fetched.

The package alone cannot open a page; a test that drives one would `skip` wherever only pip
ran, and CI would go green while checking less. So listing `playwright` under `dev` — nothing
more — makes `aisland setup` fetch chromium after the editable install (`browser_install_command`)
and makes the scaffolded CI emit the same step (`test_jenkins_scaffold.py`).
"""

from pathlib import Path
from typing import List, Tuple

import pytest

from aisland.setup import playwright_browser
from aisland.setup.playwright_browser import PLAYWRIGHT_BROWSER, browser_install_command
from aisland.workspace.projects import ProjectMetadata
from aisland.workspace.selection import dev_extra_names
from tests.records import project_metadata


class TestDevExtraNames:
    def test_every_spelling_yields_its_bare_name(self) -> None:
        data = {
            "project": {
                "optional-dependencies": {
                    "dev": [
                        "Playwright>=1.40.0,<2.0.0",
                        "pytest[extra]; python_version > '3.8'",
                        "torchsig @ git+https://github.com/TorchDSP/torchsig.git",
                        "not a requirement !!",
                    ]
                }
            }
        }
        assert dev_extra_names(data) == {"playwright", "pytest", "torchsig"}

    def test_no_dev_extra_is_no_names(self) -> None:
        assert dev_extra_names({}) == set()
        assert dev_extra_names({"project": {"optional-dependencies": {"docs": ["sphinx"]}}}) == set()


class TestBrowserInstallCommand:
    def test_a_declaring_project_gets_the_venv_playwright_install(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "0.0.0"\n\n'
            "[project.optional-dependencies]\n"
            'dev = ["playwright>=1.40.0,<2.0.0"]\n',
            encoding="utf-8",
        )
        venv_bin = tmp_path / ".venv" / "bin"
        assert browser_install_command(pyproject, venv_bin) == [
            str(venv_bin / "playwright"),
            "install",
            PLAYWRIGHT_BROWSER,
        ]

    def test_a_project_without_it_gets_none(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "0.0.0"\n\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=7.0.0,<9.0.0"]\n',
            encoding="utf-8",
        )
        assert browser_install_command(pyproject, tmp_path / ".venv" / "bin") is None

    def test_a_missing_pyproject_gets_none(self, tmp_path: Path) -> None:
        assert browser_install_command(tmp_path / "pyproject.toml", tmp_path / ".venv" / "bin") is None

    def test_playwright_as_a_runtime_dependency_is_not_a_dev_declaration(self, tmp_path: Path) -> None:
        """Only the dev extra declares: a runtime `playwright` is the project's own business."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "proj"\nversion = "0.0.0"\ndependencies = ["playwright>=1.40.0"]\n',
            encoding="utf-8",
        )
        assert browser_install_command(pyproject, tmp_path / ".venv" / "bin") is None


class TestSetupStep:
    """`aisland setup` fetches the browser for each project it installed that declares `playwright`."""

    @staticmethod
    def _project(tmp_path: Path, name: str, dev: str) -> ProjectMetadata:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.0.0"\n\n[project.optional-dependencies]\ndev = ["{dev}"]\n',
            encoding="utf-8",
        )
        return project_metadata(name, folder)

    def test_only_a_declaring_project_runs_the_install_in_its_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ran: List[Tuple[List[str], Path]] = []

        def check_call(cmd: List[str], cwd: Path) -> int:
            ran.append((cmd, cwd))
            return 0

        monkeypatch.setattr(playwright_browser.subprocess, "check_call", check_call)
        with_it = self._project(tmp_path, "viewer", "playwright>=1.40")
        without = self._project(tmp_path, "lib", "pytest")
        venv_bin = tmp_path / ".venv" / "bin"
        playwright_browser.install_browsers([with_it, without], venv_bin)
        assert ran == [([str(venv_bin / "playwright"), "install", PLAYWRIGHT_BROWSER], with_it["path"])]
        assert capsys.readouterr().out == f"==> Installing Playwright's {PLAYWRIGHT_BROWSER} for viewer...\n"

    def test_a_fetch_that_fails_is_reported_and_the_next_still_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: List[Path] = []

        def check_call(cmd: List[str], cwd: Path) -> int:
            ran.append(cwd)
            raise FileNotFoundError(cmd[0])

        reported: List[str] = []
        monkeypatch.setattr(playwright_browser.subprocess, "check_call", check_call)
        monkeypatch.setattr(playwright_browser.failures, "report", reported.append)
        first = self._project(tmp_path, "viewer", "playwright")
        second = self._project(tmp_path, "app", "playwright")
        venv_bin = tmp_path / ".venv" / "bin"
        playwright_browser.install_browsers([first, second], venv_bin)
        assert ran == [first["path"], second["path"]]
        assert reported == [
            f"  - Error: Failed to install Playwright's {PLAYWRIGHT_BROWSER} for {name}: {venv_bin / 'playwright'}"
            for name in ("viewer", "app")
        ]
