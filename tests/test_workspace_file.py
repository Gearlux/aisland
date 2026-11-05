"""`workspace.toml`: the workspace's one committed file, shaped like `.gitmodules`.

Each project is a `[project."<name>"]` table — `path` (the folder; its name when left out), `url`, `branch` —
like `.gitmodules`' `[submodule "<name>"]`; `[workspace]` holds the workspace's own choices. A machine without
aisland reads it to start (its first lines say how), `aisland workspace init --from` creates the workspace from it,
and `aisland setup` keeps its projects current from `.gitmodules` (or, without one, from the clones): a new project
added, a changed url or path followed, a gone project dropped, a branch already written kept. Every refusal names
the file and the line.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest

from aisland import __version__, editor_files, locations, manager, projects
from aisland.workspace import file as workspace_file

_AUTHOR = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture(autouse=True)
def local_submodules(monkeypatch: pytest.MonkeyPatch) -> None:
    """git refuses a submodule from a local path unless `protocol.file.allow` says otherwise (git >= 2.38)."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


def _git(cwd: Path, *args: str) -> str:
    run = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **_AUTHOR}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _remote(tmp_path: Path, name: str, branches: tuple = ()) -> Path:
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        _git(repo, *args)
    for branch in branches:
        _git(repo, "branch", branch)
    return repo


def _superproject(tmp_path: Path, *submodules: tuple) -> Path:
    """A workspace on branch `dev/main` holding each ``(path, remote, branch-or-None)`` as a submodule."""
    root = tmp_path / "acme"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev/main")
    for path, remote, branch in submodules:
        _git(root, "submodule", "add", "-q", *(["-b", branch] if branch else []), str(remote), path)
    return root


def _file(root: Path, text: str) -> Path:
    path = root / "workspace.toml"
    path.write_text(textwrap.dedent(text))
    return path


def _refusal(path: Path) -> str:
    with pytest.raises(workspace_file.FileError) as refused:
        workspace_file.read(path)
    return refused.value.message


# --- reading ---------------------------------------------------------------------------------------------------


def test_a_file_reads_like_gitmodules(tmp_path: Path) -> None:
    path = _file(
        tmp_path,
        """\
        [workspace]
        framework = ["torch"]

        [project."models"]
        path = "m"
        url = "git@example.com:acme/models.git"
        branch = "dev"
        active = false

        [project."tools"]
        url = "https://example.com/acme/tools.git"
        """,
    )
    read = workspace_file.read(path)
    assert read.settings == {"framework": ["torch"]}
    assert list(read.projects) == ["models", "tools"]
    models, tools = read.projects["models"], read.projects["tools"]
    assert (models.path, models.url, models.branch, models.others) == (
        "m",
        "git@example.com:acme/models.git",
        "dev",
        {"active": False},
    )
    assert (tools.path, tools.branch) == ("tools", None)  # no path: the folder is its name


def test_the_old_workspace_file_is_refused_naming_its_line(tmp_path: Path) -> None:
    path = _file(tmp_path, '# A workspace.\n[projects]\nmodels = { url = "x" }\n')
    assert _refusal(path) == (
        f'{path}:2: [projects] is the old workspace file — write each project as [project."<name>"] '
        "with path, url and branch"
    )


def test_a_section_the_file_has_not_is_refused_naming_its_line(tmp_path: Path) -> None:
    path = _file(tmp_path, '[workspace]\n\n[sources.models]\nsource = "local"\n')
    assert _refusal(path) == (
        f'{path}:3: [sources] is no section of workspace.toml — it has [workspace], [ci], [harness] and [project."<name>"]'
    )


@pytest.mark.parametrize(
    "body, said",
    [
        ('[project."models"]\nurl = 1\n', "project 'models': url must be text, as url = \"<text>\""),
        (
            '[project."models"]\nurl = "x"\nbranch = ["dev"]\n',
            "project 'models': branch must be text, as branch = \"<text>\"",
        ),
        (
            '[project."models"]\nurl = "x"\npath = "a/b"\n',
            "project 'models': path 'a/b' is not a folder of the workspace",
        ),
        (
            '[project."models"]\nurl = "x"\npath = ".hidden"\n',
            "project 'models': path '.hidden' is not a folder of the workspace",
        ),
    ],
)
def test_a_project_of_the_wrong_shape_is_refused_naming_its_line(tmp_path: Path, body: str, said: str) -> None:
    path = _file(tmp_path, body)
    assert _refusal(path) == f"{path}:1: {said}"


def test_a_project_written_inline_is_refused_saying_how_to_write_it(tmp_path: Path) -> None:
    path = _file(tmp_path, '[project]\nmodels = "x"\n')
    assert _refusal(path) == f"{path}:1: project 'models' is not a table: write it as [project.\"models\"]"


def test_two_projects_in_one_folder_are_refused_naming_both(tmp_path: Path) -> None:
    path = _file(tmp_path, '[project."a"]\nurl = "x"\npath = "m"\n\n[project."b"]\nurl = "y"\npath = "m"\n')
    assert _refusal(path) == f"{path}:1 and {path}:5: projects 'a' and 'b' are both checked out in the folder 'm'"


def test_a_file_that_is_no_toml_is_refused_with_tomls_own_words(tmp_path: Path) -> None:
    path = _file(tmp_path, '[project."a"]\nurl = \n')
    message = _refusal(path)
    assert message.startswith(f"{path}: ") and "line 2" in message


def test_a_missing_file_is_refused_saying_what_to_name(tmp_path: Path) -> None:
    assert _refusal(tmp_path / "workspace.toml") == (
        f"{tmp_path / 'workspace.toml'} does not exist: name the workspace.toml to create the workspace from."
    )


def test_a_workspace_without_the_file_reads_as_empty(tmp_path: Path) -> None:
    empty = workspace_file.load(tmp_path)
    assert (empty.settings, empty.projects) == ({}, {})


# --- writing ---------------------------------------------------------------------------------------------------


def test_the_written_file_says_how_to_start_a_machine_from_it_and_reads_back_the_same(tmp_path: Path) -> None:
    original = _file(
        tmp_path,
        """\
        [workspace]
        framework = ["torch"]

        [project."models"]
        path = "m"
        url = "git@example.com:acme/models.git"
        branch = "dev"
        active = false
        """,
    )
    read = workspace_file.read(original)
    written = workspace_file.write(tmp_path, read)
    assert written is not None
    text = written.read_text()
    assert "#        .venv/bin/aisland workspace init . --from workspace.toml" in text.splitlines()
    assert "#        .venv/bin/aisland setup" in text.splitlines()
    assert text.split("\n[workspace]\n", 1)[1] == textwrap.dedent(
        """\
        format = 1
        framework = ["torch"]

        [project."models"]
        path = "m"
        url = "git@example.com:acme/models.git"
        branch = "dev"
        active = false
        """
    )
    assert workspace_file.read(written) == read
    again = workspace_file.write(tmp_path, read)
    assert again is not None and again.read_text() == text  # a rewrite changes nothing


def test_the_how_to_makes_a_venv_with_pip_and_says_how_to_add_it_to_one_without(tmp_path: Path) -> None:
    """`aisland setup` installs uv into the venv with the venv's pip, and a plain `uv venv` makes none."""
    written = workspace_file.write(tmp_path, workspace_file.WorkspaceFile(settings={"framework": ["torch"]}))
    assert written is not None
    lines = written.read_text().splitlines()
    uv_venv = [line for line in lines if line.lstrip("# ").startswith("uv venv")]
    assert uv_venv == [f"#        uv venv --seed --prompt {tmp_path.name} .venv"]
    assert "#        uv pip install --python .venv/bin/python pip" in lines


def test_the_how_to_installs_aisland_with_the_venvs_own_pip_as_the_readme_and_init_do(tmp_path: Path) -> None:
    """One spelling everywhere a person reads it: `.venv/bin/pip install aisland` (the README, `init`'s Next lines)."""
    written = workspace_file.write(tmp_path, workspace_file.WorkspaceFile(settings={"framework": ["torch"]}))
    assert written is not None
    lines = written.read_text().splitlines()
    after_venv = lines[lines.index(f"#        uv venv --seed --prompt {tmp_path.name} .venv") + 1]
    assert after_venv == "#        .venv/bin/pip install aisland"
    assert f"#        .venv/bin/pip install ./aisland-{__version__}-py3-none-any.whl" in lines
    installs = [line for line in lines if "install" in line and "aisland" in line and "#        " in line]
    assert all(".venv/bin/pip install" in line for line in installs), installs


def test_a_workspace_with_nothing_to_write_has_no_file(tmp_path: Path) -> None:
    (tmp_path / "workspace.toml").write_text('[project."a"]\nurl = "x"\n')
    assert workspace_file.write(tmp_path, workspace_file.WorkspaceFile()) is None
    assert not (tmp_path / "workspace.toml").exists()


# --- `aisland setup` keeps the projects current ----------------------------------------------------------------


def test_setup_writes_each_submodule_with_its_folder_url_and_branch(tmp_path: Path) -> None:
    models, tools = _remote(tmp_path, "models", ("dev",)), _remote(tmp_path, "tools")
    root = _superproject(tmp_path, ("models", models, "dev"), ("tools", tools, None))
    assert workspace_file.refresh(root) == ["workspace.toml: added models", "workspace.toml: added tools"]
    read = workspace_file.load(root)
    assert [(p.name, p.path, p.url, p.branch) for p in read.projects.values()] == [
        ("models", "models", str(models), "dev"),
        ("tools", "tools", str(tools), None),
    ]


def test_a_submodule_following_the_roots_branch_is_written_with_the_roots_branch(tmp_path: Path) -> None:
    root = _superproject(tmp_path, ("models", _remote(tmp_path, "models"), None))
    _git(root, "config", "-f", ".gitmodules", "submodule.models.branch", ".")
    workspace_file.refresh(root)
    assert workspace_file.load(root).projects["models"].branch == "dev/main"


def test_a_branch_already_in_the_file_is_kept(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", ("dev",))
    root = _superproject(tmp_path, ("models", models, "dev"))
    _file(root, f'[project."models"]\nurl = "{models}"\nbranch = "release"\n')
    assert workspace_file.refresh(root) == []
    assert workspace_file.load(root).projects["models"].branch == "release"


def test_a_url_changed_in_gitmodules_is_followed_and_said(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    root = _superproject(tmp_path, ("models", models, None))
    _file(root, '[project."models"]\nurl = "https://old.example.com/models.git"\nactive = false\n')
    assert workspace_file.refresh(root) == [f"workspace.toml: models url {models}"]
    project = workspace_file.load(root).projects["models"]
    assert (project.url, project.others) == (str(models), {"active": False})  # its other settings stay


def test_a_project_no_longer_in_the_workspace_is_dropped_and_said(tmp_path: Path) -> None:
    """Only when nothing could bring it back: with a url, setup adds it instead (`tests/test_activation.py`)."""
    root = _superproject(tmp_path, ("models", _remote(tmp_path, "models"), None))
    _file(root, '[project."old"]\npath = "old"\n')
    assert workspace_file.refresh(root) == [
        "workspace.toml: dropped old (no longer in the workspace)",
        "workspace.toml: added models",
    ]
    assert list(workspace_file.load(root).projects) == ["models"]


def test_nothing_changed_leaves_the_file_as_it_was(tmp_path: Path) -> None:
    root = _superproject(tmp_path, ("models", _remote(tmp_path, "models"), None))
    workspace_file.refresh(root)
    before = (root / "workspace.toml").read_bytes()
    os.utime(root / "workspace.toml", (0, 0))
    assert workspace_file.refresh(root) == []
    assert (root / "workspace.toml").read_bytes() == before
    assert (root / "workspace.toml").stat().st_mtime == 0


def test_without_gitmodules_each_clone_is_a_project_and_a_link_to_it_its_path(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    models = _remote(tmp_path, "models")
    _git(root, "clone", "-q", str(models), "machine_learning")
    (root / "ml").symlink_to("machine_learning")
    workspace_file.refresh(root)
    project = workspace_file.load(root).projects["models"]
    assert (project.path, project.url, project.branch) == ("ml", str(models), "main")


def test_a_workspace_without_projects_gets_no_file(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    assert workspace_file.refresh(root) == []
    assert not (root / "workspace.toml").exists()


def test_a_file_aisland_cannot_read_stops_setups_upkeep_naming_its_line(tmp_path: Path) -> None:
    root = _superproject(tmp_path, ("models", _remote(tmp_path, "models"), None))
    _file(root, '[projects]\nmodels = { url = "x" }\n')
    with pytest.raises(workspace_file.FileError) as refused:
        workspace_file.refresh(root)
    assert refused.value.message.startswith(f"{root / 'workspace.toml'}:1: [projects] is the old workspace file")


def test_setup_keeps_the_file_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`aisland setup` runs the upkeep and prints what it changed, before it provisions anything."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    seen: List[str] = []

    class _Stop(Exception):
        pass

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(manager, "link_project_bashrc", lambda root: None)
    monkeypatch.setattr(manager, "install_harness", lambda root: [])

    def refresh(root: Path) -> List[str]:
        seen.append("refresh")
        return ["workspace.toml: added x"]

    monkeypatch.setattr(workspace_file, "refresh", refresh)
    monkeypatch.setattr("aisland.provision.install", lambda root, aislands_own=True: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        manager.setup_workspace(non_interactive=True)
    assert seen == ["refresh"]


# --- the smaller cases -----------------------------------------------------------------------------------------


def test_numbers_and_tables_of_the_workspace_section_are_written_back_as_they_were(tmp_path: Path) -> None:
    path = _file(tmp_path, '[workspace]\ncount = 3\nratio = 0.5\nextra = { a = "b", on = true }\n')
    read = workspace_file.read(path)
    written = workspace_file.write(tmp_path, read)
    assert written is not None and workspace_file.read(written) == read


def test_an_old_file_written_with_dotted_keys_is_refused_naming_the_file(tmp_path: Path) -> None:
    path = _file(tmp_path, 'projects.models.url = "x"\n')
    assert _refusal(path).startswith(f"{path}: [projects] is the old workspace file")


def test_a_folder_changed_in_gitmodules_is_followed_and_said(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models")
    root = _superproject(tmp_path, ("models", models, None))
    _file(root, f'[project."models"]\npath = "old"\nurl = "{models}"\n')
    assert workspace_file.refresh(root) == ["workspace.toml: models path models"]
    assert workspace_file.load(root).projects["models"].path == "models"


def test_a_project_without_a_branch_gets_the_one_gitmodules_names(tmp_path: Path) -> None:
    models = _remote(tmp_path, "models", ("dev",))
    root = _superproject(tmp_path, ("models", models, "dev"))
    _file(root, f'[project."models"]\nurl = "{models}"\n')
    assert workspace_file.refresh(root) == ["workspace.toml: models branch dev"]
    assert workspace_file.load(root).projects["models"].branch == "dev"


def test_what_is_no_project_of_the_file_is_left_out(tmp_path: Path) -> None:
    """A submodule below a subfolder, a file, a folder that is no clone, a clone without an origin: none is a project
    the file can list."""
    models = _remote(tmp_path, "models")
    root = _superproject(tmp_path, ("models", models, None))
    with open(root / ".gitmodules", "a") as gitmodules:
        gitmodules.write('[submodule "deep"]\n\tpath = libs/deep\n\turl = x\n')
    assert [p.name for p in workspace_file.workspace_projects(root)] == ["models"]
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.txt").write_text("not a folder\n")
    (plain / "docs").mkdir()  # a folder that is no clone
    (plain / "local").mkdir()
    _git(plain / "local", "init", "-q")
    assert workspace_file.workspace_projects(plain) == []


# --- nothing written twice ---------------------------------------------------------------------------------------


def test_a_path_equal_to_the_name_is_not_written(tmp_path: Path) -> None:
    path = _file(
        tmp_path, '[project."models"]\npath = "models"\nurl = "u"\n\n[project."tools"]\npath = "t"\nurl = "v"\n'
    )
    written = workspace_file.write(tmp_path, workspace_file.read(path))
    assert written is not None
    body = written.read_text().split("# =========================================================================\n")[
        -1
    ]
    assert (
        body
        == '\n[workspace]\nformat = 1\n\n[project."models"]\nurl = "u"\n\n[project."tools"]\npath = "t"\nurl = "v"\n'
    )


def test_a_project_without_a_url_is_read_its_settings_kept(tmp_path: Path) -> None:
    """A source can be set for a project setup has not written yet; only `init --from`, which clones, needs a url."""
    path = _file(tmp_path, '[project."loggair"]\nsource = "pypi"\n')
    project = workspace_file.read(path).projects["loggair"]
    assert (project.url, project.others) == (None, {"source": "pypi"})


# --- the workspace's name -----------------------------------------------------------------------------------------


def _repository(root: Path, origin: Optional[str] = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)
    return root


def test_the_files_name_names_the_workspace_before_its_origin_and_its_folder(tmp_path: Path) -> None:
    root = _repository(tmp_path / "workspace", "git@example.com:acme/models-root.git")
    _file(root, '[workspace]\nname = "studio"\n')
    assert projects.get_workspace_name(root) == "studio"
    assert editor_files.code_workspace_path(root) == root / "studio.code-workspace"


def test_without_a_name_the_origin_then_the_folder_names_the_workspace(tmp_path: Path) -> None:
    with_origin = _repository(tmp_path / "workspace", "git@example.com:acme/models-root.git")
    _file(with_origin, '[workspace]\nframework = ["torch"]\n')
    assert projects.get_workspace_name(with_origin) == "models-root"
    assert projects.get_workspace_name(_repository(tmp_path / "plain")) == "plain"


@pytest.mark.parametrize(
    "origin, name",
    [
        ("https://github.com/acme/models.git", "models"),
        ("git@github.com:acme/my.github-tools.git", "my.github-tools"),
        ("git@example.com:models.git", "models"),
        ("https://example.com/acme/models/", "models"),
        ("ssh://git@example.com/acme/data.git.backup", "data.git.backup"),
    ],
)
def test_the_origins_name_drops_git_from_its_end_only(tmp_path: Path, origin: str, name: str) -> None:
    """`.git` inside a repository's name is part of it: `my.github-tools.git` is `my.github-tools`, never
    `myhub-tools`."""
    assert projects.get_workspace_name(_repository(tmp_path / "workspace", origin)) == name


def test_the_how_to_uses_the_files_name(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    written = workspace_file.write(root, workspace_file.WorkspaceFile(settings={"name": "studio"}))
    assert written is not None
    lines = written.read_text().splitlines()
    assert lines[0] == "# workspace.toml — the studio workspace: its projects and its choices, committed."
    assert "#        uv venv --seed --prompt studio .venv" in lines
    assert lines[lines.index("[workspace]") + 2] == 'name = "studio"'


@pytest.mark.parametrize("value", ["3", '"a/b"', '""', '"two words"'])
def test_a_name_that_is_no_plain_name_is_refused_naming_its_line(tmp_path: Path, value: str) -> None:
    path = _file(tmp_path, f'[workspace]\nframework = ["torch"]\nname = {value}\n')
    assert _refusal(path) == (
        f'{path}:3: the workspace\'s name must be one plain name (letters, digits, _ . -), as name = "studio"'
    )


def test_a_broken_file_leaves_the_name_to_the_origin_and_the_folder(tmp_path: Path) -> None:
    """Setup's upkeep reports the file; everything that only wants a name goes on with the next answer."""
    root = _repository(tmp_path / "plain")
    _file(root, "[workspace]\nname = 3\n")
    assert projects.get_workspace_name(root) == "plain"


# --- the format ------------------------------------------------------------------------------------------------


def test_the_file_says_its_format_first_and_reads_back_without_it_in_the_settings(tmp_path: Path) -> None:
    written = workspace_file.write(tmp_path, workspace_file.WorkspaceFile(settings={"name": "studio"}))
    assert written is not None
    lines = written.read_text().splitlines()
    assert lines[lines.index("[workspace]") + 1] == f"format = {workspace_file.FORMAT}"
    assert workspace_file.read(written).settings == {"name": "studio"}


def test_a_file_without_a_format_is_read_as_the_first(tmp_path: Path) -> None:
    path = _file(tmp_path, '[workspace]\nname = "studio"\n')
    assert workspace_file.read(path).settings == {"name": "studio"}


def test_a_newer_format_is_refused_naming_its_line_and_the_upgrade(tmp_path: Path) -> None:
    path = _file(tmp_path, '[workspace]\nname = "studio"\nformat = 2\n')
    assert _refusal(path) == (
        f"{path}:3: workspace.toml is format 2, written for a newer aisland; this one reads format 1. "
        "Upgrade it: pip install --upgrade aisland"
    )


@pytest.mark.parametrize("value", ['"1"', "0", "true", "1.5"])
def test_a_format_that_is_no_whole_number_is_refused(tmp_path: Path, value: str) -> None:
    path = _file(tmp_path, f"[workspace]\nformat = {value}\n")
    assert _refusal(path) == f"{path}:2: format must be a whole number, as format = 1"
