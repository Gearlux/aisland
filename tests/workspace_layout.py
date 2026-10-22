"""Whether this checkout of aisland sits inside a workspace, for the tests that check the REAL one.

A few tests assert facts of the workspace aisland is checked out in — its root holds `.gitmodules`, the MCP servers
it ships, the shell init it links. GitHub CI checks aisland out on its own, with no workspace above it, so those
tests carry :data:`needs_workspace` and are skipped there with its reason; inside a workspace (on a developer's
machine, on the local Jenkins) they run.
"""

from pathlib import Path

import pytest

AISLAND_PROJECT = Path(__file__).resolve().parents[1]


def inside_a_workspace(project: Path) -> bool:
    """A workspace holds aisland as a submodule: the folder above the checkout has a ``.gitmodules``."""
    return (project.parent / ".gitmodules").is_file()


needs_workspace = pytest.mark.skipif(
    not inside_a_workspace(AISLAND_PROJECT),
    reason="aisland is not checked out inside a workspace (no .gitmodules above it)",
)
