"""Stage 1 and stage 2 of the workspace redesign: aisland finds what the projects declare.

STAGE 1 reads every project's ``pyproject.toml`` — nothing needs to be installed: a selection
(``[tool.aisland.selection.<name>]``: the table with ``question`` defines it, every project's ``choices`` are its
menu), a service (``[tool.aisland.service.<name>]``: a command, a port, a readiness path) and the code plugins a
project declares (``[project.entry-points."aisland.plugins"]``). STAGE 2 loads those entry points once the project
is installed, and checks each against the protocol of its kind. A project never imports aisland for either.

Every test builds a workspace without ``.gitmodules`` whose subfolders are the projects (`aisland.setup.manager
.workspace_folders` reads it as if each were a submodule), and every refusal names the file and the line.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Dict

import pytest
from click.testing import CliRunner

from aisland import cli, locations
from aisland.workspace import discovery

MATRAINER = """\
[project]
name = "matrainer"
version = "0.1.0"

[project.optional-dependencies]
torch = []
keras = []
mlflow = []
aim = []

[tool.aisland.selection.framework]
question = "Which ML frameworks should this machine install?"
choices = ["torch", "keras"]

[tool.aisland.selection.tracker]
question = "Which experiment-tracking backends should this machine install?"
choices = ["mlflow", "aim"]

[tool.aisland.service.aim]
command = ["aim", "up", "--repo", "{data}", "--port", "{port}"]
port = 43800
ready = "/"
"""

SONAIR = """\
[project]
name = "sonair"
version = "0.1.0"

[project.optional-dependencies]
torch = []

[tool.aisland.selection.framework]
choices = ["torch"]
"""


def _workspace(tmp_path: Path, projects: Dict[str, str]) -> Path:
    """A workspace at ``tmp_path / "ws"`` holding one folder per project, each with the given ``pyproject.toml``."""
    root = tmp_path / "ws"
    for name, text in projects.items():
        (root / name).mkdir(parents=True)
        (root / name / "pyproject.toml").write_text(textwrap.dedent(text))
    return root


def _refusal(root: Path) -> str:
    with pytest.raises(discovery.DeclarationError) as refused:
        discovery.discover(root)
    return refused.value.message


# --- stage 1: selections -------------------------------------------------------------------------------------------


def test_a_selection_is_defined_by_the_project_that_asks_its_question(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"matrainer": MATRAINER})
    found = discovery.discover(root)
    framework = found.selections["framework"]
    assert framework.question == "Which ML frameworks should this machine install?"
    assert framework.defined_by.project == "matrainer"
    assert str(framework.defined_by.where) == f"{root / 'matrainer' / 'pyproject.toml'}:11"
    assert sorted(found.selections) == ["framework", "tracker"]


def test_every_projects_choices_for_a_selection_are_collected(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"matrainer": MATRAINER, "sonair": SONAIR})
    framework = discovery.discover(root).selections["framework"]
    assert framework.choices == {"matrainer": ["torch", "keras"], "sonair": ["torch"]}


def test_two_projects_asking_one_selections_question_are_refused_naming_both(tmp_path: Path) -> None:
    second = SONAIR.replace('choices = ["torch"]', 'question = "Frameworks?"\nchoices = ["torch"]')
    root = _workspace(tmp_path, {"matrainer": MATRAINER, "sonair": second})
    assert _refusal(root) == (
        f"selection 'framework' is defined twice: {root / 'matrainer' / 'pyproject.toml'}:11 and "
        f"{root / 'sonair' / 'pyproject.toml'}:8 both ask its question — keep it in one project"
    )


def test_choices_for_a_selection_no_project_defines_are_reported_not_refused(tmp_path: Path) -> None:
    """A workspace without matrainer (or with it switched off) still sets up: the selection does not exist there."""
    root = _workspace(tmp_path, {"sonair": SONAIR})
    found = discovery.discover(root)
    assert found.selections == {}
    assert found.undefined == {"framework": ["sonair"]}


def test_a_choice_that_is_no_extra_of_its_project_is_refused_naming_the_line(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"matrainer": MATRAINER, "sonair": SONAIR.replace('["torch"]\n', '["torch", "jax"]\n')})
    assert _refusal(root) == (
        f"{root / 'sonair' / 'pyproject.toml'}:8: sonair offers 'jax' for selection 'framework', "
        "but defines no extra of that name"
    )


# --- stage 1: services ---------------------------------------------------------------------------------------------


def test_a_service_declares_its_command_port_and_readiness(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"matrainer": MATRAINER})
    aim = discovery.discover(root).services["aim"]
    assert (aim.project, aim.port, aim.ready) == ("matrainer", 43800, "/")
    assert aim.command == ("aim", "up", "--repo", "{data}", "--port", "{port}")
    assert str(aim.where) == f"{root / 'matrainer' / 'pyproject.toml'}:19"


def test_a_service_without_a_command_is_refused_naming_its_line(tmp_path: Path) -> None:
    text = MATRAINER.replace('command = ["aim", "up", "--repo", "{data}", "--port", "{port}"]\n', "")
    root = _workspace(tmp_path, {"matrainer": text})
    assert _refusal(root) == (
        f"{root / 'matrainer' / 'pyproject.toml'}:19: service 'aim' has no command — "
        'write it as  command = ["<program>", "<argument>", …]'
    )


def test_a_service_with_an_unknown_setting_is_refused_naming_its_line(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"matrainer": MATRAINER.replace('ready = "/"', 'ready = "/"\nhealth = "/x"')})
    assert _refusal(root) == (
        f"{root / 'matrainer' / 'pyproject.toml'}:19: service 'aim' has the unknown setting 'health' "
        "(a service takes command, port, ready and needs)"
    )


def test_two_projects_declaring_one_service_are_refused_naming_both(tmp_path: Path) -> None:
    other = SONAIR + '\n[tool.aisland.service.aim]\ncommand = ["aim", "up"]\n'
    root = _workspace(tmp_path, {"matrainer": MATRAINER, "sonair": other})
    assert _refusal(root) == (
        f"service 'aim' is declared twice: {root / 'matrainer' / 'pyproject.toml'}:19 and "
        f"{root / 'sonair' / 'pyproject.toml'}:11 — keep it in one project"
    )


# --- stage 1: what is read, and from where -------------------------------------------------------------------------


def test_a_project_without_declarations_contributes_nothing(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"plain": '[project]\nname = "plain"\nversion = "0.1.0"\n'})
    found = discovery.discover(root)
    assert (found.selections, found.services, found.plugins, found.undefined) == ({}, {}, {}, {})


def test_an_empty_project_folder_is_skipped(tmp_path: Path) -> None:
    """A detached or switched-off submodule's folder is empty: it declares nothing until it is checked out."""
    root = _workspace(tmp_path, {"matrainer": MATRAINER})
    (root / "loggair").mkdir()
    assert sorted(discovery.discover(root).selections) == ["framework", "tracker"]


def test_a_project_reached_through_a_link_is_read_once(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"matrainer": MATRAINER})
    (root / "mt").symlink_to("matrainer")
    found = discovery.discover(root)
    assert found.selections["framework"].choices == {"matrainer": ["torch", "keras"]}


def test_a_malformed_pyproject_is_refused_naming_its_file_and_line(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"matrainer": MATRAINER.replace("port = 43800", "port = ")})
    message = _refusal(root)
    assert message.startswith(f"{root / 'matrainer' / 'pyproject.toml'}: ")
    assert "line 21" in message


# --- stage 2: code plugins -----------------------------------------------------------------------------------------

PLUGIN_PROJECT = """\
[project]
name = "acme"
version = "0.1.0"

[project.entry-points."aisland.plugins"]
acme-db = "acme_plugin:Database"
"""

#: A service implemented in the project's own code: a plain class, no aisland import.
PLUGIN_MODULE = """\
class Database:
    kind = "service"

    def start(self, ctx):
        ctx.echo("started")

    def stop(self, ctx):
        ctx.echo("stopped")

    def status(self, ctx):
        return {"running": False}
"""


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str = PLUGIN_MODULE) -> None:
    """Install the ``acme`` project the way a real install leaves it: its module and a dist-info declaring the entry
    point, in a folder on the import path."""
    site = tmp_path / "site"
    (site / "acme-0.1.0.dist-info").mkdir(parents=True)
    (site / "acme-0.1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: acme\nVersion: 0.1.0\n")
    (site / "acme-0.1.0.dist-info" / "entry_points.txt").write_text(
        "[aisland.plugins]\nacme-db = acme_plugin:Database\n"
    )
    (site / "acme_plugin.py").write_text(module)
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, "acme_plugin", raising=False)


def test_a_declared_plugin_is_found_before_it_is_installed(tmp_path: Path) -> None:
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT})
    found = discovery.discover(root)
    plugin = found.plugins["acme-db"]
    assert (plugin.project, plugin.target) == ("acme", "acme_plugin:Database")
    assert str(plugin.where) == f"{root / 'acme' / 'pyproject.toml'}:6"
    assert discovery.load(found)["acme-db"].state == "not installed"


def test_an_installed_plugin_is_loaded_and_has_its_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT})
    _install(tmp_path, monkeypatch)
    loaded = discovery.load(discovery.discover(root))["acme-db"]
    assert (loaded.state, loaded.kind) == ("installed", "service")
    assert loaded.obj.status(None) == {"running": False}


def test_an_installed_plugin_missing_a_method_of_its_kind_is_refused_naming_its_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT})
    _install(
        tmp_path, monkeypatch, PLUGIN_MODULE.replace('    def start(self, ctx):\n        ctx.echo("started")\n', "")
    )
    with pytest.raises(discovery.DeclarationError) as refused:
        discovery.load(discovery.discover(root))
    assert refused.value.message == (
        f"{root / 'acme' / 'pyproject.toml'}:6: plugin 'acme-db' (acme_plugin:Database) is a service "
        "but has no start(ctx)"
    )


def test_a_service_plugin_needs_only_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """stop, status, info and reset are aisland's own when the class has none (`services/plugin.py`)."""
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT})
    module = PLUGIN_MODULE.replace('    def stop(self, ctx):\n        ctx.echo("stopped")\n', "")
    _install(
        tmp_path, monkeypatch, module.replace('    def status(self, ctx):\n        return {"running": False}\n', "")
    )
    assert discovery.load(discovery.discover(root))["acme-db"].kind == "service"


def test_an_installed_plugin_of_an_unknown_kind_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT})
    _install(tmp_path, monkeypatch, PLUGIN_MODULE.replace('kind = "service"', 'kind = "widget"'))
    with pytest.raises(discovery.DeclarationError) as refused:
        discovery.load(discovery.discover(root))
    assert refused.value.message == (
        f"{root / 'acme' / 'pyproject.toml'}:6: plugin 'acme-db' (acme_plugin:Database) has kind 'widget' "
        "(a plugin's kind is service)"
    )


def test_an_entry_point_of_a_package_outside_the_workspace_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only what a workspace project declares is loaded: an installed package nobody in the workspace declares is
    not a plugin of this workspace."""
    root = _workspace(tmp_path, {"plain": '[project]\nname = "plain"\nversion = "0.1.0"\n'})
    _install(tmp_path, monkeypatch)
    assert discovery.load(discovery.discover(root)) == {}


# --- `aisland workspace show` --------------------------------------------------------------------------------------


@pytest.fixture
def shown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _workspace(tmp_path, {"matrainer": MATRAINER, "sonair": SONAIR, "acme": PLUGIN_PROJECT})
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return root


def test_workspace_show_lists_what_the_projects_declare(shown: Path) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "show"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Declared by the projects:",
        "  selection  framework  matrainer  choices: torch, keras (matrainer), torch (sonair)",
        "  selection  tracker    matrainer  choices: mlflow, aim (matrainer)",
        "  service    aim        matrainer  port 43800",
        "  plugin     acme-db    acme       acme_plugin:Database — not installed",
    ]


def test_workspace_show_json_carries_the_same_data(shown: Path) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "show", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["selections"]["framework"]["choices"] == {"matrainer": ["torch", "keras"], "sonair": ["torch"]}
    assert data["selections"]["framework"]["defined_by"] == {
        "project": "matrainer",
        "file": os.fspath(shown / "matrainer" / "pyproject.toml"),
        "line": 11,
    }
    assert data["services"]["aim"]["port"] == 43800
    assert data["plugins"]["acme-db"] == {
        "project": "acme",
        "target": "acme_plugin:Database",
        "state": "not installed",
        "file": os.fspath(shown / "acme" / "pyproject.toml"),
        "line": 6,
    }


def test_workspace_show_reports_a_refusal_as_one_error_line(shown: Path) -> None:
    (shown / "sonair" / "pyproject.toml").write_text(SONAIR.replace('["torch"]\n', '["jax"]\n'))
    result = CliRunner().invoke(cli.main, ["workspace", "show"])
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {shown / 'sonair' / 'pyproject.toml'}:8: sonair offers 'jax' for selection 'framework', "
        "but defines no extra of that name\n"
    )


# --- the smaller refusals, and the report's other lines ------------------------------------------------------------

_EXTRAS = '[project]\nname = "p"\nversion = "0.1.0"\n\n[project.optional-dependencies]\ntorch = []\n\n'


@pytest.mark.parametrize(
    "declaration, said",
    [
        ('[tool.aisland]\nselection = { framework = "torch" }\n', "selection 'framework' is not a table"),
        (
            '[tool.aisland.selection.framework]\nchoices = ["torch"]\nhint = "x"\n',
            "selection 'framework' has the unknown setting 'hint' (a selection takes question and choices)",
        ),
        (
            '[tool.aisland.selection.framework]\nchoices = "torch"\n',
            'selection \'framework\': choices must be a list of names, as ["a", "b"]',
        ),
        (
            '[tool.aisland.selection.framework]\nquestion = 3\nchoices = ["torch"]\n',
            "selection 'framework': question must be text, as question = \"…?\"",
        ),
        ('[tool.aisland]\nservice = { db = "x" }\n', "service 'db' is not a table"),
        (
            '[tool.aisland.service.db]\ncommand = ["db"]\nport = "8080"\n',
            "service 'db': port must be a number, as port = 8080",
        ),
        (
            '[tool.aisland.service.db]\ncommand = ["db"]\nready = 1\n',
            "service 'db': ready must be a path, as ready = \"/health\"",
        ),
        (
            '[tool.aisland.service.db]\ncommand = ["db"]\nneeds = { tracker = 1 }\n',
            "service 'db': needs must name a choice per selection, as needs = { tracker = \"aim\" }",
        ),
    ],
)
def test_a_declaration_of_the_wrong_shape_is_refused_saying_what_it_should_be(
    tmp_path: Path, declaration: str, said: str
) -> None:
    root = _workspace(tmp_path, {"p": _EXTRAS + declaration})
    message = _refusal(root)
    assert message.startswith(f"{root / 'p' / 'pyproject.toml'}")
    assert message.endswith(said)


def test_a_table_aisland_cannot_find_in_the_text_is_named_by_its_file_alone(tmp_path: Path) -> None:
    """A table written inline (`selection = { … }`) has no header line: the message names the file without one."""
    root = _workspace(tmp_path, {"p": _EXTRAS + '[tool.aisland]\nselection = { framework = "torch" }\n'})
    assert _refusal(root) == f"{root / 'p' / 'pyproject.toml'}: selection 'framework' is not a table"


def test_two_projects_declaring_one_plugin_are_refused_naming_both(tmp_path: Path) -> None:
    other = PLUGIN_PROJECT.replace('name = "acme"', 'name = "other"')
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT, "other": other})
    assert _refusal(root) == (
        f"plugin 'acme-db' is declared twice: {root / 'acme' / 'pyproject.toml'}:6 and "
        f"{root / 'other' / 'pyproject.toml'}:6 — keep it in one project"
    )


def test_an_entry_point_after_another_table_is_still_found_on_its_line(tmp_path: Path) -> None:
    text = PLUGIN_PROJECT + '\n[tool.other]\nacme-db = "not this one"\n'
    root = _workspace(tmp_path, {"acme": text})
    assert discovery.discover(root).plugins["acme-db"].where.line == 6


def test_an_entry_point_written_inline_is_named_by_its_file(tmp_path: Path) -> None:
    text = '[project]\nname = "acme"\nversion = "0.1.0"\nentry-points = { "aisland.plugins" = { acme-db = "m:O" } }\n'
    root = _workspace(tmp_path, {"acme": text})
    assert str(discovery.discover(root).plugins["acme-db"].where) == str(root / "acme" / "pyproject.toml")


def test_a_submodule_whose_folder_is_empty_declares_nothing(tmp_path: Path) -> None:
    """With `.gitmodules`, a detached or switched-off submodule is listed but its folder is empty."""
    root = _workspace(tmp_path, {"matrainer": MATRAINER})
    (root / "loggair").mkdir()
    (root / ".gitmodules").write_text(
        '[submodule "matrainer"]\n\tpath = matrainer\n\turl = x\n[submodule "loggair"]\n\tpath = loggair\n\turl = y\n'
    )
    assert sorted(discovery.discover(root).selections) == ["framework", "tracker"]


def test_an_installed_entry_point_of_another_package_with_the_plugins_name_is_not_this_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT.replace('name = "acme"', 'name = "acme-two"')})
    _install(tmp_path, monkeypatch)  # installs distribution `acme`, declaring the same entry-point name
    assert discovery.load(discovery.discover(root))["acme-db"].state == "not installed"


def test_a_plugin_whose_code_fails_to_load_is_refused_with_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path, {"acme": PLUGIN_PROJECT})
    _install(tmp_path, monkeypatch, "raise RuntimeError('no database here')\n")
    with pytest.raises(discovery.DeclarationError) as refused:
        discovery.load(discovery.discover(root))
    assert refused.value.message == (
        f"{root / 'acme' / 'pyproject.toml'}:6: plugin 'acme-db' (acme_plugin:Database) cannot be loaded: "
        "no database here"
    )


def test_workspace_show_names_an_installed_plugins_kind(
    shown: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.main, ["workspace", "show"])
    assert "  plugin     acme-db    acme       acme_plugin:Database — service" in result.output.splitlines()


def test_workspace_show_says_when_nothing_is_declared_and_names_undefined_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path, {"sonair": SONAIR})
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    result = CliRunner().invoke(cli.main, ["workspace", "show"])
    assert result.output.splitlines() == [
        "Declared by the projects: nothing",
        "Choices for a selection no project defines: framework (sonair)",
    ]


def test_a_service_without_a_port_shows_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path, {"p": _EXTRAS + '[tool.aisland.service.db]\ncommand = ["db"]\n'})
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    result = CliRunner().invoke(cli.main, ["workspace", "show"])
    assert result.output.splitlines() == ["Declared by the projects:", "  service  db  p  no port"]


# --- click commands --------------------------------------------------------------------------------------------------

_SCRIPTS = '[project]\nname = "tools"\n\n[project.scripts]\ntool = "tools.cli:main"\nother = "tools.cli:other"\n\n'


def test_a_projects_click_commands_are_its_declared_scripts_each_once(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path,
        {
            "tools": _SCRIPTS + '[tool.aisland]\nclick-commands = ["tool", "other"]\n',
            "more": _SCRIPTS + '[tool.aisland]\nclick-commands = ["tool"]\n',
        },
    )
    assert sorted(discovery.discover(root).click_commands) == ["other", "tool"]


@pytest.mark.parametrize(
    "declaration, message",
    [
        ('click-commands = "tool"', 'click-commands must be a list of script names, as ["acme"]'),
        ('click-commands = ["missing"]', "tools declares no script 'missing' in [project.scripts]"),
    ],
)
def test_a_click_command_that_is_no_script_of_the_project_is_refused_naming_its_line(
    tmp_path: Path, declaration: str, message: str
) -> None:
    root = _workspace(tmp_path, {"tools": _SCRIPTS + f"[tool.aisland]\n{declaration}\n"})
    assert _refusal(root) == f"{root / 'tools' / 'pyproject.toml'}:9: {message}"
