"""`aisland workspace init <folder>` and `aisland workspace export [file]`.

`init` makes a folder a workspace aisland runs in: it writes the starting files — never over one that exists —, runs
`git init`, adds each project of a `--from` workspace file and each `--add` repository as a submodule and installs
the workspace git aliases. A folder that already is a git repository is refused untouched, and so is a workspace file
that cannot be read, before anything is written. `export` writes the workspace file of an existing workspace.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Dict

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


_AUTHOR = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _remote(tmp_path: Path, name: str, branches: tuple = ()) -> Path:
    """A repository with one commit on its first branch and each of ``branches`` beside it."""
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    run = {**os.environ, **_AUTHOR}
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "first"]):
        subprocess.run(["git", *args], cwd=repo, env=run, check=True)
    for branch in branches:
        subprocess.run(["git", "branch", branch], cwd=repo, check=True)
    return repo


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


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
    for entry in (".aisland/", ".venv", ".env", "/project.bashrc", "/*.code-workspace", "/uv.toml"):
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


# --- the venv init finds -------------------------------------------------------------------------------------------


def test_from_the_folders_own_venv_init_asks_for_no_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "acme"
    (root / ".venv").mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    result = _init(tmp_path)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-3:] == ["Next:", f"    cd {root}", "    .venv/bin/aisland setup"]


def test_a_venv_kept_elsewhere_and_linked_in_is_ignored_by_git(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    (tmp_path / "venvs" / "acme").mkdir(parents=True)
    (root / ".venv").symlink_to(tmp_path / "venvs" / "acme")
    assert _init(tmp_path).exit_code == 0
    assert subprocess.run(["git", "check-ignore", "-q", ".venv"], cwd=root).returncode == 0


# --- init --from: a workspace file ---------------------------------------------------------------------------------


def _projects_file(tmp_path: Path, body: str) -> Path:
    return _write(tmp_path / "acme.workspace.toml", f"# A workspace.\n[projects]\n{body}")


def test_a_workspace_file_clones_each_project_on_its_branch(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    tools = _remote(tmp_path, "tools")
    listed = _projects_file(
        tmp_path, f'models = {{ url = "{models}", branch = "dev" }}\ntools = {{ url = "{tools}" }}\n'
    )
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert _git(root / "models", "branch", "--show-current") == "dev"
    assert _git(root / "tools", "rev-parse", "HEAD") == _git(tools, "rev-parse", "HEAD")
    assert _git(root, "config", "-f", ".gitmodules", "submodule.models.branch") == "dev"
    assert subprocess.run(["git", "config", "-f", ".gitmodules", "submodule.tools.branch"], cwd=root).returncode == 1
    assert "mypy_path = models:tools" in (root / "mypy.ini").read_text().splitlines()
    lines = result.output.splitlines()
    assert f"    models/                   submodule {models} (dev)" in lines
    assert f"    tools/                    submodule {tools}" in lines


def test_a_workspace_file_and_add_together_clone_the_files_projects_first(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    tools = _remote(tmp_path, "tools")
    listed = _projects_file(tmp_path, f'models = {{ url = "{models}" }}\n')
    result = _init(tmp_path, "--from", str(listed), "--add", str(tools))
    assert result.exit_code == 0, result.output
    assert [path for path, _url, _branch in workspace._submodules(tmp_path / "acme")] == ["models", "tools"]


def test_a_project_written_as_its_own_table_is_read_too(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    listed = _write(tmp_path / "w.toml", f'[projects.models]\nurl = "{models}"\nbranch = "dev"\n')
    assert workspace.read_workspace_file(listed) == [workspace.FileProject("models", str(models), "dev")]


@pytest.mark.parametrize(
    "body, message",
    [
        ("models = { url = ", "{file}: Invalid value (at line 3, column 18)"),
        ("", '{file} lists no project: write each one under [projects], as  <folder> = {{ url = "<address>" }}'),
        ('models = "x"', "{file}:3: project 'models' is not a table: write it as  models = {{ url = \"<address>\" }}"),
        ('models = { branch = "dev" }', "{file}:3: project 'models' has no url."),
        (
            'models = { url = "u", path = "p" }',
            "{file}:3: project 'models' has the unknown setting 'path' (a project " "takes url and branch).",
        ),
        (
            'models = { url = "u", branch = 2 }',
            "{file}:3: project 'models': branch must be text, as in " 'branch = "<text>".',
        ),
        (
            '"a/b" = { url = "u" }',
            "{file}:3: 'a/b' is not a folder name: a project's name is the folder it is " "cloned into.",
        ),
        (
            '".hidden" = { url = "u" }',
            "{file}:3: '.hidden' is not a folder name: a project's name is the folder it " "is cloned into.",
        ),
    ],
)
def test_a_workspace_file_that_cannot_be_read_stops_before_anything_is_written(
    tmp_path: Path, body: str, message: str
) -> None:
    listed = _projects_file(tmp_path, body + "\n")
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 1
    assert result.output == "Error: " + message.format(file=listed) + "\n"
    assert not (tmp_path / "acme").exists()


def test_a_workspace_file_without_a_projects_table_is_refused(tmp_path: Path) -> None:
    listed = _write(tmp_path / "w.toml", "# nothing here\n")
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 1
    assert result.output.startswith(f"Error: {listed} lists no project:")


def test_a_missing_workspace_file_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.toml"
    result = _init(tmp_path, "--from", str(missing))
    assert result.exit_code == 1
    assert result.output == f"Error: {missing} does not exist: name the workspace file to create the workspace from.\n"
    assert not (tmp_path / "acme").exists()


def test_a_projects_branch_that_does_not_exist_stops_with_its_url(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    listed = _projects_file(tmp_path, f'models = {{ url = "{models}", branch = "nope" }}\n')
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 1
    assert f"Error: could not add {models} as a submodule:" in result.output


# --- export --------------------------------------------------------------------------------------------------------


def _workspace(tmp_path: Path, projects: Dict[str, Path], branches: Dict[str, str]) -> Path:
    """A workspace made by `init --from`, its projects on the given branches."""
    lines = []
    for name, url in projects.items():
        branch = f', branch = "{branches[name]}"' if name in branches else ""
        lines.append(f'{name} = {{ url = "{url}"{branch} }}')
    listed = _write(tmp_path / "source.toml", "[projects]\n" + "\n".join(lines) + "\n")
    assert _init(tmp_path, "--from", str(listed)).exit_code == 0
    return tmp_path / "acme"


def _export(root: Path, target: Path) -> Result:
    return CliRunner().invoke(main, ["workspace", "export", str(target)], catch_exceptions=False)


@pytest.fixture
def at_root(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    """Point the CLI at the workspace a test made (from a checkout it is always the one aisland is in)."""
    holder: Dict[str, Path] = {}
    monkeypatch.setattr("aisland.cli.get_workspace_root", lambda: holder["root"])
    return holder


def test_export_writes_each_submodule_with_its_branch(tmp_path: Path, at_root: Dict[str, Path]) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    tools = _remote(tmp_path, "tools")
    root = at_root["root"] = _workspace(tmp_path, {"models": models, "tools": tools}, {"models": "dev"})
    result = CliRunner().invoke(main, ["workspace", "export"])
    assert result.exit_code == 0, result.output
    written = root / "acme.workspace.toml"
    assert tomllib.loads(written.read_text()) == {
        "projects": {"models": {"url": str(models), "branch": "dev"}, "tools": {"url": str(tools)}}
    }
    assert result.output.splitlines() == [
        f"==> Wrote {written}: 2 projects",
        f"    models                    {models} (dev)",
        f"    tools                     {tools}",
        "Edit the list as you like; the file's first lines say how to create the workspace from it.",
    ]


def test_an_exported_file_says_how_to_create_the_workspace_from_it(tmp_path: Path, at_root: Dict[str, Path]) -> None:
    root = at_root["root"] = _workspace(tmp_path, {"models": _remote(tmp_path, "models")}, {})
    target = tmp_path / "out" / "acme.workspace.toml"
    target.parent.mkdir()
    assert _export(root, target).exit_code == 0
    lines = target.read_text().splitlines()
    assert lines[1] == "# Written by: aisland workspace export (aisland/aisland/workspace.py)"
    assert "#        uv venv --seed --prompt acme .venv" in lines
    assert "#        python3 -m venv --prompt acme .venv" in lines
    assert "#        .venv/bin/pip install aisland-manager" in lines
    assert f"#        .venv/bin/pip install ./{workspace.aisland_wheel_name()}" in lines
    assert "#        .venv/bin/aisland workspace init . --from acme.workspace.toml" in lines


def test_an_exported_file_creates_the_same_workspace_again(tmp_path: Path, at_root: Dict[str, Path]) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    tools = _remote(tmp_path, "tools")
    root = at_root["root"] = _workspace(tmp_path, {"models": models, "tools": tools}, {"models": "dev"})
    target = tmp_path / "again.toml"
    assert _export(root, target).exit_code == 0
    again = CliRunner().invoke(main, ["workspace", "init", str(tmp_path / "again"), "--from", str(target)])
    assert again.exit_code == 0, again.output
    assert workspace._submodules(tmp_path / "again") == workspace._submodules(root)


def test_a_submodule_following_the_roots_branch_is_written_with_that_branch(
    tmp_path: Path, at_root: Dict[str, Path]
) -> None:
    root = at_root["root"] = _workspace(tmp_path, {"models": _remote(tmp_path, "models")}, {})
    _git(root, "config", "-f", ".gitmodules", "submodule.models.branch", ".")
    _git(root, "switch", "-q", "-c", "dev/main")
    target = tmp_path / "w.toml"
    assert _export(root, target).exit_code == 0
    assert tomllib.loads(target.read_text())["projects"]["models"]["branch"] == "dev/main"


def test_without_gitmodules_export_writes_the_clones(tmp_path: Path, at_root: Dict[str, Path]) -> None:
    alpha = _remote(tmp_path, "alpha", branches=("dev",))
    root = at_root["root"] = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "-b", "dev", str(alpha), str(root / "alpha")], check=True)
    _write(root / "gamma" / "pyproject.toml", "")  # a project, no clone
    subprocess.run(["git", "init", "-q", str(root / "delta")], check=True)  # a clone without origin
    (root / "docs").mkdir()  # no project, no clone: not mentioned
    target = tmp_path / "w.toml"
    result = _export(root, target)
    assert result.exit_code == 0, result.output
    assert tomllib.loads(target.read_text()) == {"projects": {"alpha": {"url": str(alpha), "branch": "dev"}}}
    assert result.output.splitlines()[1:4] == [
        f"    alpha                     {alpha} (dev)",
        "    delta                     left out: it has no origin remote",
        "    gamma                     left out: not a git clone",
    ]


def test_export_never_writes_over_a_file(tmp_path: Path, at_root: Dict[str, Path]) -> None:
    at_root["root"] = _workspace(tmp_path, {"models": _remote(tmp_path, "models")}, {})
    target = _write(tmp_path / "w.toml", "mine\n")
    result = CliRunner().invoke(main, ["workspace", "export", str(target)])
    assert result.exit_code == 1
    assert result.output == f"Error: {target} exists: name another file, or remove it first.\n"
    assert target.read_text() == "mine\n"


def test_a_workspace_with_nothing_to_export_is_refused(tmp_path: Path, at_root: Dict[str, Path]) -> None:
    root = at_root["root"] = tmp_path / "acme"
    root.mkdir()
    result = CliRunner().invoke(main, ["workspace", "export"])
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {root} has no project to write: no submodule in .gitmodules and no subfolder that is a git clone.\n"
    )
    assert not (root / "acme.workspace.toml").exists()


def test_export_outside_a_workspace_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aisland.locations.workspace_found", lambda: False)
    result = CliRunner().invoke(main, ["workspace", "export"])
    assert result.exit_code == 1
    assert result.output.startswith("Error: No workspace here:")


def test_a_long_project_name_keeps_a_space_before_its_address(tmp_path: Path, at_root: Dict[str, Path]) -> None:
    name = "a_project_name_longer_than_the_column"
    remote = _remote(tmp_path, name)
    root = at_root["root"] = _workspace(tmp_path, {name: remote}, {})
    result = _export(root, tmp_path / "w.toml")
    assert f"    {name} {remote}" in result.output.splitlines()
