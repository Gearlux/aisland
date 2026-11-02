"""Shared test fixtures for aisland."""

import logging
import sys
from pathlib import Path
from typing import Iterator, Set

import pytest

from aisland import locations
from tests.workspace_layout import needs_workspace


@pytest.fixture(autouse=True)
def released_packages(monkeypatch: pytest.MonkeyPatch) -> Iterator[Set[str]]:
    """The distributions the CI scaffolder believes are RELEASED on PyPI — empty by default.

    The scaffolder asks PyPI which internal dependencies are released
    (``aisland.sources.released_on_pypi``); no test may reach the network, so the name the
    scaffolder imports is stubbed for every test. A test marks a dependency released by adding
    its distribution name to the returned set. The probe's own tests call the real function
    through :mod:`aisland.sources`, which this stub does not touch.
    """
    released: Set[str] = set()
    monkeypatch.setattr(
        "aisland.ci_scaffold.released_on_pypi",
        lambda distribution, owner: distribution in released,
        raising=False,
    )
    yield released


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every commit a test makes has an author, whatever this machine's git configuration says.

    GitHub's runners have no ``user.name``/``user.email``: a test committing through aisland (``wheels build
    --commit``) then failed with "Author identity unknown" there and passed here. git's own variables win over
    every configuration file, so the test sees one identity on every machine (``tests/test_git_identity.py``).
    """
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "aisland tests")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "tests@aisland.invalid")


@pytest.fixture(autouse=True)
def tunnel_state(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every test sees an EMPTY tunnel state, whatever this machine's own tunnel is doing.

    Two reasons, both about tests that never mention the tunnel: ``doctor.check_tunnel_relay``
    probes the relay when tunnel state exists, and no test may reach the network
    (see :func:`released_packages`); and a developer who has opened a workspace would otherwise
    have their own remembered list read into assertions about an empty one.
    """
    state = tmp_path_factory.mktemp("tunnel-state")
    monkeypatch.setenv("AISLAND_TUNNEL_HOME", str(state))
    return state


class RealWorkspaceReached(AssertionError):
    """A test reached the workspace aisland is checked out in without saying it needs it."""


def reads_the_real_workspace(node: pytest.Item) -> bool:
    """Whether a test says it works on the real workspace: :data:`tests.workspace_layout.needs_workspace` (the tests
    of its facts) or ``@pytest.mark.real_workspace``."""
    return node.get_closest_marker("real_workspace") is not None or any(
        mark == needs_workspace.mark for mark in node.iter_markers("skipif")
    )


@pytest.fixture(autouse=True)
def no_real_workspace(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the real workspace by accident.

    From a checkout every module asks :func:`aisland.locations.workspace_root` for the root, and it answers the workspace
    aisland is checked out in — the one a developer works in. A test that forgot to point it at its own folder ran
    ``aisland check`` there, whose ``isort .`` and ``black .`` rewrite files. Here the question fails the test,
    naming it; a test points the root at its folder as before (``monkeypatch.setattr(locations, "workspace_root",
    lambda: tmp_path)``, which replaces this), and one that checks the real workspace's facts says so with
    ``needs_workspace`` or ``@pytest.mark.real_workspace``.

    An ``aisland`` a test starts as a process of its own is not covered: from a checkout it finds the real root by its
    own place (``tests/test_no_real_workspace.py``)."""
    if reads_the_real_workspace(request.node):
        return

    def refuse() -> Path:
        raise RealWorkspaceReached(
            f"{request.node.nodeid} asked for the workspace root without pointing it at its own folder: "
            'monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path), or mark it '
            "@pytest.mark.real_workspace when it checks the real workspace"
        )

    monkeypatch.setattr(locations, "workspace_root", refuse)


@pytest.fixture
def empty_workspace(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace of the test's own with no project: an empty ``.gitmodules``, and the root pointed at it.

    For a test that reaches the root by accident (a command that lists the projects' services, a supervisor that reads
    ``<root>/.env``) and whose result must not depend on the workspace the developer works in."""
    root = tmp_path_factory.mktemp("empty-workspace")
    (root / ".gitmodules").write_text("")
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return root


@pytest.fixture(autouse=True)
def quiet_debug_log() -> Iterator[None]:
    """Every test leaves aisland's debug log off.

    ``aisland --debug`` adds its handler to the ``aisland`` logger for one run and takes it off when the run ends; a
    run that stops while its options are parsed never ends, and its handler would print every later test's debug
    records (``tests/test_debug_log.py``)."""
    yield
    base = sys.modules.get("aisland.cli._base")  # a test that never imported the CLI never turned the log on
    if base is not None:
        base._quiet_debug_log(logging.NOTSET)
