"""A service a project implements in its own code: an `aisland.plugins` entry point of kind `service`.

The project imports nothing of aisland. Its class needs only `start(ctx)`; `stop`, `status`, `info` and `reset` are
aisland's own unless the class has them (`info(ctx)` adds lines after aisland's). The context aisland passes:

    ctx.name, ctx.workspace, ctx.data_dir        the service's name, the workspace, its data folder
    ctx.port                                     `--port` when given, else None (the class picks its default)
    ctx.background                               `-b`
    ctx.run(command, env=, port=, ready=)        run under the supervisor; with a port and a ready path, probed
    ctx.write_env({KEY: value})                  set keys in the workspace .env
    ctx.ports()                                  the ports the supervisor knows for it
    ctx.echo(text), ctx.fail(message)            print; stop with one `Error:` line

It joins `aisland services` (started when named) and gets `aisland <name> start|stop|restart|status|info|reset`, with
`--port` on start and restart.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner, Result

from aisland import cli, manager
from aisland.services import registry

PROJECT = """\
    [project]
    name = "acme"
    version = "0.1.0"

    [project.entry-points."aisland.plugins"]
    acme-db = "acme_plugin:Database"
"""

#: Records what aisland handed it in the module's `SEEN` list.
MODULE = """\
SEEN = []


class Database:
    kind = "service"

    def start(self, ctx):
        SEEN.append(("start", ctx.name, ctx.port, ctx.background, ctx.workspace, ctx.data_dir))
        if ctx.port == 666:
            ctx.fail("port 666 is not for databases")
        ctx.write_env({"ACME_DB_URL": f"http://localhost:{ctx.port or 7000}"})
        ctx.run(["acme-db", "--port", str(ctx.port or 7000)], env={"ACME": "1"}, port=ctx.port or 7000, ready="/ping")

    def info(self, ctx):
        ctx.echo(f"  Tables        : 3 (ports {ctx.ports()})")
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace whose `acme` project declares the `acme-db` service, installed the way a real install leaves it."""
    root = tmp_path / "ws"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / "pyproject.toml").write_text(textwrap.dedent(PROJECT))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    site = tmp_path / "site"
    (site / "acme-0.1.0.dist-info").mkdir(parents=True)
    (site / "acme-0.1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: acme\nVersion: 0.1.0\n")
    (site / "acme-0.1.0.dist-info" / "entry_points.txt").write_text(
        "[aisland.plugins]\nacme-db = acme_plugin:Database\n"
    )
    (site / "acme_plugin.py").write_text(MODULE)
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, "acme_plugin", raising=False)
    monkeypatch.setattr(cli, "get_workspace_root", lambda: root)
    monkeypatch.setattr(manager, "get_workspace_root", lambda: root)
    monkeypatch.setattr("aisland.supervisor._workspace_root", lambda: root)
    monkeypatch.setattr(registry, "ENTRIES", ())
    monkeypatch.setattr("aisland.supervisor.print_status", lambda: None)
    return root


@pytest.fixture
def supervised(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def start(
        self: manager.BaseService,
        cmd: List[str],
        env: Optional[dict] = None,
        background: bool = False,
        readiness: Optional[Dict[str, Any]] = None,
    ) -> None:
        calls.append({"start": self.name, "cmd": cmd, "env": env, "background": background, "readiness": readiness})

    monkeypatch.setattr(manager.BaseService, "start", start)
    monkeypatch.setattr(manager.BaseService, "stop", lambda self: calls.append({"stop": self.name}))
    monkeypatch.setattr(manager.BaseService, "status", lambda self: calls.append({"status": self.name}))
    monkeypatch.setattr(manager.BaseService, "reset", lambda self: calls.append({"reset": self.name}))
    monkeypatch.setattr(manager.BaseService, "info", lambda self: calls.append({"info": self.name}))
    monkeypatch.setattr(manager.BaseService, "get_ports", lambda self: [7000])
    return calls


def _seen() -> List[Any]:
    return list(sys.modules["acme_plugin"].SEEN)  # type: ignore[attr-defined]


def _run(*args: str, input: str = "") -> Result:
    return CliRunner().invoke(cli.main, list(args), input=input)


# --- its commands -------------------------------------------------------------------------------------------------


def test_the_service_gets_its_own_commands_with_a_port_option(workspace: Path) -> None:
    result = _run("acme-db", "--help")
    assert result.exit_code == 0, result.output
    for command in ("start", "stop", "restart", "status", "info", "reset"):
        assert f"  {command} " in result.output
    assert "--port" in _run("acme-db", "start", "--help").output
    assert "--port" in _run("acme-db", "restart", "--help").output


def test_its_help_names_the_project_that_brings_it(workspace: Path) -> None:
    assert "Manage acme-db, a service acme brings." in _run("acme-db", "--help").output
    assert "Manage acme-db, a service acme brings." in " ".join(_run("--help").output.split())


def test_start_hands_the_class_its_context_and_runs_what_it_asks(
    workspace: Path, supervised: List[Dict[str, Any]]
) -> None:
    result = _run("acme-db", "start", "-b", "--port", "7100")
    assert result.exit_code == 0, result.output
    assert _seen() == [("start", "acme-db", 7100, True, workspace, manager.DATA_DIR / "acme-db")]
    assert supervised == [
        {
            "start": "acme-db",
            "cmd": ["acme-db", "--port", "7100"],
            "env": {"ACME": "1"},
            "background": True,
            "readiness": {"port": 7100, "path": "/ping"},
        }
    ]
    assert "ACME_DB_URL=http://localhost:7100\n" in (workspace / ".env").read_text()


def test_without_port_the_class_picks_its_own(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    assert _run("acme-db", "start", "-b").exit_code == 0
    assert _seen()[0][2] is None
    assert supervised[0]["cmd"] == ["acme-db", "--port", "7000"]


def test_fail_is_one_error_line_and_nothing_runs(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    result = _run("acme-db", "start", "--port", "666")
    assert result.exit_code == 1
    assert result.output == "Error: port 666 is not for databases\n"
    assert supervised == []


def test_what_the_class_does_not_implement_is_aislands_own(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    assert _run("acme-db", "stop").exit_code == 0
    assert _run("acme-db", "status").exit_code == 0
    assert _run("acme-db", "reset", input="y\n").exit_code == 0
    assert supervised == [{"stop": "acme-db"}, {"status": "acme-db"}, {"reset": "acme-db"}]


def test_info_is_aislands_then_the_classs_own_lines(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    result = _run("acme-db", "info")
    assert result.exit_code == 0, result.output
    assert supervised == [{"info": "acme-db"}]
    assert result.output == "  Tables        : 3 (ports [7000])\n"


def test_restart_stops_then_starts_with_the_port(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    assert _run("acme-db", "restart", "-b", "--port", "7200").exit_code == 0
    assert [list(call)[0] for call in supervised] == ["stop", "start"]
    assert supervised[1]["cmd"] == ["acme-db", "--port", "7200"]


# --- in `aisland services` ----------------------------------------------------------------------------------------


def test_it_is_started_when_named_never_by_default(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    assert _run("services", "start", "-b").exit_code == 0
    assert supervised == []
    assert _run("services", "start", "acme-db", "-b").exit_code == 0
    assert supervised[0]["cmd"] == ["acme-db", "--port", "7000"]


def test_it_is_listed(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    assert _run("services").exit_code == 0
    assert supervised == [{"status": "acme-db"}]


# --- not installed, not offered -----------------------------------------------------------------------------------


def test_a_project_not_installed_offers_no_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ws"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / "pyproject.toml").write_text(textwrap.dedent(PROJECT))
    monkeypatch.setattr(cli, "get_workspace_root", lambda: root)
    monkeypatch.setattr(registry, "ENTRIES", ())
    assert "No such command 'acme-db'" in _run("acme-db", "start").output


def test_a_class_without_start_is_refused_naming_its_entry_point(workspace: Path) -> None:
    (Path(sys.path[0]) / "acme_plugin.py").write_text(MODULE.replace("def start(self, ctx):", "def begin(self, ctx):"))
    result = _run("services")
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {workspace / 'acme' / 'pyproject.toml'}:6: plugin 'acme-db' (acme_plugin:Database) is a service "
        "but has no start(ctx)\n"
    )


def test_aislands_own_fiftyone_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FiftyOne is curaitor's service now: without curaitor in the workspace, there is none."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(cli, "get_workspace_root", lambda: root)
    assert "fiftyone" not in [entry.name for entry in registry.ENTRIES]
    assert "No such command 'fiftyone'" in _run("fiftyone", "start").output


# --- what a moved service needs beyond FiftyOne: options, the choice command, .env, its own lifecycle --------------

#: A class with an option of its own and a lifecycle that is not the supervisor's.
VIEWER = """\
SEEN = []


class Database:
    kind = "service"
    options = {"repo": "The repository to serve."}
    supervised = False

    def start(self, ctx):
        SEEN.append(dict(ctx.options))
        SEEN.append(ctx.choose_command("tracker", "aim"))
        SEEN.append((ctx.workspace_name, ctx.read_env()))

    def info(self, ctx):
        ctx.echo("  Lifecycle     : its own")
"""


def _module(text: str) -> None:
    (Path(sys.path[0]) / "acme_plugin.py").write_text(text)


def test_a_class_option_is_offered_on_start_and_restart(workspace: Path) -> None:
    _module(VIEWER)
    for command in ("start", "restart"):
        output = _run("acme-db", command, "--help").output
        assert "--repo TEXT" in output and "The repository to serve." in output


def test_an_option_given_reaches_the_class_and_one_not_given_is_none(
    workspace: Path, supervised: List[Dict[str, Any]]
) -> None:
    _module(VIEWER)
    assert _run("acme-db", "start", "-b", "--repo", "/runs/aim").exit_code == 0
    assert _run("acme-db", "start", "-b").exit_code == 0
    assert _run("services", "start", "acme-db").exit_code == 0
    assert [seen for seen in _seen() if isinstance(seen, dict)] == [
        {"repo": "/runs/aim"},
        {"repo": None},
        {"repo": None},
    ]


def test_the_class_gets_the_command_that_adds_its_choice_keeping_the_others(
    workspace: Path, supervised: List[Dict[str, Any]]
) -> None:
    from aisland import selection

    _module(VIEWER)
    selection.write_committed(workspace, "tracker", ["tensorboard"])
    assert _run("acme-db", "start").exit_code == 0
    assert _seen()[1] == "aisland workspace configure tracker set tensorboard aim"


def test_the_class_reads_the_workspace_env_and_its_name(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    _module(VIEWER)
    (workspace / ".env").write_text("ACME_USER=me\n")
    assert _run("acme-db", "start").exit_code == 0
    assert _seen()[2] == ("ws", {"ACME_USER": "me"})


def test_an_unsupervised_class_prints_only_its_own_info(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    """Its processes are not the supervisor's: aisland's supervisor lines would send an operator to the wrong tool."""
    _module(VIEWER)
    result = _run("acme-db", "info")
    assert result.exit_code == 0, result.output
    assert supervised == []
    assert result.output == "  Lifecycle     : its own\n"


@pytest.mark.parametrize(
    "options, message",
    [
        ('["repo"]', 'options must map each option\'s name to its help, as options = {"repo": "…"}'),
        ('{"port": "x"}', "option 'port' is aisland's own"),
        ('{"background": "x"}', "option 'background' is aisland's own"),
    ],
)
def test_an_option_aisland_cannot_offer_is_refused_naming_the_entry_point(
    workspace: Path, options: str, message: str
) -> None:
    _module(VIEWER.replace('options = {"repo": "The repository to serve."}', f"options = {options}"))
    result = _run("services")
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {workspace / 'acme' / 'pyproject.toml'}:6: plugin 'acme-db' (acme_plugin:Database): {message}\n"
    )


def test_reset_asks_the_classs_own_question(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    """A class whose reset wipes no data says what it removes; aisland asks that, not "wipe its data"."""
    _module(
        VIEWER.replace(
            "    supervised = False\n",
            '    supervised = False\n    reset_question = "Remove the viewer\'s image? (recordings are kept)"\n'
            "\n    def reset(self, ctx):\n        SEEN.append('reset')\n",
        )
    )
    result = _run("acme-db", "reset", input="y\n")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("Remove the viewer's image? (recordings are kept) [y/N]: y\n")
    assert _seen() == ["reset"]


def test_without_a_question_of_its_own_reset_asks_aislands(workspace: Path, supervised: List[Dict[str, Any]]) -> None:
    result = _run("acme-db", "reset", input="n\n")
    assert result.exit_code == 1
    assert result.output.startswith("Stop acme-db and wipe its data? [y/N]: n\n")
    assert supervised == []


def test_reset_help_is_the_classs_own_question(workspace: Path) -> None:
    _module(
        VIEWER.replace(
            "    supervised = False\n",
            '    supervised = False\n    reset_question = "Remove the image? (recordings are kept)"\n',
        )
    )
    assert "Remove the image? (recordings are kept)" in _run("acme-db", "reset", "--help").output
    assert "Stop it and wipe its data." not in _run("acme-db", "reset", "--help").output


def test_without_a_question_reset_help_says_it_wipes_the_data(workspace: Path) -> None:
    assert "Stop it and wipe its data." in _run("acme-db", "reset", "--help").output
