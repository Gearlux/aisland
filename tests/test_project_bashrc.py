"""`project.bashrc`: the workspace root and aisland's tool folder, found from where the real file sits.

From a checkout the root's `project.bashrc` links to `aisland/project.bashrc`; from a wheel `aisland setup` copies the
packaged file into `<workspace>/.aisland/` and links the root's entry to that copy. Either way the file's own folder
holds `bin/`, and its parent is the workspace root.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from aisland import locations, manager

BASHRC = Path(__file__).resolve().parents[1] / "project.bashrc"


def _source(root: Path) -> Tuple[str, str]:
    """`PROJECT_ROOT` and `PATH` after sourcing the root's entry from a clean environment."""
    env = {"HOME": str(root), "PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        ["bash", "-c", 'source ./project.bashrc && printf "%s\\n%s" "$PROJECT_ROOT" "$PATH"'],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    project_root, path = out.split("\n")
    return project_root, path


def _layout(root: Path, home: str) -> None:
    (root / home).mkdir(parents=True)
    (root / home / "project.bashrc").write_text(BASHRC.read_text())
    os.symlink(f"{home}/project.bashrc", root / "project.bashrc")


def test_from_a_checkout_the_root_and_aislands_bin_folder(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, "aisland")
    project_root, path = _source(root)
    assert project_root == str(root)
    assert path.split(":")[0] == f"{root}/aisland/bin"


def test_from_a_wheel_the_root_and_the_workspace_s_aisland_bin_folder(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    project_root, path = _source(root)
    assert project_root == str(root)
    assert path.split(":")[0] == f"{root}/.aisland/bin"


def test_setup_from_a_wheel_copies_the_packaged_file_and_links_the_root_to_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    package = tmp_path / "site" / "aisland"
    package.mkdir(parents=True)
    (package / "project.bashrc").write_text(BASHRC.read_text())
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setattr(locations, "PACKAGE_DIR", package)
    monkeypatch.setattr(manager, "BASE_DIR", root / ".aisland")
    manager._link_project_bashrc(root)
    assert (root / ".aisland" / "project.bashrc").read_text() == BASHRC.read_text()
    assert os.readlink(root / "project.bashrc") == ".aisland/project.bashrc"
    # A newer packaged file replaces the copy on the next setup: the copy is aisland's, never the user's.
    (package / "project.bashrc").write_text(BASHRC.read_text() + "# newer\n")
    manager._link_project_bashrc(root)
    assert (root / ".aisland" / "project.bashrc").read_text().endswith("# newer\n")
