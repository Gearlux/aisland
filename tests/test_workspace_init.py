"""`aisland workspace init <folder>`.

`init` makes a folder a workspace aisland runs in: it writes the starting files — never over one that exists —, runs
`git init`, adds each project of a `--from` workspace.toml and each `--add` repository as a submodule, writes the new
workspace's `workspace.toml` (the `--from` file, copied in) and installs the workspace git aliases. A folder that
already is a git repository is refused untouched, and so is a workspace file that cannot be read, before anything is
written.
"""

from __future__ import annotations

import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Tuple

import pytest
from click.testing import CliRunner, Result

from aisland.cli import main
from aisland.setup import init as workspace
from aisland.workspace import file as workspace_file

FILES = [".gitignore", ".env", "mypy.ini", "vscode-init-bash.sh"]
HARNESS = [".agents", ".claude", "AGENTS.md", "CLAUDE.md", "GEMINI.md"]


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
    # `.gitattributes` comes with the aliases: setup_git.sh registers the notebook-output filter there; `.agents/`,
    # `.claude/` and the three rule files with the agent harness (aisland.setup.harness).
    assert sorted(p.name for p in root.iterdir() if p.name != ".git") == sorted([*FILES, ".gitattributes", *HARNESS])
    assert "pull-all" in _git(root, "config", "--local", "--get-regexp", r"^alias\.")
    assert result.stdout.splitlines()[0] == f"==> Created workspace acme in {root}"
    assert result.stdout.splitlines()[-4:] == [
        "Next:",
        f"    cd {root}",
        "    uv venv --seed .venv && .venv/bin/pip install aisland",
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
    for name in FILES:  # every file says where it came from (aisland rules → "Generator Provenance")
        assert "aisland workspace init" in (root / name).read_text().splitlines()[1]


def test_the_new_env_is_readable_by_its_owner_alone(tmp_path: Path) -> None:
    """It will hold the workspace's secrets; the other starting files are ordinary ones."""
    _init(tmp_path)
    root = tmp_path / "acme"
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    assert (root / "mypy.ini").stat().st_mode & 0o077 != 0


def test_a_new_workspace_gets_the_agent_commands_and_rules(tmp_path: Path) -> None:
    result = _init(tmp_path)
    root = tmp_path / "acme"
    assert (root / ".agents" / "workflows" / "commit-session.md").is_file()
    assert (root / ".claude" / "commands" / "ship.md").is_file()
    assert (root / "AGENTS.md").read_text().splitlines()[0] == "@.agents/rules.md"
    assert (root / "CLAUDE.md").resolve() == (root / "AGENTS.md").resolve()
    assert "    AGENTS.md                 written (imports the rules; CLAUDE.md and GEMINI.md link to it)" in (
        result.output.splitlines()
    )


def test_an_agents_md_of_its_own_is_kept_and_its_missing_import_named(tmp_path: Path) -> None:
    _write(tmp_path / "acme" / "AGENTS.md", "# ours\n")
    result = _init(tmp_path)
    assert (tmp_path / "acme" / "AGENTS.md").read_text() == "# ours\n"
    assert (
        '                              AGENTS.md does not import the harness\'s rules — add "@.agents/rules.md" '
        "as its first line" in result.output.splitlines()
    )


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
    assert f"Error: [1/1] nothing: could not add {missing} as a submodule:" in result.output


def test_an_existing_repository_is_set_up_in_place_and_its_files_kept(tmp_path: Path, models: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text("# ours\n")
    result = _init(tmp_path, "--add", str(models))
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"==> Set up workspace acme in {root}"
    assert lines[1] == "    .git/                     kept (an existing repository)"
    assert "    .gitignore                kept (it exists)" in lines
    assert (root / ".gitignore").read_text().startswith("# ours\n")
    assert (root / "models" / "pyproject.toml").is_file()
    assert (root / "mypy.ini").is_file() and (root / "AGENTS.md").is_file()
    assert "pull-all" in _git(root, "config", "--local", "--get-regexp", r"^alias\.")


def test_an_existing_repository_refuses_a_workspace_file_and_changes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    source = _workspace_toml(tmp_path, "")
    result = _init(tmp_path, "--from", str(source))
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {root} is already a git repository, with a workspace.toml of its own: add projects with --add.\n"
    )
    assert sorted(p.name for p in root.iterdir()) == [".git"]


def test_an_existing_repository_refuses_rewrite_beside_anything_else(tmp_path: Path) -> None:
    root = _existing_workspace(tmp_path)
    result = _init(tmp_path, "--rewrite", ".env", "--untracked")
    assert result.exit_code == 1
    assert result.output == (
        "Error: in an existing repository --rewrite writes its files and nothing else: run it on its own.\n"
    )
    assert (root / ".env").read_text() == "DATA_ROOT=/data\n"


def test_a_file_that_exists_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    (root / ".env").write_text("DATA_ROOT=/data\n")
    result = _init(tmp_path)
    assert result.exit_code == 0, result.output
    assert (root / ".env").read_text() == "DATA_ROOT=/data\n"
    assert "    .env                      kept (it exists)" in result.output.splitlines()


def _existing_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "vscode-init-bash.sh").write_text("# my old shim\n")
    (root / ".env").write_text("DATA_ROOT=/data\n")
    return root


def test_rewrite_in_an_existing_repository_replaces_the_file_keeps_a_bak_and_writes_nothing_else(
    tmp_path: Path,
) -> None:
    root = _existing_workspace(tmp_path)
    result = _init(tmp_path, "--rewrite", "vscode-init-bash.sh")
    assert result.exit_code == 0, result.output
    assert (root / "vscode-init-bash.sh").read_text() == workspace.VSCODE_INIT_BASH.read_text()
    assert (root / "vscode-init-bash.sh.bak").read_text() == "# my old shim\n"
    assert (root / ".env").read_text() == "DATA_ROOT=/data\n"
    assert sorted(p.name for p in root.iterdir()) == [".env", ".git", "vscode-init-bash.sh", "vscode-init-bash.sh.bak"]
    aliases = subprocess.run(["git", "config", "--local", "--get-regexp", r"^alias\."], cwd=root, capture_output=True)
    assert aliases.stdout == b""
    assert result.output.splitlines() == [
        f"==> Rewrote in {root}",
        "    vscode-init-bash.sh       rewritten (the old one kept as vscode-init-bash.sh.bak)",
    ]


def test_rewrite_of_several_files_and_of_one_that_is_missing(tmp_path: Path) -> None:
    root = _existing_workspace(tmp_path)
    result = _init(tmp_path, "--rewrite", ".env", "--rewrite", "mypy.ini")
    assert result.exit_code == 0, result.output
    assert (root / ".env.bak").read_text() == "DATA_ROOT=/data\n"
    assert (root / ".env").read_text() == workspace._ENV
    assert (root / "mypy.ini").read_text() == workspace._mypy_ini([])
    assert not (root / "mypy.ini.bak").exists()
    assert result.output.splitlines()[1:] == [
        "    .env                      rewritten (the old one kept as .env.bak)",
        "    mypy.ini                  written",
    ]


def test_rewrite_refuses_when_a_bak_is_in_the_way_and_changes_nothing(tmp_path: Path) -> None:
    root = _existing_workspace(tmp_path)
    (root / ".env.bak").write_text("older\n")
    result = _init(tmp_path, "--rewrite", "vscode-init-bash.sh", "--rewrite", ".env")
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {root / '.env.bak'} exists: move it away first, then rewrite .env (its current copy is kept there).\n"
    )
    assert (root / "vscode-init-bash.sh").read_text() == "# my old shim\n"
    assert (root / ".env").read_text() == "DATA_ROOT=/data\n"
    assert (root / ".env.bak").read_text() == "older\n"


def test_rewrite_of_a_file_init_does_not_write_is_refused_naming_the_ones_it_does(tmp_path: Path) -> None:
    _existing_workspace(tmp_path)
    result = _init(tmp_path, "--rewrite", "project.bashrc")
    assert result.exit_code == 2
    assert "'project.bashrc' is not one of '.gitignore', '.env', 'mypy.ini', 'vscode-init-bash.sh'" in result.output


def test_rewrite_in_a_new_folder_writes_over_the_file_and_makes_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    (root / ".env").write_text("DATA_ROOT=/data\n")
    result = _init(tmp_path, "--rewrite", ".env")
    assert result.exit_code == 0, result.output
    assert (root / ".git").is_dir()
    assert (root / ".env").read_text() == workspace._ENV
    assert (root / ".env.bak").read_text() == "DATA_ROOT=/data\n"
    assert "    .env                      rewritten (the old one kept as .env.bak)" in result.output.splitlines()


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


# --- init --from: a workspace.toml --------------------------------------------------------------------------------


def _workspace_toml(tmp_path: Path, body: str) -> Path:
    return _write(tmp_path / "source" / "workspace.toml", body)


def _project(name: str, url: Path | str, branch: str = "", path: str = "") -> str:
    lines = [f'[project."{name}"]', *([f'path = "{path}"'] if path else []), f'url = "{url}"']
    return "\n".join(lines + ([f'branch = "{branch}"'] if branch else [])) + "\n\n"


@pytest.mark.slow
def test_a_workspace_file_clones_each_project_on_its_branch(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    tools = _remote(tmp_path, "tools")
    listed = _workspace_toml(tmp_path, _project("models", models, "dev") + _project("tools", tools))
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
    assert "    workspace.toml            the projects, how to start another machine from them" in lines


@pytest.mark.slow
def test_the_file_becomes_the_new_workspaces_own(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    body = '[workspace]\nframework = ["torch"]\n\n' + _project("models", models, "dev")
    listed = _workspace_toml(tmp_path, body)
    assert _init(tmp_path, "--from", str(listed)).exit_code == 0
    made = workspace_file.load(tmp_path / "acme")
    assert made.settings == {"framework": ["torch"]}
    assert [(p.name, p.path, p.url, p.branch) for p in made.projects.values()] == [
        ("models", "models", str(models), "dev")
    ]


@pytest.mark.slow
def test_a_workspace_file_and_add_together_clone_the_files_projects_first(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    tools = _remote(tmp_path, "tools")
    listed = _workspace_toml(tmp_path, _project("models", models))
    result = _init(tmp_path, "--from", str(listed), "--add", str(tools))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert [path for path, _url, _branch in workspace_file.submodules(root)] == ["models", "tools"]
    assert list(workspace_file.load(root).projects) == ["models", "tools"]  # the added one is written in too


@pytest.mark.slow
def test_init_without_a_file_writes_one_listing_the_added_projects(tmp_path: Path) -> None:
    tools = _remote(tmp_path, "tools")
    assert _init(tmp_path, "--add", str(tools)).exit_code == 0
    project = workspace_file.load(tmp_path / "acme").projects["tools"]
    assert (project.path, project.url) == ("tools", str(tools))


def test_a_new_workspace_without_projects_has_no_file_and_says_none(tmp_path: Path) -> None:
    """`workspace.toml` lists the projects: with none there is no file, and the output names none."""
    result = _init(tmp_path)
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "acme" / "workspace.toml").exists()
    assert "workspace.toml" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "body, message",
    [
        ('[project."models"]\nurl = \n', "{file}: Invalid value (at line 2, column 7)"),
        ('[projects]\nmodels = { url = "u" }\n', "{file}:1: [projects] is the old workspace file"),
        ('[project."models"]\nbranch = "dev"\n', "{file}:1: project 'models' has no url"),
        ('[project."models"]\nurl = "u"\npath = "m/x"\n', "{file}:1: project 'models': path 'm/x' is not a folder"),
    ],
)
def test_a_workspace_file_that_cannot_be_read_stops_before_anything_is_written(
    tmp_path: Path, body: str, message: str
) -> None:
    listed = _workspace_toml(tmp_path, body)
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 1
    assert result.output.startswith("Error: " + message.format(file=listed))
    assert not (tmp_path / "acme").exists()


def test_a_missing_workspace_file_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.toml"
    result = _init(tmp_path, "--from", str(missing))
    assert result.exit_code == 1
    assert result.output == f"Error: {missing} does not exist: name the workspace.toml to create the workspace from.\n"
    assert not (tmp_path / "acme").exists()


@pytest.mark.slow
def test_a_project_with_a_path_is_cloned_into_that_folder(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    listed = _workspace_toml(tmp_path, _project("models", models, "dev", path="m"))
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert _git(root / "m", "branch", "--show-current") == "dev"
    assert not (root / "models").exists()
    assert not (root / "m").is_symlink()
    assert f"    m/                        submodule {models} (dev)" in result.output.splitlines()
    assert workspace_file.load(root).projects["models"].path == "m"


def test_a_projects_branch_that_does_not_exist_stops_with_its_url(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    listed = _workspace_toml(tmp_path, _project("models", models, "nope"))
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 1
    assert f"Error: [1/1] models: could not add {models} as a submodule:" in result.output


@pytest.mark.slow
def test_a_workspace_made_from_the_file_of_another_holds_the_same_projects(tmp_path: Path) -> None:
    """What `export` used to check: the file one workspace keeps creates the same workspace again."""
    models = _remote(tmp_path, "models", branches=("dev",))
    tools = _remote(tmp_path, "tools")
    listed = _workspace_toml(tmp_path, _project("models", models, "dev", path="m") + _project("tools", tools))
    assert _init(tmp_path, "--from", str(listed)).exit_code == 0
    first = tmp_path / "acme"
    again = CliRunner().invoke(
        main, ["workspace", "init", str(tmp_path / "again"), "--from", str(first / "workspace.toml")]
    )
    assert again.exit_code == 0, again.output
    assert workspace_file.submodules(tmp_path / "again") == workspace_file.submodules(first)
    assert workspace_file.load(tmp_path / "again").projects == workspace_file.load(first).projects


def test_there_is_no_export_command(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["workspace", "export"])
    assert result.exit_code == 2
    assert "No such command 'export'" in result.output


def test_the_repository_name_comes_from_the_address() -> None:
    assert workspace_file.repository_name("https://host/scm/team/machine_learning.git") == "machine_learning"
    assert workspace_file.repository_name("git@github.com:Gearlux/aisland.git") == "aisland"
    assert workspace_file.repository_name("/srv/git/models/") == "models"
    assert workspace_file.repository_name("https://host/a b.git") is None


# --- --local and --untracked -------------------------------------------------------------------------------------


@pytest.fixture
def author(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity `--local`'s first commit is made under, whatever machine runs the suite."""
    for key, value in _AUTHOR.items():
        monkeypatch.setenv(key, value)


def _status(root: Path) -> list:
    # Not stripped: the first line's leading space is part of its status.
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout.splitlines()


def _move(project: Path) -> None:
    (project / "f.txt").write_text("new\n")
    _git(project, "add", "f.txt")
    _git(project, "commit", "-q", "-m", "second")


@pytest.mark.slow
def test_a_local_workspace_tracks_only_its_projects_and_has_nothing_to_commit(
    tmp_path: Path, models: Path, author: None
) -> None:
    result = _init(tmp_path, "--local", "--add", str(models))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert _status(root) == []
    assert _git(root, "ls-files").splitlines() == [".gitmodules", "models"]
    assert _git(root, "log", "--format=%s").splitlines() == ["chore: the workspace's projects (models)"]
    assert _git(root, "config", "--local", "--type=bool", "aisland.workspace.local") == "true"
    assert "/*" in (root / ".gitignore").read_text().splitlines()
    lines = result.output.splitlines()
    assert "    .git/                     git init (local: it tracks only the projects)" in lines
    assert "    first commit              .gitmodules, models" in lines
    # --local alone still records the projects' commits: the root shows a project that moved.
    _move(root / "models")
    assert _status(root) == [" M models"]


def test_untracked_hides_a_projects_new_commits_and_writes_it_down(tmp_path: Path, models: Path, author: None) -> None:
    result = _init(tmp_path, "--local", "--untracked", "--add", str(models))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert _git(root, "config", "-f", ".gitmodules", "submodule.models.ignore") == "all"
    assert workspace_file.load(root).settings["track"] is False  # the projects to come follow it too
    assert "    models                    untracked: the root records none of its commits" in result.output.splitlines()
    assert _status(root) == []
    _move(root / "models")
    assert _status(root) == []


@pytest.mark.slow
def test_untracked_in_a_shared_root_leaves_the_files_for_its_first_commit(tmp_path: Path, models: Path) -> None:
    result = _init(tmp_path, "--untracked", "--add", str(models))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert _git(root, "config", "-f", ".gitmodules", "submodule.models.ignore") == "all"
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=root, capture_output=True).returncode


def test_untracked_in_an_existing_repository_untracks_its_submodules(tmp_path: Path, models: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "submodule", "add", "-q", str(models), "models"], cwd=root, check=True, capture_output=True)
    result = _init(tmp_path, "--untracked")
    assert result.exit_code == 0, result.output
    assert _git(root, "config", "-f", ".gitmodules", "submodule.models.ignore") == "all"
    assert workspace_file.load(root).settings["track"] is False  # the projects to come follow it too


@pytest.mark.slow
def test_a_local_workspace_without_projects_makes_no_commit(tmp_path: Path, author: None) -> None:
    result = _init(tmp_path, "--local")
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert _status(root) == []
    assert subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=root, capture_output=True).returncode
    assert not any(line.startswith("    first commit") for line in result.output.splitlines())


@pytest.mark.slow
def test_a_first_commit_git_refuses_is_said_and_the_workspace_still_made(
    tmp_path: Path, models: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in _AUTHOR:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-identity"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "true")
    result = _init(tmp_path, "--local", "--add", str(models))
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("    first commit"))
    assert row.startswith("    first commit              not made: ")
    assert (tmp_path / "acme" / "models" / "pyproject.toml").is_file()


@pytest.mark.slow
def test_a_plain_workspace_records_its_projects_commits_as_before(tmp_path: Path, models: Path) -> None:
    _init(tmp_path, "--add", str(models))
    root = tmp_path / "acme"
    ignore = subprocess.run(["git", "config", "-f", ".gitmodules", "submodule.models.ignore"], cwd=root)
    assert ignore.returncode == 1
    assert "/*" not in (root / ".gitignore").read_text().splitlines()
    marker = subprocess.run(["git", "config", "--local", "aisland.workspace.local"], cwd=root, capture_output=True)
    assert marker.stdout == b""
    assert "track" not in workspace_file.load(root).projects["models"].others


def test_local_in_an_existing_repository_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    result = _init(tmp_path, "--local")
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {root} is already a git repository: --local makes a new one that only this machine has.\n"
    )
    assert sorted(p.name for p in root.iterdir()) == [".git"]


@pytest.mark.slow
def test_rewrite_of_the_gitignore_in_a_local_workspace_keeps_it_local(tmp_path: Path, author: None) -> None:
    _init(tmp_path, "--local")
    root = tmp_path / "acme"
    (root / ".gitignore").write_text("# mine\n")
    result = _init(tmp_path, "--rewrite", ".gitignore")
    assert result.exit_code == 0, result.output
    assert "/*" in (root / ".gitignore").read_text().splitlines()


def test_the_from_files_name_names_the_new_workspace(tmp_path: Path) -> None:
    listed = _write(tmp_path / "listed" / "workspace.toml", '[workspace]\nname = "studio"\n')
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == f"==> Created workspace studio in {tmp_path / 'acme'}"


# --- Progress: what init says while it works (stderr), the report after it (stdout) ---


def _progress(result: Result) -> list:
    """The progress lines on stderr, each clone's time written as `T`."""
    return [re.sub(r"done in \d+\.\d s$", "done in T s", line) for line in result.stderr.splitlines()]


@pytest.mark.slow
def test_each_project_is_said_as_its_clone_starts_and_ends(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", branches=("dev",))
    tools = _remote(tmp_path, "tools")
    listed = _workspace_toml(tmp_path, _project("models", models, "dev") + _project("tools", tools))
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 0, result.output
    assert _progress(result) == [
        f"==> Creating workspace in {tmp_path / 'acme'}",
        f"[1/2] models  cloning {models} (dev) …",
        "[1/2] models  done in T s",
        f"[2/2] tools   cloning {tools} …",
        "[2/2] tools   done in T s",
        "      writing the starting files, the git aliases and the agent rules …",
    ]


@pytest.mark.slow
def test_an_added_repository_is_said_by_its_repository_name_after_the_files_projects(
    tmp_path: Path, models: Path
) -> None:
    tools = _remote(tmp_path, "tools")
    listed = _workspace_toml(tmp_path, _project("tools", tools))
    result = _init(tmp_path, "--from", str(listed), "--add", str(models))
    assert result.exit_code == 0, result.output
    assert _progress(result)[1:5] == [
        f"[1/2] tools   cloning {tools} …",
        "[1/2] tools   done in T s",
        f"[2/2] models  cloning {models} …",
        "[2/2] models  done in T s",
    ]


@pytest.mark.slow
def test_an_optional_project_is_said_to_be_skipped_and_not_counted(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    viewer = _remote(tmp_path, "viewer")
    body = _project("viewer", viewer) + "active = false\n\n" + _project("models", models)
    result = _init(tmp_path, "--from", str(_workspace_toml(tmp_path, body)))
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "acme" / "viewer").exists()
    assert _progress(result)[1:4] == [
        "[–]   viewer  skipped (optional: aisland setup asks)",
        f"[1/1] models  cloning {models} …",
        "[1/1] models  done in T s",
    ]


def test_a_failed_clone_is_named_by_its_place_and_the_ones_after_it_are_not_started(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    tools = _remote(tmp_path, "tools")
    body = _project("models", models, "nope") + _project("tools", tools)
    result = _init(tmp_path, "--from", str(_workspace_toml(tmp_path, body)))
    assert result.exit_code == 1
    assert _progress(result)[1] == f"[1/2] models  cloning {models} (nope) …"
    assert _progress(result)[2].startswith(f"Error: [1/2] models: could not add {models} as a submodule: fatal: ")
    assert "tools" not in result.stderr


@pytest.mark.slow
def test_the_report_on_stdout_holds_no_progress(tmp_path: Path, models: Path) -> None:
    result = _init(tmp_path, "--add", str(models))
    assert result.exit_code == 0, result.output
    root = tmp_path / "acme"
    assert result.stdout.splitlines()[:3] == [
        f"==> Created workspace acme in {root}",
        "    .git/                     git init",
        f"    models/                   submodule {models}",
    ]
    assert "cloning" not in result.stdout and "done in" not in result.stdout


@pytest.mark.slow
def test_without_a_terminal_gits_progress_stays_out(tmp_path: Path, models: Path) -> None:
    result = _init(tmp_path, "--add", f"file://{models}")
    assert result.exit_code == 0, result.output
    assert "\r" not in result.output and "%" not in result.output


def _on_a_terminal(cwd: Path, *args: str) -> Tuple[int, str, str]:
    """Run `aisland` with its stderr on a pseudo-terminal and its stdout on a pipe: (exit code, stderr, stdout)."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-c", "from aisland.cli import main; main()", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=slave,
    )
    os.close(slave)
    seen = b""
    deadline = time.monotonic() + 60
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"aisland did not finish within 60 s; it wrote: {seen!r}"
        ready, _, _ = select.select([master], [], [], left)
        if not ready:
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:  # the terminal closed with the process (EIO)
            break
        if not chunk:
            break
        seen += chunk
    os.close(master)
    assert process.stdout is not None
    stdout = process.stdout.read().decode()
    return process.wait(timeout=60), seen.decode().replace("\r\n", "\n"), stdout


@pytest.mark.slow
def test_on_a_terminal_gits_progress_is_drawn_on_one_line_and_cleared(tmp_path: Path, models: Path) -> None:
    url = f"file://{models}"
    code, terminal, stdout = _on_a_terminal(tmp_path, "workspace", "init", str(tmp_path / "acme"), "--add", url)
    assert code == 0, terminal
    assert f"[1/1] models  cloning {url} …\n" in terminal
    drawn = terminal.split(f"cloning {url} …\n")[1].split("[1/1] models  done in")[0]
    assert drawn.startswith("\r\x1b[K      ") and "%" in drawn  # each of git's lines over the last, indented
    assert drawn.endswith("\r\x1b[K")  # and gone before the done line
    assert re.search(r"\[1/1\] models  done in \d+\.\d s\n", terminal)
    assert "\r" not in stdout and stdout.splitlines()[0] == f"==> Created workspace acme in {tmp_path / 'acme'}"


def test_on_a_terminal_a_failed_clone_says_gits_error_without_its_progress(tmp_path: Path, models: Path) -> None:
    listed = _workspace_toml(tmp_path, _project("models", f"file://{models}", "nope"))
    code, terminal, _stdout = _on_a_terminal(
        tmp_path, "workspace", "init", str(tmp_path / "acme"), "--from", str(listed)
    )
    assert code == 1
    error = "Error:" + terminal.split("Error:", 1)[1].split("\n")[0]  # the progress drawn before it ends no line
    assert f"Error: [1/1] models: could not add file://{models} as a submodule: fatal: " in error
    assert "%" not in error and "remote:" not in error


# --- a failed init leaves nothing it made ----------------------------------------------------------------------------


def test_a_failed_init_from_a_file_removes_the_folder_it_made_and_runs_again(tmp_path: Path) -> None:
    """A project that cannot be cloned stopped `init --from` half way: a repository with the first project in it,
    which the same command then refused ("already a git repository, with a workspace.toml of its own")."""
    models = _remote(tmp_path, "models")
    missing = tmp_path / "remote" / "tools"
    listed = _workspace_toml(tmp_path, _project("models", models) + _project("tools", missing))
    result = _init(tmp_path, "--from", str(listed))
    assert result.exit_code == 1
    root = tmp_path / "acme"
    assert f"Error: [2/2] tools: could not add {missing} as a submodule:" in result.output
    assert f"{root} is removed again: this init made it. Run the same command once the cause is fixed." in result.output
    assert not root.exists()
    _remote(tmp_path, "tools")
    again = _init(tmp_path, "--from", str(listed))
    assert again.exit_code == 0, again.output


def test_an_empty_folder_init_was_given_is_removed_too(tmp_path: Path) -> None:
    (tmp_path / "acme").mkdir()
    result = _init(tmp_path, "--add", str(tmp_path / "remote" / "nothing"))
    assert result.exit_code == 1
    assert not (tmp_path / "acme").exists()


def test_a_folder_that_held_files_is_never_removed_and_the_message_says_what_to_remove(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    (root / "notes.txt").write_text("mine\n")
    result = _init(tmp_path, "--add", str(tmp_path / "remote" / "nothing"))
    assert result.exit_code == 1
    assert (root / "notes.txt").read_text() == "mine\n"
    assert (
        f"{root} held files before this init, so it is left as it stopped: remove {root / '.git'} and "
        ".gitmodules, then run the same command again." in result.output
    )


def test_an_existing_repository_set_up_in_place_is_left_as_it_stopped(tmp_path: Path) -> None:
    root = _existing_workspace(tmp_path)
    result = _init(tmp_path, "--add", str(tmp_path / "remote" / "nothing"))
    assert result.exit_code == 1
    assert (root / ".git").is_dir() and "is removed again" not in result.output
