"""`project.bashrc`: the workspace root and aisland's tool folder, found from where the real file sits.

From a checkout the root's `project.bashrc` links to `aisland/project.bashrc`; from a wheel `aisland setup` copies the
packaged file into `<workspace>/.aisland/` and links the root's entry to that copy. Either way the file's own folder
holds `bin/`, and its parent is the workspace root.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from aisland import locations, manager

BASHRC = Path(__file__).resolve().parents[1] / "project.bashrc"


def _bash(root: Path, script: str, extra_env: Optional[Dict[str, str]] = None) -> str:
    """Stdout of `script` run by bash in `root` from a clean environment plus `extra_env`."""
    env = {"HOME": str(root), "PATH": "/usr/bin:/bin", **(extra_env or {})}
    return subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, check=True).stdout


def _source(root: Path) -> Tuple[str, str]:
    """`PROJECT_ROOT` and `PATH` after sourcing the root's entry from a clean environment."""
    out = _bash(root, 'source ./project.bashrc && printf "%s\\n%s" "$PROJECT_ROOT" "$PATH"')
    project_root, path = out.split("\n")
    return project_root, path


def _fake_venv(root: Path) -> None:
    """A `.venv` whose `activate` only records which venv was activated."""
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text(f'export VIRTUAL_ENV="{root}/.venv"\n')


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


def test_a_project_root_inherited_from_another_workspace_is_replaced_by_this_ones(tmp_path: Path) -> None:
    # A terminal opened from a shell that sourced another workspace inherits its PROJECT_ROOT; this workspace's
    # file still activates this workspace's venv.
    other = (tmp_path / "acme").resolve()
    _layout(other, "aisland")
    _fake_venv(other)
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    _fake_venv(root)
    out = _bash(
        root,
        'source ./project.bashrc && printf "%s\\n%s" "$PROJECT_ROOT" "$VIRTUAL_ENV"',
        {"PROJECT_ROOT": str(other), "VIRTUAL_ENV": f"{other}/.venv"},
    )
    assert out.split("\n") == [str(root), f"{root}/.venv"]


def test_the_venv_of_the_workspace_itself_is_activated_from_a_clean_environment(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, "aisland")
    _fake_venv(root)
    assert _bash(root, 'source ./project.bashrc && printf "%s" "$VIRTUAL_ENV"') == f"{root}/.venv"


@pytest.mark.parametrize("content", ["", "# only a comment\n# DATA_ROOT=/data\n", "# comment\n\n   \n"])
def test_an_env_file_without_settings_prints_nothing(tmp_path: Path, content: str) -> None:
    # A bare `export` lists every exported variable (`declare -x HOME=...`) on each terminal start.
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text(content)
    assert _bash(root, "source ./project.bashrc") == ""


def test_the_env_files_settings_are_exported_and_its_comments_skipped(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text("# DATA_ROOT=/commented\nDATA_ROOT=/data\n\nRUN_DIR=/runs\n")
    out = _bash(root, 'source ./project.bashrc && bash -c \'printf "%s\\n%s" "$DATA_ROOT" "$RUN_DIR"\'')
    assert out.split("\n") == ["/data", "/runs"]


def test_a_quoted_value_with_a_space_is_exported_whole_and_a_variable_in_a_value_expanded(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text('DATA_ROOT="/Volumes/Store/My Data"\nRUN_DIR=$HOME/runs\n')
    out = _bash(root, 'source ./project.bashrc && bash -c \'printf "%s\\n%s" "$DATA_ROOT" "$RUN_DIR"\'')
    assert out.split("\n") == ["/Volumes/Store/My Data", f"{root}/runs"]


def test_variables_set_after_the_env_file_are_not_exported(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text("DATA_ROOT=/data\n")
    out = _bash(root, 'source ./project.bashrc && LATER=1 && bash -c \'printf "%s" "${LATER:-unexported}"\'')
    assert out == "unexported"


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
