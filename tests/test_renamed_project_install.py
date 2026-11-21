"""`aisland setup` uninstalls what a project left in the venv under a name or a folder it no longer has.

Measured 2026-10-29: aisland was renamed `aisland-manager` → `aisland` in its own folder, and the curator project
folder and all. Setup's `uv pip install -e` added `aisland` beside `aisland-manager` — two editable distributions
owning one package and one `bin/aisland`, and uninstalling the old one afterwards deleted the shared script — and left
the curator's old name installed from a folder that was gone. The real uv, offline, runs every install here; the
failure paths use a `uv` script.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Set

import pytest

from aisland import failures
from aisland.setup import manager
from aisland.workspace.projects import ProjectMetadata, get_project_metadata

from .test_wheels import _repackaged_wheel

UV = str(Path(sys.executable).with_name("uv"))


@pytest.fixture(autouse=True)
def no_failures() -> Iterator[None]:
    failures.reset()
    yield
    failures.reset()


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with its own empty `.venv`, which every uv command here installs into: the real uv, offline, with a
    fresh cache, none of this machine's uv settings, and setuptools from a local folder in PyPI's place."""
    pypi = tmp_path / "pypi"
    pypi.mkdir()
    _repackaged_wheel(pypi, "setuptools")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    monkeypatch.setenv("UV_NO_INDEX", "1")
    monkeypatch.setenv("UV_FIND_LINKS", str(pypi))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    # uv's `pip` commands take VIRTUAL_ENV first: the workspace's venv, never the one running the tests.
    monkeypatch.setenv("VIRTUAL_ENV", str(ws / ".venv"))
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    subprocess.run([UV, "venv", "--quiet", "--python", sys.executable, str(ws / ".venv")], check=True)
    return ws


def _project(folder: Path, name: str, package: str = "tool") -> ProjectMetadata:
    """The project in `folder`, distribution `name`: the package `package` and a script of that name printing `ran`."""
    (folder / package).mkdir(parents=True, exist_ok=True)
    (folder / package / "__init__.py").write_text("")
    (folder / package / "cli.py").write_text("def main() -> None:\n    print('ran')\n")
    (folder / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=64"]\nbuild-backend = "setuptools.build_meta"\n\n'
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n\n'
        f'[project.scripts]\n{package} = "{package}.cli:main"\n\n'
        f'[tool.setuptools]\npackages = ["{package}"]\n'
    )
    return get_project_metadata(folder / "pyproject.toml")


def _install(ws: Path, *projects: ProjectMetadata) -> None:
    """Install `projects` editable the way setup does not: by hand, whatever the workspace's projects are."""
    for project in projects:
        subprocess.run([UV, "pip", "install", "--quiet", "-e", str(project["path"])], cwd=ws, check=True)


def _installed(ws: Path) -> Set[str]:
    listing = subprocess.run(
        [UV, "pip", "list", "--format", "json"], cwd=ws, check=True, capture_output=True, text=True
    )
    return {entry["name"] for entry in json.loads(listing.stdout)}


def test_a_project_renamed_in_its_folder_is_installed_under_the_new_name_only(
    ws: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert manager._run_installs(ws, UV, [_project(ws / "tool", "tool-manager")]) == ([], [])
    assert _installed(ws) == {"tool-manager"}

    assert manager._run_installs(ws, UV, [_project(ws / "tool", "tool")]) == ([], [])

    assert _installed(ws) == {"tool"}
    script = ws / ".venv" / "bin" / "tool"
    assert subprocess.run([str(script)], check=True, capture_output=True, text=True).stdout == "ran\n"
    out = capsys.readouterr().out
    assert f"==> Uninstalling tool-manager: installed from {ws / 'tool'}, whose project is now tool.\n" in out


def test_where_an_earlier_setup_installed_both_names_the_old_goes_and_the_script_stays(ws: Path) -> None:
    """The venv an older setup left after the rename: the new name installed beside the old one, both writing
    `bin/tool`. Uninstalling the old one deletes that script; the install after it must write it again."""
    _install(ws, _project(ws / "tool", "tool-manager"))
    tool = _project(ws / "tool", "tool")
    _install(ws, tool)
    assert _installed(ws) == {"tool", "tool-manager"}

    assert manager._run_installs(ws, UV, [tool]) == ([], [])

    assert _installed(ws) == {"tool"}
    script = ws / ".venv" / "bin" / "tool"
    assert subprocess.run([str(script)], check=True, capture_output=True, text=True).stdout == "ran\n"


def test_a_distribution_installed_from_a_workspace_folder_that_is_gone_is_uninstalled(
    ws: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert manager._run_installs(ws, UV, [_project(ws / "oldtool", "oldtool", package="oldtool")]) == ([], [])
    shutil.rmtree(ws / "oldtool")

    assert manager._run_installs(ws, UV, [_project(ws / "newtool", "newtool", package="newtool")]) == ([], [])

    assert _installed(ws) == {"newtool"}
    assert not (ws / ".venv" / "bin" / "oldtool").exists()
    assert f"==> Uninstalling oldtool: installed from {ws / 'oldtool'}, which no longer exists.\n" in (
        capsys.readouterr().out
    )


def test_a_distribution_installed_from_anywhere_else_stays(
    ws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Outside the workspace; a workspace folder still there that is no project of this install (a deactivated
    project: `git submodule deinit` leaves its folder, empty); a gone folder BELOW a workspace folder; and a project
    installed under the name it has."""
    far = _project(tmp_path / "elsewhere" / "far", "far", package="far")
    optional = _project(ws / "optional", "optional", package="optional")
    deep = _project(ws / "scratch" / "deep", "deep", package="deep")
    tool = _project(ws / "tool", "tool")
    _install(ws, far, optional, deep, tool)
    shutil.rmtree(ws / "optional")
    (ws / "optional").mkdir()
    shutil.rmtree(ws / "scratch" / "deep")

    assert manager._run_installs(ws, UV, [tool]) == ([], [])

    assert _installed(ws) == {"far", "optional", "deep", "tool"}
    assert "Uninstalling" not in capsys.readouterr().out


# --- the failure paths, with uv replaced by a script ----------------------------------------------------------------


def _fake_uv(folder: Path, listing: List[dict]) -> Path:
    """A `uv` whose `pip list` prints `listing`, whose `pip uninstall` fails, and which logs every command to `calls`."""
    script = folder / "uv"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{folder / "calls"}"\n'
        f"case \"$2\" in\n  list) echo '{json.dumps(listing)}' ;;\n  uninstall) exit 1 ;;\nesac\n"
    )
    script.chmod(0o755)
    return script


def test_a_failed_uninstall_of_the_old_name_is_that_projects_failed_install(tmp_path: Path) -> None:
    """Installing beside the old name is what this step exists to prevent: the project is not installed."""
    tool = _project(tmp_path / "tool", "tool")
    listing = [{"name": "tool-manager", "version": "0.1.0", "editable_project_location": str(tmp_path / "tool")}]
    uv = _fake_uv(tmp_path, listing)

    assert manager._run_installs(tmp_path, str(uv), [tool]) == (["tool"], [])

    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls == ["pip list --editable --format json", "pip uninstall tool-manager"]
    assert failures.reported() == 1


def test_a_failed_uninstall_of_a_gone_folders_distribution_is_reported_and_the_installs_go_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _project(tmp_path / "tool", "tool")
    listing = [{"name": "oldtool", "version": "0.1.0", "editable_project_location": str(tmp_path / "oldtool")}]
    uv = _fake_uv(tmp_path, listing)

    assert manager._run_installs(tmp_path, str(uv), [tool]) == ([], [])

    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[:2] == ["pip list --editable --format json", "pip uninstall oldtool"]
    assert calls[2].startswith("pip install -e ")
    assert "Error: Failed to uninstall oldtool" in capsys.readouterr().err
    assert failures.reported() == 1
