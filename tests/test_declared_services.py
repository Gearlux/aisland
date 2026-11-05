"""A service a project declares in its own `pyproject.toml`, run by aisland's supervisor (`services/declared.py`).

    [tool.aisland.service.notes]
    command = ["notes-server", "--port", "8123"]   # what the supervisor runs
    port = 8123                                    # checked free before it starts …
    ready = "/health"                              # … and asked until it answers
    needs = { tracker = "aim" }                    # offered only while this choice is made

It joins `aisland services` (started when named, never by default) and gets its own `aisland <name>
start|stop|restart|status|info|reset`, built from the declaration — the project writes no aisland code.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner, Result

from aisland import cli, locations
from aisland.services import base, registry
from aisland.workspace import selection

NOTES = """\
    [project]
    name = "notes"

    [tool.aisland.service.notes]
    command = ["notes-server", "--port", "8123"]
    port = 8123
    ready = "/health"
"""


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pyproject: str = NOTES) -> Path:
    root = tmp_path / "ws"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "pyproject.toml").write_text(textwrap.dedent(pyproject))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    monkeypatch.setattr(registry, "ENTRIES", ())  # aisland's own services stay out of these tests
    return root


@pytest.fixture
def supervised(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """What the supervisor was asked to run, stop and report, recorded instead of done."""
    calls: List[Dict[str, Any]] = []

    def start(
        self: base.BaseService,
        cmd: List[str],
        env: Optional[dict] = None,
        background: bool = False,
        readiness: Optional[Dict[str, Any]] = None,
    ) -> None:
        calls.append({"start": self.name, "cmd": cmd, "background": background, "readiness": readiness})

    monkeypatch.setattr(base.BaseService, "start", start)
    monkeypatch.setattr(base.BaseService, "stop", lambda self: calls.append({"stop": self.name}))
    monkeypatch.setattr(base.BaseService, "status", lambda self: calls.append({"status": self.name}))
    monkeypatch.setattr(base.BaseService, "is_running", lambda self: False)
    monkeypatch.setattr("aisland.supervisor.print_status", lambda: None)
    return calls


def _run(*args: str, input: str = "") -> Result:
    return CliRunner().invoke(cli.main, list(args), input=input)


# --- its own commands ---------------------------------------------------------------------------------------------


def test_a_declared_service_gets_its_own_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path, monkeypatch)
    result = _run("notes", "--help")
    assert result.exit_code == 0, result.output
    for command in ("start", "stop", "restart", "status", "info", "reset"):
        assert f"  {command} " in result.output


def test_its_help_names_the_project_that_declares_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path, monkeypatch, NOTES.replace("service.notes", "service.journal"))
    assert "Manage journal, a service notes brings." in _run("journal", "--help").output


def test_start_runs_the_declared_command_under_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]
) -> None:
    _workspace(tmp_path, monkeypatch)
    assert _run("notes", "start", "-b").exit_code == 0
    assert supervised == [
        {
            "start": "notes",
            "cmd": ["notes-server", "--port", "8123"],
            "background": True,
            "readiness": {"port": 8123, "path": "/health"},
        }
    ]


def test_without_a_port_and_a_ready_path_nothing_is_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]
) -> None:
    _workspace(tmp_path, monkeypatch, NOTES.replace('    ready = "/health"\n', ""))
    assert _run("notes", "start", "-b").exit_code == 0
    assert supervised[0]["readiness"] is None


def test_stop_and_status_reach_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]
) -> None:
    _workspace(tmp_path, monkeypatch)
    assert _run("notes", "stop").exit_code == 0
    assert _run("notes", "status").exit_code == 0
    assert supervised == [{"stop": "notes"}, {"status": "notes"}]


def test_info_says_what_runs_where_it_is_declared_and_its_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]
) -> None:
    root = _workspace(tmp_path, monkeypatch)
    lines = _run("notes", "info").output.splitlines()
    assert "  Command       : notes-server --port 8123" in lines
    assert f"  Declared in   : {root / 'notes' / 'pyproject.toml'}:4" in lines
    assert "  URL           : http://localhost:8123" in lines


def test_reset_asks_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]) -> None:
    _workspace(tmp_path, monkeypatch)
    assert _run("notes", "reset", input="n\n").exit_code == 1
    assert _run("notes", "reset", input="y\n").exit_code == 0


# --- in `aisland services` ----------------------------------------------------------------------------------------


def test_it_is_listed_and_started_when_named_never_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]
) -> None:
    _workspace(tmp_path, monkeypatch)
    assert _run("services").exit_code == 0
    assert supervised == [{"status": "notes"}]
    supervised.clear()
    assert _run("services", "start", "-b").exit_code == 0
    assert supervised == []
    assert _run("services", "start", "notes", "-b").exit_code == 0
    assert [call.get("start") for call in supervised] == ["notes"]


# --- offered only while what it needs is chosen -------------------------------------------------------------------


def test_a_service_that_needs_a_choice_is_offered_only_once_it_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]
) -> None:
    root = _workspace(tmp_path, monkeypatch, NOTES + '    needs = { tracker = "aim" }\n')
    assert "No such command 'notes'" in _run("notes", "start").output
    assert "Error: no service 'notes'" in _run("services", "start", "notes").output
    selection.write_committed(root, "tracker", ["mlflow", "aim"])
    assert _run("notes", "start", "-b").exit_code == 0
    assert [call.get("start") for call in supervised] == ["notes"]


# --- a name that is taken -----------------------------------------------------------------------------------------


def test_the_name_of_one_of_aislands_own_services_is_refused_naming_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path, monkeypatch, NOTES.replace("service.notes", "service.mlflow"))
    monkeypatch.setattr(registry, "ENTRIES", (registry.Entry("mlflow", lambda: None, True, True),))  # type: ignore
    result = _run("services")
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {root / 'notes' / 'pyproject.toml'}:4: service 'mlflow' is one of aisland's own — "
        "give it another name\n"
    )


def test_a_service_named_like_an_aisland_command_is_reached_through_aisland_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supervised: List[Dict[str, Any]]
) -> None:
    """A command a newer aisland adds may take a service's name: the service then loses only its shortcut
    `aisland <name>` — the command wins — and the workspace keeps working."""
    _workspace(tmp_path, monkeypatch, NOTES.replace("service.notes", "service.setup"))
    assert "Usage: main setup" in _run("setup", "--help").output  # the command itself
    assert _run("services", "start", "setup", "-b").exit_code == 0
    assert [call.get("start") for call in supervised] == ["setup"]
    assert _run("--help").output.count("  setup ") == 1
    shown = _run("workspace", "show")
    assert shown.exit_code == 0
    assert "Warning: service 'setup' shares its name with the command `aisland setup`" in shown.stderr
