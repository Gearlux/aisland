"""``aisland.__version__`` is the installed distribution's version, read from its metadata.

The version is written ONCE, in ``pyproject.toml``. A typed-in ``__version__ = "..."`` is a second
copy that drifts: before this rule three workspace packages reported ``0.2.0`` while their
``pyproject.toml`` said ``0.1.0``.

A checkout the venv has no record of reads that ``pyproject.toml`` itself. Measured 2026-10-29: after the rename
``aisland-manager`` → ``aisland`` the venv had no ``aisland`` record until setup installed it, and setup writes
``workspace.toml``'s how-to before it installs — with the fallback ``0.0.0.dev0`` in it.
"""

import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from click.testing import CliRunner

import aisland
from aisland import locations
from aisland.cli import main


def test_version_is_the_installed_distribution_version() -> None:
    assert aisland.__version__ == version("aisland")


def test_the_command_says_its_version_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """`aisland --version` answers in a workspace and outside one (from a wheel, where every workspace command is
    refused): it is how a user, and the release's smoke test, see which aisland they run."""
    expected = f"aisland, version {version('aisland')}\n"
    assert CliRunner().invoke(main, ["--version"]).output == expected
    monkeypatch.setattr(locations, "workspace_found", lambda: False)
    result = CliRunner().invoke(main, ["--version"])
    assert (result.exit_code, result.output) == (0, expected)


# --- no install record: a fresh interpreter without site-packages imports a copy of the package -------------------


def _version_without_a_record(folder: Path) -> str:
    """The `__version__` of the package copied into `folder`, imported by a Python that has no installed
    distribution: `-S` leaves site-packages off the path, `-E` ignores `PYTHONPATH`, and the folder is the one it
    runs in."""
    shutil.copytree(Path(aisland.__file__).parent, folder / "aisland", ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        [sys.executable, "-S", "-E", "-c", "import aisland; print(aisland.__version__)"],
        cwd=folder,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_a_checkout_the_venv_has_no_record_of_says_its_pyprojects_version(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "aisland"\nversion = "9.8.7"\n')
    assert _version_without_a_record(tmp_path) == "9.8.7"


def test_a_package_without_a_record_or_a_checkout_falls_back(tmp_path: Path) -> None:
    """A wheel installed without its record (a `--bundle` holds every project's code under one name): no
    `pyproject.toml` is beside the package."""
    assert _version_without_a_record(tmp_path) == "0.0.0.dev0"


@pytest.mark.parametrize(
    "pyproject",
    ["[project\n", '[project]\nname = "aisland"\n', 'project = "aisland"\n'],
    ids=["not-toml", "no-version", "project-not-a-table"],
)
def test_a_pyproject_without_a_readable_version_falls_back_and_the_import_never_raises(
    tmp_path: Path, pyproject: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(pyproject)
    assert _version_without_a_record(tmp_path) == "0.0.0.dev0"
