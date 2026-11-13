"""``aisland.__version__`` is the installed distribution's version, read from its metadata.

The version is written ONCE, in ``pyproject.toml``. A typed-in ``__version__ = "..."`` is a second
copy that drifts: before this rule three workspace packages reported ``0.2.0`` while their
``pyproject.toml`` said ``0.1.0``.
"""

from importlib.metadata import version

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
