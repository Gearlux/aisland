"""`aisland workspace init <folder>`: a folder made into a workspace aisland runs in.

It writes the starting files — never over one that exists —, runs `git init`, adds each `--add` repository as a
submodule and installs the workspace git aliases. A folder that already is a git repository is refused untouched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from aisland import workspace
from aisland.cli import main

FILES = [".gitignore", ".env", "config.toml", "mypy.ini", "vscode-init-bash.sh"]


@pytest.fixture(autouse=True)
def local_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    """git refuses a submodule from a local path unless `protocol.file.allow` says otherwise (git >= 2.38)."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


@pytest.fixture
def models(tmp_path: Path) -> Path:
    """A repository with one commit, as `--add` would find on a server."""
    repo = tmp_path / "remote" / "models"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "models"\nversion = "0.1.0"\n')
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "first"]):
        subprocess.run(["git", *args], cwd=repo, env={**os.environ, **env}, check=True)
    return repo


def _init(tmp_path: Path, *args: str) -> Result:
    return CliRunner().invoke(main, ["workspace", "init", str(tmp_path / "acme"), *args])


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def test_a_new_folder_becomes_a_workspace(tmp_path: Path) -> None:
    result = _init(tmp_path)
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert (root / ".git").is_dir()
    # `.gitattributes` comes with the aliases: setup_git.sh registers the notebook-output filter there.
    assert sorted(p.name for p in root.iterdir() if p.name != ".git") == sorted([*FILES, ".gitattributes"])
    assert "pull-all" in _git(root, "config", "--local", "--get-regexp", r"^alias\.")
    assert result.output.splitlines()[0] == f"==> Created workspace acme in {root}"
    assert result.output.splitlines()[-4:] == [
        "Next:",
        f"    cd {root}",
        "    uv venv --seed .venv && .venv/bin/pip install <the aisland wheel>",
        "    .venv/bin/aisland setup",
    ]


def test_the_starting_files_are_what_aisland_setup_needs(tmp_path: Path) -> None:
    _init(tmp_path)
    root = tmp_path / "acme"
    ignored = (root / ".gitignore").read_text().splitlines()
    for entry in (".aisland/", ".venv/", ".env", "/project.bashrc", "/*.code-workspace", "/uv.toml"):
        assert entry in ignored
    assert "mypy_path =" in (root / "mypy.ini").read_text().splitlines()
    assert (root / "vscode-init-bash.sh").read_text() == workspace.VSCODE_INIT_BASH.read_text()
    for name in FILES:  # every file says where it came from (root AGENTS.md "Generator Provenance")
        assert "aisland workspace init" in (root / name).read_text().splitlines()[1]


def test_a_repository_added_is_a_submodule_and_on_mypys_path(tmp_path: Path, models: Path) -> None:
    result = _init(tmp_path, "--add", str(models))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert (root / "models" / "pyproject.toml").is_file()
    assert _git(root, "config", "-f", ".gitmodules", "submodule.models.url") == str(models)
    assert "mypy_path = models" in (root / "mypy.ini").read_text().splitlines()
    assert f"    models/                   submodule {models}" in result.output.splitlines()


def test_a_repository_that_cannot_be_added_stops_with_its_url(tmp_path: Path) -> None:
    missing = tmp_path / "remote" / "nothing"
    result = _init(tmp_path, "--add", str(missing))
    assert result.exit_code == 1
    assert f"Error: could not add {missing} as a submodule:" in result.output


def test_a_git_repository_is_refused_and_left_as_it_was(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    result = _init(tmp_path)
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {root} is already a git repository. `aisland workspace init` starts a new one; "
        "in an existing repository run `aisland setup`.\n"
    )
    assert sorted(p.name for p in root.iterdir()) == [".git"]


def test_a_file_that_exists_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    (root / ".env").write_text("DATA_ROOT=/data\n")
    result = _init(tmp_path)
    assert result.exit_code == 0, result.output
    assert (root / ".env").read_text() == "DATA_ROOT=/data\n"
    assert "    .env                      kept (it exists)" in result.output.splitlines()


def test_a_file_in_the_way_of_the_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "acme").write_text("")
    result = _init(tmp_path)
    assert result.exit_code == 1
    assert result.output == f"Error: {tmp_path / 'acme'} is a file, not a folder.\n"
