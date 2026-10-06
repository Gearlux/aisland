"""Plain `pytest` is every project's fast run (root AGENTS.md "Two Test Runs", user ruling 2026-10-05).

A project that registers the `slow` marker must deselect those tests by default — `addopts` carrying `-m 'not slow'` —
so a bare `pytest` (an agent, `/ship`, a habit) never starts the slow run. `pytest -m slow` and `pytest -m ''` still
override it: a later `-m` wins. Checked over the real workspace, like `test_workspace_dependency_specs.py`, plus
synthetic projects naming each shape.
"""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import List, Optional, Sequence, Union

import pytest

from aisland.manager import discover_projects, get_workspace_root


def _default_markexpr(addopts: Union[str, Sequence[str], None]) -> Optional[str]:
    """The `-m` expression the configured `addopts` apply; None when they apply none.

    A string is split as pytest splits it (shlex); a TOML list is already the argument list.
    """
    args = shlex.split(addopts) if isinstance(addopts, str) else [str(a) for a in addopts or []]
    found: Optional[str] = None
    for index, arg in enumerate(args):
        if arg == "-m" and index + 1 < len(args):
            found = args[index + 1]
        elif arg.startswith("-m") and len(arg) > 2:
            found = arg[2:]
    return found


def fast_run_default_violations(workspace_root: Path) -> List[str]:
    """Every project that registers `slow` but whose plain `pytest` would run the slow tests too."""
    violations = []
    for pyproject in discover_projects(workspace_root):
        with pyproject.open("rb") as handle:
            ini = tomllib.load(handle).get("tool", {}).get("pytest", {}).get("ini_options", {})
        if not any(str(m).split(":", 1)[0].strip() == "slow" for m in ini.get("markers", [])):
            continue
        expression = _default_markexpr(ini.get("addopts", ""))
        if " ".join((expression or "").split()) != "not slow":
            violations.append(
                f"{pyproject}: registers the `slow` marker but plain `pytest` runs those tests too — "
                "add `addopts = \"-m 'not slow'\"` to [tool.pytest.ini_options]"
            )
    return sorted(violations)


def test_the_real_workspace_runs_the_fast_run_by_default() -> None:
    violations = fast_run_default_violations(get_workspace_root())
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "ini, violates",
    [
        ('markers = ["slow: the slow run"]\naddopts = "-m \'not slow\'"', False),
        ('markers = ["slow: the slow run"]\naddopts = "--cov-branch -m \'not slow\'"', False),
        ('markers = ["slow: the slow run"]\naddopts = ["-m", "not slow"]', False),
        ('markers = ["slow: the slow run"]', True),
        ('markers = ["slow: the slow run"]\naddopts = "--cov-branch"', True),
        ('markers = ["slow: the slow run"]\naddopts = "-m slow"', True),
        ('markers = ["integration: end to end"]', False),
        ("", False),
    ],
    ids=["string", "beside other options", "list", "no addopts", "other addopts", "slow by default", "no slow", "none"],
)
def test_a_project_that_registers_slow_must_deselect_it_by_default(tmp_path: Path, ini: str, violates: bool) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text(f'[project]\nname = "proj"\n\n[tool.pytest.ini_options]\n{ini}\n')
    violations = fast_run_default_violations(tmp_path)
    assert bool(violations) is violates
    if violates:
        assert str(project / "pyproject.toml") in violations[0]
