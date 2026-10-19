"""The wheel carries every file the package reads or runs, not only its modules.

setuptools puts only ``*.py`` files into a wheel unless ``[tool.setuptools.package-data]`` names
the others. A file the package reads at run time (the workspace-file template) or runs (the git
alias scripts) that is missing from the wheel works in the editable checkout and fails only once
aisland is installed from a wheel — so the wheel is built and its members checked here.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from typing import Dict, List, Optional

import pytest

from aisland import manager

PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT / "aisland"


def _package_files_that_are_not_modules() -> List[str]:
    """Every file of the package a wheel needs that is not a module, as its path inside the wheel."""
    return sorted(
        path.relative_to(PROJECT).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc"} and "__pycache__" not in path.parts
    )


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel from a copy of the checkout (a build in place leaves `build/` behind)."""
    source = tmp_path_factory.mktemp("source")
    for name in ("pyproject.toml", "setup.py", "project.bashrc"):
        shutil.copy(PROJECT / name, source / name)
    shutil.copytree(PACKAGE, source / "aisland", ignore=shutil.ignore_patterns("__pycache__"))
    dist = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(dist), str(source)],
        check=True,
        capture_output=True,
    )
    (built,) = dist.glob("*.whl")
    return built


@pytest.fixture(scope="module")
def wheel_members(wheel: Path) -> List[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def test_the_package_holds_the_templates_and_the_git_scripts() -> None:
    assert manager.CODE_WORKSPACE_TEMPLATE.parent == PACKAGE
    assert _package_files_that_are_not_modules() == [
        "aisland/code-workspace.json",
        "aisland/git/git_pull_all.sh",
        "aisland/git/setup_git.sh",
        "aisland/templates/vscode-init-bash.sh",
    ]


def test_the_wheel_carries_every_file_of_the_package(wheel_members: List[str]) -> None:
    missing = [name for name in _package_files_that_are_not_modules() if name not in wheel_members]
    assert missing == []


def test_the_wheel_carries_the_shell_init_beside_the_package(wheel_members: List[str]) -> None:
    """`project.bashrc` stays in the checkout's `aisland/` (its place there defines the workspace root), so the
    build copies it into the package for a wheel."""
    assert "aisland/project.bashrc" in wheel_members
    assert "aisland/project.bashrc" not in _package_files_that_are_not_modules()


# --- the wheel, run in a workspace that is not this one --------------------------------------------------------


@pytest.fixture(scope="module")
def installed(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheel unpacked into a folder of its own — a pure-Python wheel installs by unpacking."""
    site = tmp_path_factory.mktemp("site")
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    return site


def _run(installed: Path, cwd: Path, *args: str, env: Optional[Dict[str, str]] = None) -> CompletedProcess[str]:
    """This interpreter (for click and PyYAML) with the unpacked wheel first on the path, never this checkout."""
    environment = {k: v for k, v in os.environ.items() if k != "AISLAND_WORKSPACE"}
    environment.update({"PYTHONPATH": str(installed), **(env or {})})
    return subprocess.run([sys.executable, "-P", *args], cwd=cwd, env=environment, capture_output=True, text=True)


def _aisland(installed: Path, cwd: Path, *args: str) -> CompletedProcess[str]:
    return _run(installed, cwd, "-c", "from aisland.cli import main; main()", *args)


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


_REPORT = """
import json
from aisland import locations, manager, pytest_stats, supervisor
print(json.dumps({
    "package": locations.PACKAGE_DIR.as_posix(),
    "from_checkout": locations.FROM_CHECKOUT,
    "root": manager.get_workspace_root().as_posix(),
    "data": manager.DATA_DIR.as_posix(),
    "bin": manager.BIN_DIR.as_posix(),
    "supervisor": supervisor.DATA_DIR.as_posix(),
    "stats": pytest_stats.STATS_DIR.as_posix(),
    "template": manager.CODE_WORKSPACE_TEMPLATE.is_file(),
}))
"""


def test_from_the_wheel_the_workspace_is_where_it_runs_and_the_state_goes_into_it(installed: Path, acme: Path) -> None:
    (acme / "models").mkdir()
    result = _run(installed, acme / "models", "-c", _REPORT)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    root = acme.resolve().as_posix()
    assert report == {
        "package": (installed / "aisland").resolve().as_posix(),
        "from_checkout": False,
        "root": root,
        "data": f"{root}/.aisland/data",
        "bin": f"{root}/.aisland/bin",
        "supervisor": f"{root}/.aisland/data",
        "stats": f"{root}/.aisland/data/test-stats",
        "template": True,
    }


def test_from_the_wheel_outside_a_workspace_a_command_is_refused_with_the_way_out(
    installed: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _aisland(installed, plain, "status")
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"Error: No workspace here: {plain.resolve()} is not inside a git repository. Run aisland inside your "
        "workspace, set AISLAND_WORKSPACE to it, or create one with `aisland workspace init <folder>`.\n"
    )


def test_from_the_wheel_a_workspace_is_created_and_its_aliases_point_into_the_package(
    installed: Path, tmp_path: Path
) -> None:
    result = _aisland(installed, tmp_path, "workspace", "init", "acme")
    assert result.returncode == 0, result.stdout + result.stderr
    root = tmp_path / "acme"
    assert (root / ".git").is_dir()
    alias = subprocess.run(
        ["git", "config", "--local", "alias.pull-all"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    assert str((installed / "aisland" / "git" / "git_pull_all.sh").resolve()) in alias
