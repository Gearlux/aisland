"""Plain `pytest` is every project's fast run (aisland rules → "Two Test Runs", user ruling 2026-10-05).

A project that registers the `slow` marker must deselect those tests by default — `addopts` carrying `-m 'not slow'` —
so a bare `pytest` (an agent, `/ship`, a habit) never starts the slow run. `pytest -m slow` and `pytest -m ''` still
override it: a later `-m` wins. Checked over the real workspace, like `test_workspace_dependency_specs.py`, plus
synthetic projects naming each shape.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import List, Optional, Sequence, Union

import pytest

from aisland.projects import discover_projects, get_workspace_root


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


def test_aislands_own_pytest_reads_the_table_that_registers_slow(pytestconfig: pytest.Config) -> None:
    """pytest reads a `pytest.ini` instead of `[tool.pytest.ini_options]`, which the scaffold reads: asked itself,
    pytest knows `slow` and leaves a slow test out of a plain run."""
    slow = "tests/test_workspace_init.py::test_a_workspace_made_from_the_file_of_another_holds_the_same_projects"
    asked = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:aisland_test_stats", "-p", "no:cacheprovider", "--markers"],
        cwd=pytestconfig.rootpath,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "@pytest.mark.slow:" in asked.stdout, asked.stdout + asked.stderr
    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:aisland_test_stats",
            "-p",
            "no:cacheprovider",
            "--co",
            "-q",
            "tests/test_workspace_init.py",
        ],
        cwd=pytestconfig.rootpath,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    assert slow not in collected.stdout.splitlines() and "deselected" in collected.stdout


def test_aislands_own_fast_run_is_parallel(pytestconfig: pytest.Config) -> None:
    """Plain `pytest` runs over four worker processes: one process took 13 min on this machine against the
    workspace's 5-minute budget, most of it waiting on git, uv and builds (user pick 3, 2026-10-28). Asked itself,
    pytest names its workers; the plugin that gives them is in `dev`, which is all CI installs."""
    asked = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:aisland_test_stats",
            "-p",
            "no:cacheprovider",
            "tests/test_version.py",
        ],
        cwd=pytestconfig.rootpath,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert asked.returncode == 0, asked.stdout + asked.stderr
    assert "created: 4/4 workers" in asked.stdout, asked.stdout + asked.stderr
    dev = tomllib.loads((pytestconfig.rootpath / "pyproject.toml").read_text())["project"]["optional-dependencies"][
        "dev"
    ]
    assert any(spec.startswith("pytest-xdist") for spec in dev)
