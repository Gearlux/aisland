"""The `active` plugin: which projects this machine has, from `workspace.toml` and this machine's choice.

A project whose table in `workspace.toml` has an `active` key is OPTIONAL and the value is its default; a project
without the key is always part of the workspace. `aisland workspace init --from` clones only the projects without
it, and `aisland setup` asks about each optional one this machine has not answered (`aisland.active.<project>.active`
in `.git/config`), then brings the folders in line: an active project is checked out — added from its url and branch
when the workspace does not have it yet — and an inactive one emptied. Setup also writes what git reads: `.gitmodules`
`active = false` for a project whose default is off, and this machine's `submodule.<name>.active`.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest
from click.testing import CliRunner

from aisland import cli, console, locations
from aisland.setup import manager
from aisland.workspace import activation
from aisland.workspace import file as workspace_file

_AUTHOR = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's git configuration is neither read nor touched; local paths may be submodules."""
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


def _remote(tmp_path: Path, name: str, branches: tuple = ()) -> Path:
    repo = tmp_path / "remote" / name
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "first"]):
        _git(repo, *args)
    for branch in branches:
        _git(repo, "branch", branch)
    return repo


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A workspace holding `models` (always) and `tools` (optional, default off) as submodules, and listing `extra`
    (optional, default on) which it does not have yet — a fresh machine made by `init --from`."""
    root = tmp_path / "acme"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev/main")
    remotes = {name: _remote(tmp_path, name, ("dev",)) for name in ("models", "tools", "extra")}
    for name in ("models", "tools"):
        _git(root, "submodule", "add", "-q", str(remotes[name]), name)
    _git(root, "commit", "-q", "-m", "workspace")
    (root / "workspace.toml").write_text(
        textwrap.dedent(
            f"""\
            [project."models"]
            url = "{remotes['models']}"

            [project."tools"]
            url = "{remotes['tools']}"
            active = false

            [project."extra"]
            url = "{remotes['extra']}"
            branch = "dev"
            active = true
            """
        )
    )
    return root


# --- what is optional, and what is in effect here ----------------------------------------------------------------


@pytest.mark.slow
def test_a_project_with_an_active_key_is_optional_and_the_value_is_its_default(ws: Path) -> None:
    assert activation.optional(ws) == {"tools": False, "extra": True}


@pytest.mark.slow
def test_this_machines_choice_wins_else_the_default_else_on(ws: Path) -> None:
    assert [activation.effective(ws, p) for p in ("models", "tools", "extra")] == [True, False, True]
    activation.set_machine_choice(ws, "tools", True)
    activation.set_machine_choice(ws, "models", False)
    assert _git(ws, "config", "--local", "--get", "aisland.active.tools.active") == "true"
    assert [activation.effective(ws, p) for p in ("models", "tools", "extra")] == [False, True, True]
    activation.set_machine_choice(ws, "tools", None)
    assert activation.machine_choice(ws, "tools") is None and activation.effective(ws, "tools") is False


# --- setup asks about the optional projects ----------------------------------------------------------------------


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Every question asked, answered with its own default."""
    asked: List[str] = []

    def confirm(text: str, default: bool = False) -> bool:
        asked.append(f"{text} [{'Y/n' if default else 'y/N'}]")
        return default

    monkeypatch.setattr(console, "confirm", confirm)
    return asked


@pytest.mark.slow
def test_setup_asks_about_each_optional_project_with_its_default_and_keeps_the_answer(
    ws: Path, answers: List[str]
) -> None:
    activation.resolve(ws, non_interactive=True)
    assert answers == ["  Activate 'tools'? (clone + install) [y/N]", "  Activate 'extra'? (clone + install) [Y/n]"]
    assert (activation.machine_choice(ws, "tools"), activation.machine_choice(ws, "extra")) == (False, True)


@pytest.mark.slow
def test_an_answered_project_is_not_asked_again_under_yes(ws: Path, answers: List[str]) -> None:
    activation.set_machine_choice(ws, "tools", True)
    activation.resolve(ws, non_interactive=True)
    assert answers == ["  Activate 'extra'? (clone + install) [Y/n]"]


@pytest.mark.slow
def test_without_yes_every_optional_project_is_asked_its_answer_the_default(ws: Path, answers: List[str]) -> None:
    activation.set_machine_choice(ws, "tools", True)
    activation.resolve(ws, non_interactive=False)
    assert answers[0] == "  Activate 'tools'? (clone + install) [Y/n]"


@pytest.mark.slow
def test_a_workspace_without_optional_projects_asks_nothing(ws: Path, answers: List[str]) -> None:
    (ws / "workspace.toml").write_text('[project."models"]\nurl = "u"\n')
    activation.resolve(ws, non_interactive=False)
    assert answers == []


# --- setup brings the folders in line ----------------------------------------------------------------------------


@pytest.mark.slow
def test_reconcile_checks_out_the_active_empties_the_inactive_and_tells_git(ws: Path) -> None:
    activation.reconcile(ws)
    assert list((ws / "tools").iterdir()) == []  # default off, never answered
    assert (ws / "extra" / "pyproject.toml").is_file()  # default on: added from its url
    assert _git(ws / "extra", "branch", "--show-current") == "dev"  # on its branch
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.tools.active") == "false"
    assert _get(ws, "config", "-f", ".gitmodules", "submodule.extra.active") is None  # default on: no key
    assert _git(ws, "config", "--local", "submodule.tools.active") == "false"
    assert _git(ws, "config", "--local", "submodule.extra.active") == "true"


@pytest.mark.slow
def test_switching_a_project_off_empties_its_folder_and_the_choice_survives_the_deinit(ws: Path) -> None:
    activation.set_machine_choice(ws, "models", False)
    activation.reconcile(ws)
    assert list((ws / "models").iterdir()) == []
    assert activation.machine_choice(ws, "models") is False  # git dropped its own section, not ours
    activation.set_machine_choice(ws, "models", True)
    activation.reconcile(ws)
    assert (ws / "models" / "pyproject.toml").is_file()


@pytest.mark.slow
def test_a_folder_with_uncommitted_work_is_never_emptied(ws: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (ws / "models" / "pyproject.toml").write_text("# edited\n")
    activation.set_machine_choice(ws, "models", False)
    activation.reconcile(ws)
    assert (ws / "models" / "pyproject.toml").read_text() == "# edited\n"
    assert "skip models: deinit refused" in capsys.readouterr().out


@pytest.mark.slow
def test_a_project_on_a_package_source_stays_empty_whatever_its_choice(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aisland.workspace.activation.detached_projects", lambda root: {"models"})
    activation.reconcile(ws)
    assert list((ws / "models").iterdir()) == []
    assert activation.machine_choice(ws, "models") is None  # the source emptied it, no choice was made


# --- `aisland workspace configure active` ------------------------------------------------------------------------


@pytest.fixture
def at(ws: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(locations, "workspace_root", lambda: ws)
    return ws


def _configure(*args: str) -> str:
    result = CliRunner().invoke(cli.main, ["workspace", "configure", "active", *args])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.slow
def test_the_listing_shows_the_optional_projects_and_this_machines_choices(at: Path) -> None:
    activation.set_machine_choice(at, "models", False)
    assert _configure().splitlines() == [
        "  PROJECT  COMMITTED  THIS MACHINE  FOLDER",
        "  models   -          false         checked out",
        "  tools    false      -             checked out",
        "  extra    true       -             empty",
    ]


@pytest.mark.slow
def test_set_machine_only_records_this_machines_choice_and_applies_it(at: Path) -> None:
    _configure("set", "models", "false", "--machine-only")
    assert activation.machine_choice(at, "models") is False
    assert list((at / "models").iterdir()) == []
    assert "active" not in workspace_file.load(at).projects["models"].others


@pytest.mark.slow
def test_set_makes_a_project_optional_with_that_default(at: Path) -> None:
    _configure("set", "models", "false")
    assert workspace_file.load(at).projects["models"].others == {"active": False}
    assert workspace_file.load(at).projects["tools"].others == {"active": False}  # the others' keys kept
    assert _git(at, "config", "-f", ".gitmodules", "submodule.models.active") == "false"
    assert list((at / "models").iterdir()) == []


@pytest.mark.slow
def test_unset_makes_a_project_always_part_of_the_workspace_again(at: Path) -> None:
    _configure("unset", "tools")
    assert "active" not in workspace_file.load(at).projects["tools"].others
    assert _get(at, "config", "-f", ".gitmodules", "submodule.tools.active") is None
    assert (at / "tools" / "pyproject.toml").is_file()


@pytest.mark.slow
def test_unset_machine_only_forgets_this_machines_choice(at: Path) -> None:
    activation.set_machine_choice(at, "tools", True)
    _configure("unset", "tools", "--machine-only")
    assert activation.machine_choice(at, "tools") is None


@pytest.mark.slow
def test_set_refuses_a_project_the_file_does_not_list(at: Path) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "configure", "active", "set", "gamma", "true"])
    assert result.exit_code == 1
    assert "'gamma' is no project of workspace.toml (the projects: models, tools, extra)" in result.output


def test_active_is_a_plugin_of_configure() -> None:
    from aisland.setup import configure

    plugin = configure.PLUGINS["active"]
    assert (plugin.main.name, plugin.main.choices, plugin.options) == ("active", ("true", "false"), ())


# --- the commands around it --------------------------------------------------------------------------------------


@pytest.mark.slow
def test_init_from_clones_only_the_projects_without_an_active_key(tmp_path: Path, ws: Path) -> None:
    result = CliRunner().invoke(
        cli.main, ["workspace", "init", str(tmp_path / "fresh"), "--from", str(ws / "workspace.toml")]
    )
    assert result.exit_code == 0, result.output
    fresh = tmp_path / "fresh"
    assert (fresh / "models" / "pyproject.toml").is_file()
    assert not (fresh / "tools").exists() and not (fresh / "extra").exists()
    assert list(workspace_file.load(fresh).projects) == ["models", "tools", "extra"]  # the optional ones stay listed


@pytest.mark.slow
def test_setup_keeps_an_optional_project_the_workspace_does_not_have_yet(ws: Path) -> None:
    """`extra` (optional, not checked out) stays listed; a project with no url to clone from is dropped."""
    (ws / "workspace.toml").write_text((ws / "workspace.toml").read_text() + '\n[project."gone"]\npath = "gone"\n')
    lines = workspace_file.refresh(ws)
    assert lines == ["workspace.toml: dropped gone (no longer in the workspace)"]
    assert list(workspace_file.load(ws).projects) == ["models", "tools", "extra"]


def _write_lib(ws: Path, tmp_path: Path) -> None:
    lib = _remote(tmp_path, "lib")
    (ws / "workspace.toml").write_text(
        (ws / "workspace.toml").read_text() + f'\n[project."lib"]\nurl = "{lib}"\nbranch = "main"\n'
    )


def test_a_project_written_into_the_file_by_hand_is_kept_for_setup_to_add(ws: Path, tmp_path: Path) -> None:
    """The file is a way to add a project: a listed project with a url the workspace lacks is not gone (user choice
    2026-10-28, release review 3a). To remove one, its row goes too."""
    _write_lib(ws, tmp_path)
    before = (ws / "workspace.toml").read_text()
    assert workspace_file.refresh(ws) == ["workspace.toml: lib is not in the workspace yet; setup adds it"]
    assert (ws / "workspace.toml").read_text() == before


def test_setup_clones_a_project_written_into_the_file_by_hand(
    ws: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_lib(ws, tmp_path)
    workspace_file.refresh(ws)
    assert activation.reconcile(ws) == []
    assert "  - add lib (active)" in capsys.readouterr().out
    assert (ws / "lib" / "pyproject.toml").is_file()
    assert _git(ws, "config", "-f", ".gitmodules", "submodule.lib.branch") == "main"
    assert workspace_file.refresh(ws) == []  # listed and present: nothing more to say


@pytest.mark.parametrize("flag", ["--activate", "--deactivate"])
def test_setup_has_no_activation_flags(flag: str) -> None:
    result = CliRunner().invoke(cli.main, ["setup", flag, "models"])
    assert result.exit_code == 2
    assert f"No such option '{flag}'" in result.output or f"No such option: {flag}" in result.output  # click >= / < 8.4


def test_setup_asks_then_reconciles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    calls: List[str] = []

    class _Stop(Exception):
        pass

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(manager, "link_project_bashrc", lambda root: None)
    monkeypatch.setattr(manager, "install_harness", lambda root: [])
    monkeypatch.setattr("aisland.setup.provision.install", lambda root, aislands_own=True: None)

    def resolve(root: Path, non_interactive: bool) -> None:
        calls.append(f"resolve {non_interactive}")

    def reconcile(root: Path) -> List[str]:
        calls.append("reconcile")
        return []

    monkeypatch.setattr(activation, "resolve", resolve)
    monkeypatch.setattr(activation, "reconcile", reconcile)

    def apply_all(*args: object, **kwargs: object) -> None:
        raise _Stop

    monkeypatch.setattr("aisland.setup.manager.apply_all", apply_all)
    with pytest.raises(_Stop):
        manager.setup_workspace(non_interactive=True)
    assert calls == ["resolve True", "reconcile"]


# --- the smaller cases -----------------------------------------------------------------------------------------


def test_a_workspace_without_the_file_has_nothing_to_bring_in_line(tmp_path: Path) -> None:
    assert activation.reconcile(tmp_path) == []


@pytest.mark.slow
def test_an_optional_project_switched_off_before_it_was_ever_added_is_left_alone(ws: Path) -> None:
    activation.set_machine_choice(ws, "extra", False)
    assert activation.reconcile(ws) == []
    assert not (ws / "extra").exists()
    assert "extra" not in _git(ws, "config", "-f", ".gitmodules", "--get-regexp", r"\.path$")


@pytest.mark.slow
def test_an_active_project_that_cannot_be_added_is_said_and_returned(
    ws: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (ws / "workspace.toml").write_text(
        '[project."models"]\nurl = "u"\n\n[project."nourl"]\nactive = true\n\n'
        '[project."bad"]\nurl = "/no/such/repository.git"\nactive = true\n'
    )
    assert activation.reconcile(ws) == ["nourl", "bad"]
    out = capsys.readouterr().out
    assert "skip nourl: active, but workspace.toml gives it no url to clone from." in out
    assert "skip bad: " in out


@pytest.mark.slow
def test_the_listing_leaves_out_a_project_that_is_neither_optional_nor_chosen(at: Path) -> None:
    assert [line.split()[0] for line in _configure().splitlines()[1:]] == ["tools", "extra"]


@pytest.mark.slow
def test_unset_refuses_a_project_the_file_does_not_list(at: Path) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "configure", "active", "unset", "gamma"])
    assert result.exit_code == 1
    assert "'gamma' is no project of workspace.toml" in result.output
