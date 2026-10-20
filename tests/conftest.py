"""Shared test fixtures for aisland."""

from pathlib import Path
from typing import Iterator, Set

import pytest


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
        "aisland.services.jenkins.released_on_pypi", lambda distribution: distribution in released, raising=False
    )
    yield released


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
