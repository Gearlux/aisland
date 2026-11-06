"""The orchestrator job maps each pyproject name to the workspace folder whose job it runs.

A name that maps to no initialized job is silently left out of the orchestrator's build phases, so
every project's job — aisland's included — must be found from its pyproject name.
"""

from __future__ import annotations

import pytest

from aisland.ci.jenkins_job_config import pyproject_to_workspace_dir


def test_pyproject_to_workspace_dir_identity() -> None:
    """Most projects: pyproject name == workspace dir name."""
    initialized = {"confluid", "loggair", "matrainer"}
    assert pyproject_to_workspace_dir("confluid", initialized) == "confluid"
    assert pyproject_to_workspace_dir("matrainer", initialized) == "matrainer"


def test_pyproject_to_workspace_dir_dashed_pypi_name() -> None:
    """Dashed PyPI name -> dashless dir (the loggair / recordstream convention)."""
    initialized = {"loggair", "recordstream"}
    assert pyproject_to_workspace_dir("loggair", initialized) == "loggair"
    assert pyproject_to_workspace_dir("recordstream", initialized) == "recordstream"


def test_pyproject_to_workspace_dir_finds_aisland() -> None:
    """The orchestrator runs aisland's own job: without it aisland's suite is out of the workspace-wide CI gate."""
    initialized = {"aisland", "loggair"}
    assert pyproject_to_workspace_dir("aisland", initialized) == "aisland"


def test_pyproject_to_workspace_dir_unknown_passes_through() -> None:
    """An unknown name (e.g. an external dep) is returned as-is — the caller decides what to do."""
    initialized = {"loggair", "matrainer"}
    assert pyproject_to_workspace_dir("requests", initialized) == "requests"


@pytest.mark.parametrize(
    "pyproject_name,initialized,expected",
    [
        # Dashless only kicks in when the identity check fails.
        ("raidar-ultralytics", {"raidar-ultralytics"}, "raidar-ultralytics"),
    ],
)
def test_pyproject_to_workspace_dir_priority(pyproject_name: str, initialized: set, expected: str) -> None:
    """Identity match wins; the dashless rewrite is a fallback."""
    assert pyproject_to_workspace_dir(pyproject_name, initialized) == expected
