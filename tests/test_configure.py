"""`aisland workspace configure`: one set of commands for every plugin of the workspace.

A plugin declares its keys; the commands are built from them — `configure <plugin>` lists, `set PROJECT VALUE
[--<key> VALUE…] [--machine-only]` and `unset PROJECT [--machine-only]` change — so a plugin writes no command of
its own. The first plugin is `source`, where each project's package comes from: it replaced the `aisland workspace configure source`
group, its settings stored in `workspace.toml` and `.git/config` (`tests/test_sources.py`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner, Result

from aisland import cli, locations
from aisland.setup import configure, source_commands
from aisland.workspace import sources as s


def _run(*args: str) -> Result:
    return CliRunner().invoke(cli.main, ["workspace", "configure", *args])


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    return tmp_path


# --- the commands, built from a plugin's keys ---------------------------------------------------------------------


@pytest.fixture
def fake(root: Path, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """A plugin `colour` with a main key and one option, recording what the commands call."""
    calls: List[Any] = []

    def set_(workspace: Path, project: str, value: str, options: Dict[str, Optional[str]], machine_only: bool) -> int:
        calls.append(("set", workspace, project, value, options, machine_only))
        return 0

    def unset(workspace: Path, project: str, machine_only: bool) -> int:
        calls.append(("unset", workspace, project, machine_only))
        return 3

    plugin = configure.Plugin(
        name="colour",
        summary="Each project's colour.",
        main=configure.Key("colour", "The colour.", choices=("red", "blue")),
        options=(configure.Key("shade", "How dark."),),
        rows=lambda workspace: [{"project": "models", "colour": "red"}, {"project": "tools", "colour": "blue"}],
        set=set_,
        unset=unset,
        empty="No project has a colour.",
        notes=lambda workspace: ["a note under the table"],
    )
    monkeypatch.setitem(configure.PLUGINS, "colour", plugin)
    return calls


def test_configure_alone_lists_every_plugin_and_how_to_set_it(fake: List[Any]) -> None:
    result = _run()
    assert result.exit_code == 0, result.output
    assert "  colour  Each project's colour." in result.output.splitlines()
    assert "          set PROJECT {red|blue} [--shade SHADE] [--machine-only]" in result.output.splitlines()


def test_a_plugin_alone_lists_its_rows(fake: List[Any]) -> None:
    result = _run("colour")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "  PROJECT  COLOUR",
        "  models   red",
        "  tools    blue",
        "a note under the table",
    ]


def test_a_plugins_rows_as_json(fake: List[Any]) -> None:
    result = _run("colour", "--json")
    assert json.loads(result.output) == {
        "rows": [{"project": "models", "colour": "red"}, {"project": "tools", "colour": "blue"}],
        "notes": ["a note under the table"],
    }


def test_set_passes_the_value_and_options_and_exits_with_the_plugins_code(root: Path, fake: List[Any]) -> None:
    result = _run("colour", "set", "models", "red", "--shade", "dark", "--machine-only")
    assert result.exit_code == 0, result.output
    assert fake == [("set", root, "models", "red", {"shade": "dark"}, True)]


def test_set_refuses_a_value_outside_the_keys_choices(fake: List[Any]) -> None:
    result = _run("colour", "set", "models", "green")
    assert result.exit_code == 2
    assert "'green' is not one of 'red', 'blue'" in result.output
    assert fake == []


def test_unset_exits_with_the_plugins_code(root: Path, fake: List[Any]) -> None:
    result = _run("colour", "unset", "tools")
    assert result.exit_code == 3
    assert fake == [("unset", root, "tools", False)]


def test_a_plugin_with_nothing_to_list_says_so(fake: List[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = configure.PLUGINS["colour"]
    monkeypatch.setitem(
        configure.PLUGINS, "colour", configure.Plugin(**{**plugin.__dict__, "rows": lambda workspace: []})
    )
    assert _run("colour").output.splitlines() == ["No project has a colour.", "a note under the table"]


def test_an_unknown_plugin_is_refused_naming_the_plugins(fake: List[Any]) -> None:
    result = _run("flavour")
    assert result.exit_code == 2
    assert "No such command 'flavour'" in result.output


# --- source ----------------------------------------------------------------------------------------------------


def test_mirror_is_a_plugin_of_configure() -> None:
    plugin = configure.PLUGINS["mirror"]
    assert (plugin.main.name, plugin.main.choices, plugin.options) == ("mirror", None, ())
    assert plugin.usage() == "set PROJECT MIRROR [--machine-only]"


def test_source_is_a_plugin_of_configure() -> None:
    plugin = configure.PLUGINS["source"]
    assert plugin.main.name == "source" and plugin.main.choices == ("local", "github", "pypi")
    assert [key.name for key in plugin.options] == ["branch", "version"]


def test_configure_source_lists_each_projects_source(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"project": "loggair", "committed": "pypi", "override": "-", "effective": "pypi", "state": "detached"}
    monkeypatch.setattr(s, "describe", lambda workspace_root=None: [row])
    result = _run("source")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "  PROJECT  COMMITTED  OVERRIDE  EFFECTIVE  STATE",
        "  loggair  pypi       -         pypi       detached",
    ]


@pytest.mark.parametrize(
    "args, called",
    [
        (["loggair", "pypi"], dict(project="loggair", kind="pypi", ref=None, version=None, machine_only=False)),
        (
            ["loggair", "local", "--branch", "dev/main", "--machine-only"],
            dict(project="loggair", kind="local", ref="dev/main", version=None, machine_only=True),
        ),
        (
            ["loggair", "pypi", "--version", ">=0.1"],
            dict(project="loggair", kind="pypi", ref=None, version=">=0.1", machine_only=False),
        ),
    ],
)
def test_configure_source_set_sets_and_applies_the_source(
    root: Path, monkeypatch: pytest.MonkeyPatch, args: List[str], called: Dict[str, Any]
) -> None:
    seen: List[Dict[str, Any]] = []

    def set_project_source(**kwargs: Any) -> int:
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(source_commands, "set_project_source", set_project_source)
    result = _run("source", "set", *args)
    assert result.exit_code == 0, result.output
    assert seen == [{**called, "workspace_root": root}]


def test_configure_source_unset(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Dict[str, Any]] = []

    def unset_project_source(**kwargs: Any) -> int:
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(source_commands, "unset_project_source", unset_project_source)
    assert _run("source", "unset", "loggair", "--machine-only").exit_code == 0
    assert seen == [{"project": "loggair", "machine_only": True, "workspace_root": root}]


@pytest.mark.parametrize("command", [["source", "list"], ["source", "apply"]])
@pytest.mark.usefixtures("empty_workspace")
def test_the_source_group_is_gone(command: List[str]) -> None:
    result = CliRunner().invoke(cli.main, command)
    assert result.exit_code == 2
    assert "No such command 'source'" in result.output


def test_a_warning_goes_to_stderr_and_leaves_the_json_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A script reads `--json` from stdout: a warning about a setting it cannot use goes to stderr."""
    (tmp_path / "workspace.toml").write_text('[workspace]\nformat = 1\n\n[project."lib"]\nsource = "pypy"\n')
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "pyproject.toml").write_text('[project]\nname = "lib"\nversion = "0.1.0"\n')
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    result = _run("source", "--json")
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert "has source 'pypy'; expected one of local, github, pypi" in result.stderr
