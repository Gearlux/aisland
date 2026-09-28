"""A `playwright` dev dependency is the declaration that its browser must be fetched.

The package alone cannot open a page; a test that drives one would `skip` wherever only pip
ran, and CI would go green while checking less. So listing `playwright` under `dev` — nothing
more — makes `aisland setup` fetch chromium after the editable install (`browser_install_command`)
and makes the scaffolded CI emit the same step (`test_jenkins_scaffold.py`).
"""

from pathlib import Path

from aisland.selection import PLAYWRIGHT_BROWSER, browser_install_command, dev_extra_names


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
