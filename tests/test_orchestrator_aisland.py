"""Regression test: the orchestrator job MUST include aisland.

Pre-fix, ``_to_dir_name`` in ``_create_orchestrator_job`` tried
``aisland-manager`` and ``aislandmanager`` against the initialized-jobs set
(which contains the workspace-dir name ``aisland``) and silently dropped
aisland from the orchestrator's build phases.
"""

from __future__ import annotations

import pytest

from aisland.services.jenkins import pyproject_to_workspace_dir


def test_pyproject_to_workspace_dir_identity() -> None:
    """Most projects: pyproject name == workspace dir name."""
    initialized = {"confluid", "loggair", "marainer"}
    assert pyproject_to_workspace_dir("confluid", initialized) == "confluid"
    assert pyproject_to_workspace_dir("marainer", initialized) == "marainer"


def test_pyproject_to_workspace_dir_dashed_pypi_name() -> None:
    """Dashed PyPI name -> dashless dir (the loggair / sampleflux convention)."""
    initialized = {"loggair", "sampleflux"}
    assert pyproject_to_workspace_dir("loggair", initialized) == "loggair"
    assert pyproject_to_workspace_dir("sampleflux", initialized) == "sampleflux"


def test_pyproject_to_workspace_dir_handles_aisland_manager_suffix() -> None:
    """Regression: aisland's pyproject name is `aisland-manager` but the dir is `aisland`.

    Without this mapping, the orchestrator silently skipped aisland's own
    job, leaving its test suite out of the workspace-wide CI gate.
    """
    initialized = {"aisland", "loggair"}
    assert pyproject_to_workspace_dir("aisland-manager", initialized) == "aisland"


def test_pyproject_to_workspace_dir_unknown_passes_through() -> None:
    """An unknown name (e.g. an external dep) is returned as-is — the caller decides what to do."""
    initialized = {"loggair", "marainer"}
    assert pyproject_to_workspace_dir("requests", initialized) == "requests"


@pytest.mark.parametrize(
    "pyproject_name,initialized,expected",
    [
        # Pre-trim takes priority over dashless rewrite.
        ("aisland-manager", {"aisland", "aisland-manager"}, "aisland-manager"),
        # Dashless only kicks in when the identity check fails.
        ("signals-rfuav", {"signals-rfuav"}, "signals-rfuav"),
    ],
)
def test_pyproject_to_workspace_dir_priority(pyproject_name: str, initialized: set, expected: str) -> None:
    """Identity match wins; dashless and ``-manager`` trim are fallbacks."""
    assert pyproject_to_workspace_dir(pyproject_name, initialized) == expected
