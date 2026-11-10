"""The orchestrator builds each seeded project one phase after the deepest of its workspace dependencies.

A job is named by its project's workspace folder; a requirement reaches that project by its PEP 503 name, whatever its
specifier or spelling. A project the orchestrator cannot place is built beside its dependency, before it is ready —
and every project's job, aisland's included, must be placed.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from aisland.ci.jenkins_job_config import build_phases
from tests.records import project_metadata


def _phases(projects: List[tuple], seeded: List[str]) -> dict:
    """``build_phases`` over ``(distribution, folder, requirements)`` in dependency order."""
    order = [project_metadata(name, Path("/ws") / folder, *deps) for name, folder, deps in projects]
    return build_phases(order, seeded)


def test_a_project_builds_one_phase_after_its_dependency() -> None:
    phases = _phases([("lib", "lib", []), ("app", "app", ["lib>=0.1"])], ["lib", "app"])
    assert phases == {0: ["lib"], 1: ["app"]}


@pytest.mark.parametrize(
    "spec", ["lib~=1.0", "lib!=0.2", "lib>0.1,<2", "lib[extra]>=0.1", "Lib_Core", "lib ; python_version>'3'"]
)
def test_every_specifier_and_spelling_reaches_the_project(spec: str) -> None:
    """`dep.split(">=")…` read `lib~=1.0` as a project of that whole name: the app was built beside its library."""
    name = "lib-core" if "Lib_Core" in spec else "lib"
    phases = _phases([(name, "lib", []), ("app", "app", [spec])], ["lib", "app"])
    assert phases == {0: ["lib"], 1: ["app"]}


def test_a_distribution_named_unlike_its_folder_is_found_by_its_folder() -> None:
    phases = _phases([("acme-tools", "tools", []), ("aisland", "aisland", ["acme-tools"])], ["tools", "aisland"])
    assert phases == {0: ["tools"], 1: ["aisland"]}


def test_a_requirement_outside_the_workspace_and_an_unseeded_project_are_left_out() -> None:
    phases = _phases([("lib", "lib", []), ("app", "app", ["requests>=2", "lib"])], ["app"])
    assert phases == {0: ["app"]}
