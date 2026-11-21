"""A workspace with a root project: one clone holds the workspace's own files, and nothing records the others' commits.

The root project is an ordinary clone in the workspace folder whose `workspace.toml` names it (`[workspace] root`).
Clone it into a folder and run `aisland setup` there: setup links `workspace.toml` into the folder (and, through the
root project's harness set, its rules and the files its `harness.toml` places), clones every other project the file
lists beside it — plain clones: no `.gitmodules`, no root repository — and installs them. `workspace.toml` wins: a url
it gives is the one the clone follows. This machine's choices live in the root project's own `.git/config`, which is
never committed. The root project is a project for git only: it is never installed, and it is listed last.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import pytest
from click.testing import CliRunner

from aisland import cli, locations, machine_config, pytest_stats
from aisland.errors import AislandError
from aisland.setup import harness, manager, source_commands
from aisland.workspace import activation
from aisland.workspace import check as workspace_check
from aisland.workspace import file as workspace_file
from aisland.workspace import mirrors, root_project, source_apply, sources, tracking
from aisland.workspace.model import Workspace

_AUTHOR = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's git configuration is neither read nor touched; local paths may be cloned."""
    for key in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key, value in _AUTHOR.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "gitconfig").write_text('[protocol "file"]\n\tallow = always\n')


def _git(cwd: Path, *args: str) -> str:
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _get(cwd: Path, *args: str) -> Optional[str]:
    run = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return run.stdout.strip() if run.returncode == 0 else None


def _remote(tmp_path: Path, name: str, files: Dict[str, str], branches: Sequence[str] = ()) -> Path:
    """A repository under `remote/` holding `files` on `main`, with `branches` beside it."""
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    for path, text in files.items():
        (repo / path).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        _git(repo, *args)
    for branch in branches:
        _git(repo, "branch", branch)
    return repo


def _python(name: str) -> Dict[str, str]:
    return {"pyproject.toml": f'[project]\nname = "{name}"\nversion = "0.1.0"\n'}


@dataclass
class Rooted:
    """`work/`, a plain folder holding only `acme/`, a clone of the root project; the projects' remotes by name."""

    root: Path
    project: Path
    remotes: Dict[str, Path]

    @property
    def listed(self) -> Path:
        """The root project's `workspace.toml` — the file itself, not the folder's link to it."""
        return self.project / "workspace.toml"

    def add_row(self, text: str) -> None:
        """A row another machine wrote, as a pull of the root project brings it."""
        self.listed.write_text(self.listed.read_text() + "\n" + textwrap.dedent(text))


@pytest.fixture
def rooted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rooted:
    """A new machine: the root project `acme` cloned into `work/`, nothing else yet. Its `workspace.toml` lists itself,
    `models` (on `dev/main`) and `tools` (optional, off by default); `extra` and `lib` have remotes no row names yet."""
    remotes = {name: _remote(tmp_path, name, _python(name), ("dev/main",)) for name in ("models", "tools", "extra")}
    remotes["lib"] = _remote(tmp_path, "lib", _python("lib"))
    acme_url = tmp_path / "remote" / "acme"
    listed = textwrap.dedent(
        f"""\
        [workspace]
        format = 1
        root = "acme"

        [project."acme"]
        url = "{acme_url}"
        branch = "main"

        [project."models"]
        url = "{remotes['models']}"
        branch = "dev/main"

        [project."tools"]
        url = "{remotes['tools']}"
        branch = "dev/main"
        active = false
        """
    )
    remotes["acme"] = _remote(
        tmp_path,
        "acme",
        {
            "workspace.toml": listed,
            "AGENTS.md": "# acme rules\n",
            "TASKS.md": "# tasks\n",
            "harness.toml": 'format = 1\nrules = "AGENTS.md"\n\n[files]\n"TASKS.md" = "TASKS.md"\n',
        },
    )
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "clone", "-q", str(remotes["acme"]), "acme")
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return Rooted(root, root / "acme", remotes)


def _set_up(rooted: Rooted) -> None:
    """What `aisland setup` does with the projects before it installs them, in its order: the root project's file
    linked, `workspace.toml` kept current, each project's folder brought in line."""
    root_project.link(rooted.root)
    workspace_file.refresh(rooted.root)
    activation.reconcile(rooted.root)


# --- which folder is the workspace, which clone its root project ----------------------------------------------------


def test_a_folder_holding_the_root_project_is_the_workspace(rooted: Rooted) -> None:
    """Only the root project is cloned yet, and it holds no `pyproject.toml`: none of the other rules finds the
    folder — it holds no `.gitmodules`, no project and no repository."""
    assert locations.find_workspace(rooted.root) == rooted.root


def test_from_inside_the_root_project_the_workspace_is_its_parent(rooted: Rooted) -> None:
    """Its own `.git` folder would otherwise make it a workspace of its own."""
    assert locations.find_workspace(rooted.project) == rooted.root


def test_the_root_project_is_the_clone_whose_workspace_toml_names_it(rooted: Rooted) -> None:
    assert locations.root_project(rooted.root) == rooted.project
    root_project.link(rooted.root)
    assert locations.root_project(rooted.root) == rooted.project  # read through the folder's link now


def test_a_clone_whose_workspace_toml_names_another_project_is_no_root_project(rooted: Rooted) -> None:
    rooted.listed.write_text(rooted.listed.read_text().replace('root = "acme"', 'root = "models"'))
    assert locations.root_project(rooted.root) is None


def test_a_workspace_whose_file_names_no_root_project_has_none(tmp_path: Path) -> None:
    (tmp_path / "workspace.toml").write_text('[workspace]\nformat = 1\n\n[project."models"]\nurl = "x"\n')
    assert locations.root_project(tmp_path) is None
    assert root_project.link(tmp_path) == []


def test_root_is_a_key_aisland_reads(rooted: Rooted) -> None:
    """`aisland setup` names every key nothing reads (a typo); `root` is one it reads."""
    root_project.link(rooted.root)
    assert workspace_check.unknown_keys(rooted.root) == []


# --- workspace.toml: the root project's, linked into the folder -----------------------------------------------------


def test_setup_links_workspace_toml_to_the_root_projects(rooted: Rooted) -> None:
    assert root_project.link(rooted.root) == ["linked workspace.toml to acme/workspace.toml, the root project's"]
    link = rooted.root / "workspace.toml"
    assert link.is_symlink() and os.readlink(link) == "acme/workspace.toml"
    assert root_project.link(rooted.root) == []  # already linked: nothing to say


@pytest.mark.parametrize("kind", ["a file of its own", "a link to other/workspace.toml"])
def test_a_workspace_toml_that_is_not_the_root_projects_is_refused_and_left(rooted: Rooted, kind: str) -> None:
    own = rooted.root / "workspace.toml"
    if kind == "a file of its own":
        own.write_text(rooted.listed.read_text())
    else:
        (rooted.root / "other").mkdir()
        (rooted.root / "other" / "workspace.toml").write_text(rooted.listed.read_text())
        own.symlink_to("other/workspace.toml")
    with pytest.raises(AislandError) as refusal:
        root_project.link(rooted.root)
    assert refusal.value.format_message() == (
        f"workspace.toml is {kind}, not the root project's acme/workspace.toml. "
        "Remove it and run aisland setup again: setup links it."
    )
    assert own.is_symlink() == (kind != "a file of its own")


def test_setup_writes_workspace_toml_through_the_link_into_the_root_project(rooted: Rooted) -> None:
    """A project cloned by hand gets its row in the root project's file, to be committed there."""
    root_project.link(rooted.root)
    _git(rooted.root, "clone", "-q", str(rooted.remotes["extra"]), "extra")
    workspace_file.refresh(rooted.root)
    assert (rooted.root / "workspace.toml").is_symlink()
    assert '[project."extra"]' in rooted.listed.read_text()
    assert _git(rooted.project, "status", "--porcelain") == "M workspace.toml"


def test_a_url_changed_in_workspace_toml_is_followed_and_never_written_back(rooted: Rooted) -> None:
    """Another machine moved `models` to a new host and pushed the root project. Here the clone's origin follows the
    file; the clone's old origin never overwrites the file's url (which this machine would then commit)."""
    _set_up(rooted)
    moved = rooted.remotes["models"].parent / "models-moved"
    _git(rooted.root, "clone", "-q", "--bare", str(rooted.remotes["models"]), str(moved))
    old_line, new_line = f'url = "{rooted.remotes["models"]}"\n', f'url = "{moved}"\n'
    rooted.listed.write_text(rooted.listed.read_text().replace(old_line, new_line))
    _set_up(rooted)
    assert new_line in rooted.listed.read_text()
    assert _git(rooted.root / "models", "remote", "get-url", "origin") == str(moved)


def test_setup_links_the_root_projects_file_before_it_installs_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness reads `[harness] sets` from `workspace.toml`: the link must be there first."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    calls: List[str] = []

    class _Stop(Exception):
        pass

    def link(root: Path) -> List[str]:
        calls.append("link")
        return []

    def install_harness(root: Path) -> List[str]:
        calls.append("harness")
        raise _Stop

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(manager, "link_project_bashrc", lambda root: None)
    monkeypatch.setattr(root_project, "link", link)
    monkeypatch.setattr(manager, "install_harness", install_harness)
    with pytest.raises(_Stop):
        manager.setup_workspace(non_interactive=True)
    assert calls == ["link", "harness"]


# --- the projects: plain clones beside the root project -------------------------------------------------------------


def test_setup_clones_every_listed_project_beside_the_root_project(
    rooted: Rooted, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_up(rooted)
    models = rooted.root / "models"
    assert (models / ".git").is_dir()  # a clone holding its own repository, not a submodule's `.git` file
    assert _git(models, "branch", "--show-current") == "dev/main"
    assert "  - clone models (dev/main)" in capsys.readouterr().out
    assert not (rooted.root / "tools").exists()  # optional and off, unless this machine switches it on
    assert not (rooted.root / ".gitmodules").exists()
    assert not (rooted.root / ".git").exists()


def test_a_row_a_pull_brought_is_cloned_by_the_next_setup(rooted: Rooted) -> None:
    _set_up(rooted)
    rooted.add_row(f'[project."extra"]\nurl = "{rooted.remotes["extra"]}"\n')
    _set_up(rooted)
    assert (rooted.root / "extra" / "pyproject.toml").is_file()
    assert _git(rooted.root / "extra", "branch", "--show-current") == "main"  # no branch in its row: origin's default


def test_a_project_this_machine_switches_on_is_cloned(rooted: Rooted) -> None:
    activation.set_machine_choice(rooted.root, "tools", True)
    _set_up(rooted)
    assert (rooted.root / "tools" / ".git").is_dir()


def test_a_project_switched_off_keeps_its_clone_and_is_not_installed(
    rooted: Rooted, capsys: pytest.CaptureFixture[str]
) -> None:
    """A plain clone's repository has no other place to live: emptying the folder would delete it."""
    _set_up(rooted)
    models = rooted.root / "models"
    _git(models, "commit", "-q", "--allow-empty", "-m", "work no remote has")
    activation.set_machine_choice(rooted.root, "models", False)
    capsys.readouterr()
    activation.reconcile(rooted.root)
    assert "  - models is off here: not installed, its clone kept" in capsys.readouterr().out
    assert _git(models, "log", "-1", "--format=%s") == "work no remote has"
    assert "models" not in [project["name"] for project in Workspace(rooted.root).install_order]


def test_a_project_installed_from_pypi_gets_no_clone(rooted: Rooted) -> None:
    rooted.add_row(f'[project."lib"]\nurl = "{rooted.remotes["lib"]}"\nsource = "pypi"\n')
    _set_up(rooted)
    assert not (rooted.root / "lib").exists()


@pytest.mark.parametrize("kind", ["pypi", "github"])
def test_a_clone_taken_from_its_release_is_kept_and_the_release_installed(
    rooted: Rooted, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """In a workspace of submodules the folder is emptied (its repository stays in `.git/modules`); a plain clone's
    repository lives nowhere else, so it stays. A `github` source installs from the url its row gives."""
    _set_up(rooted)
    runs: List[List[str]] = []

    def run(cmd: List[str], cwd: Path, *, quiet: bool = False) -> bool:
        runs.append(cmd)
        return True

    monkeypatch.setattr(source_apply, "_run", run)
    source = sources.ProjectSource(kind=kind, ref="main", distribution="models")
    assert source_apply.apply_source(rooted.root, "models", source, "uv") is True
    assert _git(rooted.root / "models", "log", "-1", "--format=%s") == "first"
    wanted = "models" if kind == "pypi" else sources.pip_vcs_url(str(rooted.remotes["models"]), "main")
    assert runs[-1] == ["uv", "pip", "install", "-U", wanted]


def test_a_clone_may_be_set_to_take_its_release(rooted: Rooted, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder of projects refuses a detached source, which would empty a folder nothing can check out again; a
    workspace with a root project empties nothing, so the source is set and the clone stays."""
    _set_up(rooted)
    (rooted.root / ".venv" / "bin").mkdir(parents=True)
    monkeypatch.setattr(source_commands, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(source_commands, "write_code_workspace", lambda root: None)
    monkeypatch.setattr(source_apply, "_run", lambda cmd, cwd, quiet=False: True)
    assert (
        source_commands.set_project_source(
            "models", "github", ref="main", machine_only=True, workspace_root=rooted.root
        )
        == 0
    )
    override = sources.local_override(rooted.root, "models")
    assert override is not None and override.kind == "github"
    assert (rooted.root / "models" / ".git").is_dir()


# --- this machine's choices: in the root project's repository -------------------------------------------------------


def test_this_machines_choices_live_in_the_root_projects_repository(rooted: Rooted) -> None:
    """The workspace folder is no repository; the root project's `.git/config` is this machine's, never committed."""
    machine_config.put(rooted.root, "aisland.active.tools.active", True)
    assert _git(rooted.project, "config", "--local", "--get", "aisland.active.tools.active") == "true"
    assert machine_config.get(rooted.root, "aisland.active.tools.active") == "true"
    assert _git(rooted.project, "status", "--porcelain") == ""


def test_the_test_stats_switch_is_read_from_the_root_projects_repository(rooted: Rooted) -> None:
    """The pytest plugin reads its switches with the standard library, from the same repository."""
    _git(rooted.project, "config", "--local", "aisland.test-stats.default", "false")
    root_project.link(rooted.root)
    assert pytest_stats.recording(rooted.root, "models") == pytest_stats.Recording(False, "this machine", "models")


# --- the root project is a project for git, never for Python --------------------------------------------------------


def test_the_root_project_is_never_installed_even_with_a_pyproject(rooted: Rooted) -> None:
    (rooted.project / "pyproject.toml").write_text('[project]\nname = "acme"\nversion = "0.1.0"\n')
    _set_up(rooted)
    assert [project["name"] for project in Workspace(rooted.root).install_order] == ["models"]


def test_workspace_projects_lists_the_root_project_last(rooted: Rooted) -> None:
    """`/commit-all` and `/ship` walk this list: an edit to `TASKS.md` is committed only when the root project is on
    it, and last, after every project whose work it may describe."""
    _set_up(rooted)
    result = CliRunner().invoke(cli.main, ["workspace", "projects"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["models", "acme"]


@pytest.mark.parametrize("change", [tracking.set_committed, tracking.set_machine])
def test_tracking_is_refused_where_nothing_records_commits(
    rooted: Rooted, change: Callable[[Path, Sequence[str], bool], List[tracking.Change]]
) -> None:
    _set_up(rooted)
    with pytest.raises(AislandError) as refusal:
        change(rooted.root, [], False)
    assert refusal.value.format_message() == (
        "nothing records the projects' commits here: acme is the workspace's root project, "
        "and the workspace folder is no repository"
    )


# --- what the root project brings: rules and files, always as links -------------------------------------------------


@pytest.fixture
def builtin_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """aisland's own harness set, a small stand-in."""
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "harness.toml").write_text('rules = "AGENTS.md"\n')
    (builtin / "AGENTS.md").write_text("# aisland rules\n")
    monkeypatch.setattr(harness, "HARNESS_DIR", builtin)
    monkeypatch.setattr(harness, "registered", lambda: {})


@pytest.mark.usefixtures("builtin_set")
def test_the_root_projects_rules_and_files_reach_the_folder_as_links(rooted: Rooted) -> None:
    """From a wheel too (`link=False`): a copy of `TASKS.md` would keep an edit made at the root from ever reaching
    the root project. Without `[harness] sets`, a workspace with a root project takes aisland's set and the root
    project's, in that order."""
    root = rooted.root
    root_project.link(root)
    harness.install_harness(root, link=False)
    imports = [line for line in (root / ".agents" / "rules.md").read_text().splitlines() if line.startswith("@")]
    assert imports == ["@.agents/aisland/AGENTS.md", "@.agents/acme/AGENTS.md"]
    rules = root / ".agents" / "acme" / "AGENTS.md"
    assert rules.is_symlink() and rules.resolve() == (rooted.project / "AGENTS.md").resolve()
    assert (root / "CLAUDE.md").read_text().splitlines()[0] == "@.agents/rules.md"
    tasks = root / "TASKS.md"
    assert tasks.is_symlink()
    tasks.write_text("# tasks\n- one more\n")
    assert _git(rooted.project, "status", "--porcelain") == "M TASKS.md"  # the commands' link is excluded
    assert (rooted.project / ".claude" / "commands").is_symlink()  # a session started in it finds the commands


@pytest.mark.usefixtures("builtin_set")
def test_the_skills_of_the_root_project_and_of_each_project_installed_here_reach_the_root(rooted: Rooted) -> None:
    """A project switched off here keeps its clone, and its skill no longer reaches the root."""
    _set_up(rooted)
    for folder in (rooted.root / "models", rooted.project):
        skill = folder / ".claude" / "skills" / f"{folder.name}-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {folder.name}-skill\n---\n")
    harness.install_harness(rooted.root, link=False)
    skills = rooted.root / ".claude" / "skills"
    assert sorted(link.name for link in skills.iterdir()) == ["acme-skill", "models-skill"]
    activation.set_machine_choice(rooted.root, "models", False)
    harness.install_harness(rooted.root, link=False)
    assert sorted(link.name for link in skills.iterdir()) == ["acme-skill"]


# --- mirrors: on a clone, the primary is the row's url --------------------------------------------------------------


def test_a_mirror_is_set_and_synced_on_a_clone(rooted: Rooted, tmp_path: Path) -> None:
    _set_up(rooted)
    mirror = tmp_path / "mirror" / "models.git"
    mirror.parent.mkdir()
    _git(mirror.parent, "init", "-q", "--bare", str(mirror))
    assert mirrors.primary_url(rooted.root, "models") == str(rooted.remotes["models"])
    assert mirrors.set_mirror("models", str(mirror), workspace_root=rooted.root) == 0
    models = rooted.root / "models"
    assert _git(models, "remote", "get-url", "mirror") == str(mirror)
    mirrors.sync(workspace_root=rooted.root)
    assert _get(mirror, "rev-parse", "dev/main") == _git(models, "rev-parse", "origin/dev/main")
