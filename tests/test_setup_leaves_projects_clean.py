"""`aisland setup` leaves every project's `git status` as it found it.

Setup writes a `pyrightconfig.json` into each project, and the editable install leaves a `<name>.egg-info/` folder.
Both are kept out of git through the project's own `.git/info/exclude` — a list on this machine, never committed —
and no `.gitignore` is created or edited (user choice 2026-10-28, release review 3b). Before, setup created a
`.gitignore` in every project, and `aisland wheels build` of a project needing it was refused:

    Error: lib has changes no commit holds; its wheel would not contain them:
        ?? .gitignore
        ?? lib.egg-info/
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from aisland import git_exclude
from aisland.manager import _write_pyrightconfig


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _repository(path: Path, files: tuple = ("pyproject.toml",)) -> Path:
    path.mkdir(parents=True)
    for name in files:
        (path / name).write_text("x\n")
    _git(path, "init", "-q")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "first")
    return path


def _setup_files(project: Path) -> None:
    """What setup and the editable install leave in a project: the generated config and the install metadata, in a
    flat and in a src layout."""
    _write_pyrightconfig(project.parent, [{"name": project.name, "path": project}])
    (project / f"{project.name}.egg-info").mkdir()
    (project / f"{project.name}.egg-info" / "PKG-INFO").write_text("x\n")
    (project / "src" / f"{project.name}.egg-info").mkdir(parents=True)
    (project / "src" / f"{project.name}.egg-info" / "PKG-INFO").write_text("x\n")


def _exclude_lines(repository: Path) -> List[str]:
    return (repository / ".git" / "info" / "exclude").read_text().splitlines()


def test_a_project_of_its_own_repository_stays_clean_and_gets_no_gitignore(tmp_path: Path) -> None:
    lib = _repository(tmp_path / "lib")
    _setup_files(lib)
    assert _git(lib, "status", "--porcelain") == ""
    assert not (lib / ".gitignore").exists()
    assert _exclude_lines(lib)[-2:] == ["/pyrightconfig.json", "/**/*.egg-info/"]


def test_a_tracked_gitignore_is_left_as_it_is(tmp_path: Path) -> None:
    lib = _repository(tmp_path / "lib", ("pyproject.toml", ".gitignore"))
    _setup_files(lib)
    assert (lib / ".gitignore").read_text() == "x\n"
    assert _git(lib, "status", "--porcelain") == ""


def test_running_setup_again_writes_each_line_once(tmp_path: Path) -> None:
    lib = _repository(tmp_path / "lib")
    _setup_files(lib)
    _write_pyrightconfig(tmp_path, [{"name": "lib", "path": lib}])
    lines = _exclude_lines(lib)
    assert lines.count("/pyrightconfig.json") == 1 and lines.count("/**/*.egg-info/") == 1


def test_a_project_folder_of_a_root_repository_is_excluded_under_its_own_path(tmp_path: Path) -> None:
    """A workspace whose projects are folders of the root's repository (no submodules)."""
    root = _repository(tmp_path / "root", ("README.md",))
    lib = root / "lib"
    lib.mkdir()
    (lib / "pyproject.toml").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "lib")
    _setup_files(lib)
    assert _git(root, "status", "--porcelain", "--", "lib") == ""  # the root's own config is its .gitignore's
    assert _exclude_lines(root)[-2:] == ["/lib/pyrightconfig.json", "/lib/**/*.egg-info/"]
    (root / "other.egg-info").mkdir()
    (root / "other.egg-info" / "PKG-INFO").write_text("x\n")
    assert "other.egg-info" in _git(root, "status", "--porcelain")  # only the project's own


def test_a_project_in_no_repository_gets_nothing_and_no_error(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    _setup_files(lib)
    assert not (lib / ".gitignore").exists()


@pytest.mark.parametrize("existing", ["", "*.pyc", "*.pyc\n"])
def test_the_exclude_file_keeps_what_it_held(tmp_path: Path, existing: str) -> None:
    lib = _repository(tmp_path / "lib")
    (lib / ".git" / "info" / "exclude").write_text(existing)
    git_exclude.exclude(lib, ["pyrightconfig.json"])
    assert _exclude_lines(lib) == [*existing.splitlines(), "/pyrightconfig.json"]
