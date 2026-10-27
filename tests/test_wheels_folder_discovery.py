"""`aisland wheels build` refuses a project whose setuptools discovery would take the `wheels/` folder for a package.

`wheels/` is written into the project. A setuptools project that leaves its packages to automatic discovery, in a
flat layout, then has two top-level packages, and setuptools stops the build ("Multiple top-level packages
discovered in a flat-layout"); one whose `[tool.setuptools.packages.find]` names no `include` ships `wheels/` inside
its own wheel. Both are refused before anything is built or written, with the fix setuptools itself recommends:
list the project's packages.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import click
import pytest

from aisland import wheels as w

_BUILD_SYSTEM = '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n'


def _project(root: Path, extra: str = "", *, package: Optional[str] = "app", build_system: str = _BUILD_SYSTEM) -> Path:
    """`<root>/app`: a pyproject (its build system, `[project]`, then `extra`) and, unless None, one package."""
    path = root / "app"
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(f'{build_system}[project]\nname = "app"\nversion = "0.1.0"\n{extra}')
    if package:
        (path / package).mkdir(parents=True)
        (path / package / "__init__.py").write_text("")
    return path


def _workspace(root: Path) -> Path:
    (root / "TASKS.md").write_text("")
    (root / ".gitmodules").write_text('[submodule "app"]\n\tpath = app\n\turl = git@github.com:acme/app.git\n')
    return root


def _problem(path: Path) -> Optional[str]:
    return w.wheels_folder_problem(w.workspace_projects(path.parent)["app"])


# --- refused ---------------------------------------------------------------------------------------------------------


def test_a_flat_project_without_a_package_list_is_refused_with_the_table_that_fixes_it(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _project(root)
    with pytest.raises(click.ClickException) as error:
        w.vendor_wheels(root, "app")
    assert error.value.message.splitlines() == [
        "app finds its packages automatically, so the wheels/ folder this command writes would become a second "
        "package and stop app from building.",
        "List its packages in app/pyproject.toml:",
        "    [tool.setuptools.packages.find]",
        '    include = ["app*"]',
    ]
    assert not (root / "app" / "wheels").exists()


def test_a_find_table_without_include_is_refused_because_it_would_ship_the_folder(tmp_path: Path) -> None:
    path = _project(_workspace(tmp_path), '[tool.setuptools.packages.find]\nwhere = ["."]\n')
    assert _problem(path) == (
        "app's [tool.setuptools.packages.find] takes every folder, so the wheels/ folder this command writes would be "
        "shipped inside app's wheel.\n"
        "Name its packages in app/pyproject.toml:\n"
        "    [tool.setuptools.packages.find]\n"
        '    include = ["app*"]'
    )


def test_a_project_without_a_build_system_table_is_built_by_setuptools_and_refused(tmp_path: Path) -> None:
    assert _problem(_project(_workspace(tmp_path), build_system="")) is not None


def test_a_project_with_no_package_folder_is_told_to_name_its_own(tmp_path: Path) -> None:
    problem = _problem(_project(_workspace(tmp_path), package=None))
    assert problem is not None and problem.endswith('include = ["your_package*"]')


def test_every_package_folder_is_named(tmp_path: Path) -> None:
    path = _project(_workspace(tmp_path))
    (path / "app_extra").mkdir()
    (path / "app_extra" / "__init__.py").write_text("")
    problem = _problem(path)
    assert problem is not None and problem.endswith('include = ["app*", "app_extra*"]')


# --- left alone ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        '[tool.setuptools.packages.find]\ninclude = ["app*"]\n',
        '[tool.setuptools.packages.find]\nexclude = ["wheels*"]\n',
        "[tool.setuptools.packages.find]\nnamespaces = false\n",
        '[tool.setuptools.packages.find]\nwhere = ["lib"]\n',
        '[tool.setuptools]\npackages = ["app"]\n',
        '[tool.setuptools]\npy-modules = ["app"]\n',
    ],
    ids=["include", "exclude", "no-namespaces", "other-where", "package-list", "py-modules"],
)
def test_a_project_that_declares_its_packages_is_left_alone(tmp_path: Path, extra: str) -> None:
    assert _problem(_project(_workspace(tmp_path), extra)) is None


def test_an_include_that_takes_the_wheels_folder_is_refused(tmp_path: Path) -> None:
    assert _problem(_project(_workspace(tmp_path), '[tool.setuptools.packages.find]\ninclude = ["*"]\n')) is not None


def test_a_src_layout_project_is_left_alone(tmp_path: Path) -> None:
    path = _project(_workspace(tmp_path), package="src/app")
    assert _problem(path) is None


def test_a_project_with_a_setup_py_is_left_alone(tmp_path: Path) -> None:
    path = _project(_workspace(tmp_path))
    (path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    assert _problem(path) is None


def test_another_build_backend_is_left_alone(tmp_path: Path) -> None:
    hatch = '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    assert _problem(_project(_workspace(tmp_path), build_system=hatch)) is None


# --- the advice works --------------------------------------------------------------------------------------------------


def _builds(path: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(out), str(path)],
        capture_output=True,
        text=True,
    )


def test_the_table_the_refusal_names_makes_the_project_build_with_its_wheels_folder(tmp_path: Path) -> None:
    """The refusal's own advice, pasted into the project, is what setuptools needs: the build passes and the wheel
    holds the package and not the folder."""
    path = _project(_workspace(tmp_path))
    (path / "wheels").mkdir()
    (path / "wheels" / "install.py").write_text("")
    refused = _builds(path, tmp_path / "out")
    assert "Multiple top-level packages discovered in a flat-layout" in refused.stderr
    problem = _problem(path)
    assert problem is not None
    table = "\n".join(line.strip() for line in problem.splitlines()[-2:])
    with (path / "pyproject.toml").open("a") as handle:
        handle.write(table + "\n")
    built = _builds(path, tmp_path / "out")
    assert built.returncode == 0, built.stderr
    wheel = next((tmp_path / "out").glob("app-*.whl"))
    names = subprocess.run(
        [sys.executable, "-m", "zipfile", "-l", str(wheel)], capture_output=True, text=True, check=True
    ).stdout
    assert "app/__init__.py" in names and "wheels/" not in names
