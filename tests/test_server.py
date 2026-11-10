"""aisland's MCP server (`aisland-mcp`, the `[mcp]` extra): the agreed tools and nothing else.

Each tool answers with one JSON object — the data the matching command prints with `--json`, from the same
function. The tools that change this machine (the editor's theme and colours, starting and stopping Jenkins and
MLflow) are marked so, and nothing that prompts, runs for minutes or deletes is a tool. Over real stdio a stray print
lands on stderr, never in the protocol.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import pytest
from click.testing import CliRunner
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.mcpserver.exceptions import ToolError

from aisland import cli, console, doctor, editor, failures, locations, server
from aisland.ci import stats_report
from aisland.errors import AislandError
from aisland.services import docker, registry, supervisor, swiftbar
from aisland.services.base import ServiceState, service_state
from aisland.setup import editor_files, mcp_config
from aisland.tunnel import store as tunnel_store
from aisland.workspace import projects
from aisland.workspace import sources as sources_mod
from tests.test_editor import no_vscode, vscode  # noqa: F401  (fixtures)

READ_ONLY = {
    "doctor",
    "tests_report",
    "editor_themes",
    "editor_colors",
    "sources",
    "services_status",
    "tunnel_list",
    "tunnel_status",
    "track_status",
}
CHANGING = {
    "editor_set_theme",
    "editor_set_color",
    "editor_unset",
    "service_start",
    "tunnel_start",
}
STOPPING = {"service_stop", "service_restart", "tunnel_stop"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace `acme/` that is a git repository, the server pointed at it."""
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(editor_files, "_loggair_log_dir", lambda workspace_root: None)
    monkeypatch.setattr(server, "get_workspace_root", lambda: root)
    return root


@pytest.fixture
def tracked(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`repo` with the projects `models` (its own `track = false`, hidden from the root) and `tools` (the default),
    as `.gitmodules` lists them and `workspace.toml` describes them; the command pointed at it too."""
    (repo / ".gitmodules").write_text(
        '[submodule "models"]\n\tpath = models\n\turl = https://example.invalid/models.git\n\tignore = all\n'
        '[submodule "tools"]\n\tpath = tools\n\turl = https://example.invalid/tools.git\n'
    )
    (repo / "workspace.toml").write_text(
        '[project."models"]\nurl = "https://example.invalid/models.git"\ntrack = false\n\n'
        '[project."tools"]\nurl = "https://example.invalid/tools.git"\n'
    )
    monkeypatch.setattr(locations, "workspace_root", lambda: repo)
    return repo


def _server() -> "server.MCPServer":
    """The server, which the test venv's ``[mcp]`` extra builds."""
    assert server.mcp_server is not None
    return server.mcp_server


def _tools() -> Dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(_server().list_tools())}


def _result(name: str, args: Optional[Dict[str, Any]] = None) -> Any:
    async def go() -> Any:
        async with Client(_server()) as connection:
            return await connection.call_tool(name, args or {})

    return asyncio.run(go())


def _call(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = _result(name, args)
    assert not result.is_error, result.content
    payload: Dict[str, Any] = json.loads(result.content[0].text)
    return payload


def _error(name: str, args: Optional[Dict[str, Any]] = None) -> str:
    result = _result(name, args)
    assert result.is_error, result.content
    text: str = result.content[0].text
    return text


def _local_config(root: Path) -> str:
    return subprocess.run(
        ["git", "config", "--local", "--get-regexp", r"^aisland\.editor\."], cwd=root, capture_output=True, text=True
    ).stdout


# --- the tool list ------------------------------------------------------------------------


def test_the_tools_are_the_agreed_set() -> None:
    assert set(_tools()) == READ_ONLY | CHANGING | STOPPING


def test_a_reading_tool_says_it_only_reads_and_a_stopping_one_that_it_ends_something() -> None:
    """Stopping the container engine stops every container in it; a client asks before a destructive tool."""
    tools = _tools()
    for name in READ_ONLY:
        assert tools[name].annotations.read_only_hint is True, name
    for name in CHANGING | STOPPING:
        assert tools[name].annotations.read_only_hint is False, name
        assert tools[name].annotations.destructive_hint is (name in STOPPING), name


def test_every_tool_says_what_it_does() -> None:
    assert all(tool.description and len(tool.description) > 20 for tool in _tools().values())


def test_the_server_advertises_tools_only() -> None:
    """No `resources/list` bait: an empty answer reads to some clients as a dead server."""
    capabilities = _server()._lowlevel_server.get_capabilities(NotificationOptions(), experimental_capabilities={})
    assert capabilities.tools is not None
    assert capabilities.resources is None and capabilities.prompts is None


def test_an_sdk_that_moved_its_request_handlers_is_refused_saying_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server drops the resources and prompts handlers through the SDK's private table: a release that renames
    it is refused naming the table and the releases aisland runs on, never an AttributeError."""

    class Renamed:
        def __init__(self, name: str, instructions: str) -> None:
            self._low_level = object()

    monkeypatch.setattr(server, "MCPServer", Renamed)
    with pytest.raises(AislandError, match=r"no `MCPServer._lowlevel_server._request_handlers` table"):
        server._build_server()


# --- the read-only tools ------------------------------------------------------------------


def test_doctor_returns_every_checks_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "CHECKS",
        [
            lambda: doctor.CheckResult("opencv", True, "headless only"),
            lambda: doctor.CheckResult("mcp", False, "1.9 installed", "uv pip install 'mcp>=2.0'"),
        ],
    )
    assert _call("doctor") == {
        "ok": False,
        "python": sys.executable,
        "checks": [
            {"name": "opencv", "ok": True, "detail": "headless only", "remedy": ""},
            {"name": "mcp", "ok": False, "detail": "1.9 installed", "remedy": "uv pip install 'mcp>=2.0'"},
        ],
    }


class _Report:
    def to_dict(self) -> Dict[str, Any]:
        return {"project": "loggair", "fast_run_seconds": 12.5}


def test_tests_report_is_what_the_command_prints_with_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "loggair").mkdir()
    (tmp_path / "loggair" / "pyproject.toml").write_text("[project]\nname = 'loggair'\n")
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    asked: List[Any] = []

    def build_report(project: str, days: int, project_dir: Optional[Path] = None) -> _Report:
        asked.append((project, days))
        return _Report()

    monkeypatch.setattr(stats_report, "build_report", build_report)
    assert _call("tests_report", {"project": "loggair", "days": 90}) == {"project": "loggair", "fast_run_seconds": 12.5}
    printed = CliRunner().invoke(cli.main, ["tests", "report", "loggair", "--days", "90", "--json"])
    assert json.loads(printed.output) == _call("tests_report", {"project": "loggair", "days": 90})
    assert asked[0] == ("loggair", 90)


def test_tests_report_refuses_a_project_the_workspace_does_not_have(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    assert f"no project 'nothere' in the workspace {tmp_path}" in _error("tests_report", {"project": "nothere"})


def test_editor_themes_marks_the_workspaces(vscode: Dict[str, Path], repo: Path) -> None:  # noqa: F811
    _call("editor_set_theme", {"name": "Solarized Dark"})
    themes = _call("editor_themes")["themes"]
    assert len(themes) == 6
    assert {"name": "Solarized Dark", "kind": "dark", "source": "built-in", "current": True} in themes
    assert sum(theme["current"] for theme in themes) == 1


def test_editor_themes_is_empty_without_vs_code(no_vscode: None, repo: Path) -> None:  # noqa: F811
    assert _call("editor_themes") == {"themes": []}


def test_editor_colors_gives_the_choice_and_the_names_holding_a_part(repo: Path) -> None:
    _call("editor_set_color", {"role": "background", "color": "navy"})
    assert _call("editor_colors", {"part": "slate blue"}) == {
        "choice": {"theme": None, "background": "navy", "foreground": None, "area": "frame"},
        "colors": [
            {"name": "darkslateblue", "value": "#483d8b"},
            {"name": "mediumslateblue", "value": "#7b68ee"},
            {"name": "slateblue", "value": "#6a5acd"},
        ],
    }
    assert len(_call("editor_colors")["colors"]) == 148


def test_sources_are_the_rows_source_list_shows(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"project": "loggair", "committed": "pypi", "override": "-", "effective": "pypi", "state": "empty"}]
    monkeypatch.setattr(sources_mod, "describe", lambda workspace_root=None: rows)
    assert _call("sources") == {"sources": rows}


def test_tunnel_list_is_what_the_command_prints_with_json() -> None:
    tunnel_store.remember("sandbox-4821", url="https://example.invalid/sandbox-4821/?folder=/mnt/w", folder="/mnt/w")
    printed = CliRunner().invoke(cli.main, ["tunnel", "list", "--json"])
    assert _call("tunnel_list") == {"workspaces": json.loads(printed.output)}
    assert _call("tunnel_list")["workspaces"][0]["name"] == "sandbox-4821"


class _Notes:
    """A project's service whose state is known: running as PID 7 on port 5005."""

    name = "notes"

    def state(self) -> ServiceState:
        return service_state("notes", "running", pid=7, ports=[5005])


@pytest.fixture
def every_service(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """The workspace's services: aisland's own — Jenkins running under the supervisor, the container engine not
    installed, the menu stopped — and `notes`, one a project brings."""
    notes = registry.Entry("notes", cast(Any, _Notes), by_default=False, takes_background=True, project="app")
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [notes])
    monkeypatch.setattr(
        supervisor, "list_processes", lambda: {"jenkins": {"name": "jenkins", "is_running": True, "pid": 4242}}
    )
    monkeypatch.setattr(supervisor, "get_ports", lambda name: [9080] if name == "jenkins" else [])
    for service in (docker.DockerService, docker.LinuxDockerService):
        monkeypatch.setattr(service, "state", lambda self: service_state("docker", "not installed"))
    monkeypatch.setattr(swiftbar.SwiftBarService, "state", lambda self: service_state("swiftbar", "stopped"))
    monkeypatch.setattr(locations, "workspace_root", lambda: repo)  # the command's root too


@pytest.mark.usefixtures("every_service")
def test_services_status_answers_for_every_service_not_only_the_supervisors() -> None:
    """The container engine and a project's service are not the supervisor's processes; the tool still answers for
    them, as `aisland services status` does."""
    assert _call("services_status") == {
        "services": [
            {"service": "docker", "state": "not installed", "pid": None, "ports": [], "ready": None},
            {"service": "jenkins", "state": "running", "pid": 4242, "ports": [9080], "ready": None},
            {"service": "swiftbar", "state": "stopped", "pid": None, "ports": [], "ready": None},
            {"service": "notes", "state": "running", "pid": 7, "ports": [5005], "ready": None},
        ]
    }


@pytest.mark.usefixtures("every_service")
def test_services_status_is_what_the_command_prints_with_json() -> None:
    printed = CliRunner().invoke(cli.main, ["services", "status", "--json"])
    assert printed.exit_code == 0, printed.output
    assert _call("services_status") == json.loads(printed.output)


def test_track_status_is_what_the_command_prints_with_json(tracked: Path) -> None:
    printed = CliRunner().invoke(cli.main, ["workspace", "track", "status", "--json"])
    assert printed.exit_code == 0, printed.output
    answer = _call("track_status")
    assert answer == json.loads(printed.output)
    assert answer["default_tracked"] is True
    assert [(row["project"], row["tracked"], row["source"], row["git"]) for row in answer["projects"]] == [
        ("models", False, "project", "applied"),
        ("tools", True, "default", "applied"),
    ]


def test_track_status_of_named_projects_answers_those_alone(tracked: Path) -> None:
    assert [row["project"] for row in _call("track_status", {"projects": ["tools"]})["projects"]] == ["tools"]
    assert [row["project"] for row in _call("track_status", {"projects": ["tools", "models"]})["projects"]] == [
        "tools",
        "models",
    ]


def test_track_status_of_an_unknown_project_is_refused_in_the_commands_words(tracked: Path) -> None:
    assert "'nothing' is no submodule of the workspace (the submodules: models, tools)" in _error(
        "track_status", {"projects": ["nothing"]}
    )


def test_track_status_without_submodules_lists_no_project(repo: Path) -> None:
    assert _call("track_status") == {"default_tracked": True, "projects": []}


def test_the_reading_tools_change_nothing(vscode: Dict[str, Path], repo: Path) -> None:  # noqa: F811
    _call("editor_set_theme", {"name": "Solarized Dark"})
    config, workspace_file = _local_config(repo), editor_files.code_workspace_path(repo).read_text()
    for name in ("editor_themes", "editor_colors"):
        _call(name)
    assert _local_config(repo) == config
    assert editor_files.code_workspace_path(repo).read_text() == workspace_file


# --- the editor's theme and colours -------------------------------------------------------


def test_editor_set_theme_writes_the_workspace_file_and_returns_the_choice(
    vscode: Dict[str, Path], repo: Path  # noqa: F811
) -> None:
    assert _call("editor_set_theme", {"name": "solarized dark"}) == {
        "theme": "Solarized Dark",
        "background": None,
        "foreground": None,
        "area": "frame",
    }
    assert '"workbench.colorTheme": "Solarized Dark"' in editor_files.code_workspace_path(repo).read_text()


def test_without_vs_code_editor_set_theme_keeps_the_name_and_says_it_is_unchecked(
    no_vscode: None, repo: Path  # noqa: F811
) -> None:
    assert _call("editor_set_theme", {"name": "Monokai Pro"}) == {
        "theme": "Monokai Pro",
        "background": None,
        "foreground": None,
        "area": "frame",
        "note": "not checked: VS Code is not on this machine, so the theme comes from the VS Code that opens this "
        "workspace remotely",
    }
    assert '"workbench.colorTheme": "Monokai Pro"' in editor_files.code_workspace_path(repo).read_text()


def test_an_unknown_theme_is_refused_with_the_commands_words(vscode: Dict[str, Path], repo: Path) -> None:  # noqa: F811
    assert "no installed VS Code theme is named 'Solarzed Dark' (did you mean 'Solarized Dark'?)" in _error(
        "editor_set_theme", {"name": "Solarzed Dark"}
    )
    assert _local_config(repo) == ""


def test_editor_set_color_takes_a_name_or_hex_and_an_area(repo: Path) -> None:
    assert _call("editor_set_color", {"role": "background", "color": "Dark Slate Blue", "area": "both"}) == {
        "theme": None,
        "background": "darkslateblue",
        "foreground": None,
        "area": "both",
    }
    choice = _call("editor_set_color", {"role": "foreground", "color": "#E0E0E0"})
    assert (choice["foreground"], choice["area"]) == ("#e0e0e0", "both")
    assert '"editor.background": "#483d8b"' in editor_files.code_workspace_path(repo).read_text()


def test_editor_set_color_takes_the_sidebar_area(repo: Path) -> None:
    assert _call("editor_set_color", {"role": "background", "color": "navy", "area": "sidebar"})["area"] == "sidebar"
    assert '"sideBarSectionHeader.background": "#000080"' in editor_files.code_workspace_path(repo).read_text()
    assert '"editor.background"' not in editor_files.code_workspace_path(repo).read_text()


def test_a_colour_that_is_no_colour_is_refused(repo: Path) -> None:
    assert "'bluish' is not a colour" in _error("editor_set_color", {"role": "background", "color": "bluish"})


@pytest.mark.parametrize(
    ("what", "left"),
    [
        ("theme", {"theme": None, "background": "navy", "foreground": "white", "area": "both"}),
        ("background", {"theme": "Solarized Dark", "background": None, "foreground": "white", "area": "both"}),
        ("foreground", {"theme": "Solarized Dark", "background": "navy", "foreground": None, "area": "both"}),
        ("colors", {"theme": "Solarized Dark", "background": None, "foreground": None, "area": "frame"}),
    ],
)
def test_editor_unset_forgets_what_it_names(
    vscode: Dict[str, Path], repo: Path, what: str, left: Dict[str, Any]  # noqa: F811
) -> None:
    _call("editor_set_theme", {"name": "Solarized Dark"})
    _call("editor_set_color", {"role": "background", "color": "navy", "area": "both"})
    _call("editor_set_color", {"role": "foreground", "color": "white"})
    assert _call("editor_unset", {"what": what}) == left


# --- the services --------------------------------------------------------------------------------------------

#: The time limit each command the `ran` fixture records was given.
limits: List[float] = []


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch, repo: Path) -> List[List[str]]:
    """Every `aisland` command line the service tools run, each answering exit 0 with one line; the workspace's
    services are aisland's own and `notes`, one a project brings."""
    calls: List[List[str]] = []
    limits.clear()

    def run(args: List[str], timeout: float = server.SERVICE_TIMEOUT) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        limits.append(timeout)
        return subprocess.CompletedProcess(args, 0, stdout=f"{args[-1].capitalize()} started.\n", stderr="")

    notes = registry.Entry("notes", cast(Any, _Notes), by_default=False, takes_background=True, project="app")
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [notes])
    monkeypatch.setattr(server, "_run_aisland", run)
    for service in (docker.DockerService, docker.LinuxDockerService):
        monkeypatch.setattr(service, "state", lambda self: service_state("docker", "running"))
    monkeypatch.setattr(swiftbar.SwiftBarService, "state", lambda self: service_state("swiftbar", "running"))
    return calls


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("service_start", ["services", "start", "--background", "notes"]),
        ("service_stop", ["services", "stop", "notes"]),
        ("service_restart", ["services", "restart", "--background", "notes"]),
    ],
)
def test_a_service_tool_runs_the_aisland_command_in_the_background(
    ran: List[List[str]], tool: str, args: List[str]
) -> None:
    assert _call(tool, {"name": "notes"}) == {
        "service": "notes",
        "state": "running",
        "pid": 7,
        "ports": [5005],
        "ready": None,
        "output": "Notes started.",
    }
    assert ran == [args]


@pytest.mark.parametrize(
    ("tool", "args"),
    [("service_start", ["services", "start", "docker"]), ("service_restart", ["services", "restart", "swiftbar"])],
)
def test_a_service_that_detaches_by_itself_gets_no_background_flag(
    ran: List[List[str]], tool: str, args: List[str]
) -> None:
    """`aisland docker start` takes no `--background`: Colima detaches by itself."""
    _call(tool, {"name": args[-1]})
    assert ran == [args]


@pytest.mark.parametrize(("tool", "factor"), [("service_start", 1), ("service_restart", 2)])
def test_a_service_tool_waits_as_long_as_the_service_may_take_to_start(
    ran: List[List[str]], tool: str, factor: int
) -> None:
    """A first Colima start boots its VM, up to `COLIMA_START_SECONDS`: a tool that gave up sooner left it starting,
    unseen."""
    _call(tool, {"name": "docker"})
    assert limits == [factor * max(server.SERVICE_TIMEOUT, docker.COLIMA_START_SECONDS)]


def test_a_command_past_its_limit_is_stopped_with_everything_it_started(tmp_path: Path) -> None:
    """The time limit ends the whole process group — the command and what it started — never only the command."""
    pidfile = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        server._run_as_a_group(["/bin/sh", "-c", script], tmp_path, timeout=1)
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child)


def test_a_descendant_in_a_session_of_its_own_holding_the_pipes_never_holds_up_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descendant that left the group keeps the output pipes open and outlives the signals: the limit still ends in
    the refusal at once — never an error from signalling a group that holds only the exited command, never a wait
    on pipes that stay open."""
    monkeypatch.setattr(server, "STOP_GRACE_SECONDS", 0.5)
    pidfile = tmp_path / "away.pid"
    script = (
        "import subprocess, sys, time\n"
        "away = subprocess.Popen(['sleep', '15'], start_new_session=True)\n"
        f"open({str(pidfile)!r}, 'w').write(str(away.pid))\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            server._run_as_a_group([sys.executable, "-c", script], tmp_path, timeout=1)
        assert time.monotonic() - started < 8
    finally:
        if pidfile.exists():
            os.kill(int(pidfile.read_text()), 9)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return (
        subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1] != "Z"
    )


def test_a_service_named_like_an_aisland_command_is_still_the_service(
    ran: List[List[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project's service called `clean` has no `aisland clean` shortcut — that is aisland's own command — so the
    tool runs it through `aisland services`, never `aisland clean stop`."""
    clean = registry.Entry("clean", cast(Any, _Notes), by_default=False, takes_background=True, project="app")
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [clean])
    _call("service_stop", {"name": "clean"})
    assert ran == [["services", "stop", "clean"]]


def test_every_service_of_the_workspace_is_one_a_tool_controls_and_no_other(ran: List[List[str]]) -> None:
    text = _error("service_start", {"name": "clearml"})
    assert "no service 'clearml' (the services: docker, jenkins, notes, swiftbar)" in text
    assert ran == []


def test_the_schema_names_no_fixed_list_of_services() -> None:
    """The workspace's services change with its projects: the schema takes a name, the tool checks it when it runs."""
    for name in ("service_start", "service_stop", "service_restart"):
        assert _tools()[name].input_schema["properties"]["name"]["type"] == "string"
        assert "enum" not in _tools()[name].input_schema["properties"]["name"]
    assert "mlflow" not in server.INSTRUCTIONS.lower()


def test_a_command_that_fails_is_an_error_carrying_its_output(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(
        server,
        "_run_aisland",
        lambda args, timeout=0: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="Error: port 9080 is in use by PID 12\n"
        ),
    )
    assert "Error: port 9080 is in use by PID 12" in _error("service_start", {"name": "jenkins"})


def test_the_aisland_command_never_reads_the_servers_stdin(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's stdin is the client's JSON-RPC stream: a command that asks (`click.confirm`) or a git credential
    prompt must find it empty, never eat the protocol's bytes."""
    given: Dict[str, Any] = {}
    real_popen = subprocess.Popen

    def popen(args: List[str], **kwargs: Any) -> "subprocess.Popen[str]":
        given.update(kwargs)
        return real_popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(server.subprocess, "Popen", popen)
    server._run_aisland(["jenkins", "status"])
    assert given["stdin"] is subprocess.DEVNULL


def test_the_aisland_command_runs_with_this_interpreter_at_the_workspace_root(repo: Path) -> None:
    """At the root `-P` keeps the `aisland/` checkout from shadowing the package; from a wheel the root is where
    aisland finds its workspace."""
    result = server._run_aisland(["workspace", "--help"])
    assert result.returncode == 0, result.stderr
    assert "Usage: " in result.stdout and "workspace" in result.stdout


@pytest.mark.parametrize(
    "refusal",
    [server.ToolRefusal("no project 'x' in the workspace /w"), editor.EditorChoiceError("'bluish' is not a colour")],
)
def test_an_anticipated_refusal_reaches_the_client_as_a_tool_error_in_its_own_words(refusal: Exception) -> None:
    """From mcp 2.1 on, only a `ToolError`'s words reach the client; any other exception reads `Error executing
    tool <name>` there and its text stays on the server."""

    def refuses() -> None:
        raise refusal

    with pytest.raises(ToolError, match=re.escape(str(refusal))):
        server._refusals_reach_the_client(refuses)()


def test_a_warning_the_tools_code_gives_comes_back_in_its_object(capsys: pytest.CaptureFixture[str]) -> None:
    """The tool's progress lines are dropped and its warnings kept: the server's stdout is the protocol."""

    def warns() -> Dict[str, object]:
        console.echo("==> Reading the editor's settings")
        failures.warn("  - Warning: ignoring aisland.editor.color: not a colour")
        return {"theme": "Default Dark+"}

    assert server._refusals_reach_the_client(warns)() == {
        "theme": "Default Dark+",
        "warnings": ["  - Warning: ignoring aisland.editor.color: not a colour"],
    }
    assert capsys.readouterr() == ("", "")


def test_a_tool_that_says_nothing_on_stderr_has_no_warnings_key() -> None:
    def quiet() -> Dict[str, object]:
        console.echo("progress")
        return {"ok": True}

    assert server._refusals_reach_the_client(quiet)() == {"ok": True}


def test_a_question_inside_a_tool_is_refused_in_its_own_words() -> None:
    def asks() -> Dict[str, object]:
        console.confirm("Delete every backup?")
        return {}

    with pytest.raises(ToolError, match="no one is here to answer: Delete every backup?"):
        server._refusals_reach_the_client(asks)()


def test_a_crash_is_left_to_the_sdk() -> None:
    def crashes() -> None:
        raise KeyError("pid")

    with pytest.raises(KeyError):
        server._refusals_reach_the_client(crashes)()


# --- the process --------------------------------------------------------------------------


def test_without_the_mcp_extra_the_command_says_what_to_install() -> None:
    blocked = "import sys; sys.modules['mcp'] = None; from aisland.server import main; main()"
    result = subprocess.run([sys.executable, "-P", "-c", blocked], capture_output=True, text=True, cwd="/")
    assert result.returncode == 1
    assert result.stderr == "Error: aisland-mcp needs the mcp extra: pip install 'aisland[mcp]'\n"


def test_over_stdio_a_print_goes_to_stderr_and_the_tools_still_answer(tmp_path: Path) -> None:
    """The real process, as a client starts it: a tool whose code prints does not corrupt the protocol."""
    script = (
        "import click; from aisland.tunnel import store as tunnel_store; "
        "tunnel_store.load_store = lambda: (click.echo('noise on stdout'), [])[1]; "
        "from aisland.server import main; main()"
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-P", "-c", script], cwd=str(tmp_path), env={"AISLAND_TUNNEL_HOME": str(tmp_path)}
    )

    async def go() -> Any:
        with (tmp_path / "stderr.log").open("w") as errlog:
            async with Client(stdio_client(params, errlog=errlog)) as connection:
                result = await connection.call_tool("tunnel_list", {})
                listed = await connection.list_tools()
        return result, listed

    result, listed = asyncio.run(asyncio.wait_for(go(), timeout=60))
    assert not result.is_error
    assert json.loads(result.content[0].text) == {"workspaces": []}
    assert {tool.name for tool in listed.tools} == READ_ONLY | CHANGING | STOPPING
    assert "noise on stdout" in (tmp_path / "stderr.log").read_text()


@pytest.mark.real_workspace
def test_the_workspace_lists_the_server_in_mcp_json(tmp_path: Path) -> None:
    """`aisland setup` writes `.mcp.json` from discovery: a `<pkg>.server:main` script importing `mcp.server`."""
    root = projects.get_workspace_root()
    if not (root / "aisland" / "pyproject.toml").is_file():
        pytest.skip("not run inside the workspace")
    servers = mcp_config.discover_mcp_servers(root, tmp_path / "bin")
    assert servers["aisland"] == {"command": str(tmp_path / "bin" / "aisland-mcp")}


def test_editor_choice_is_reported_as_json_shaped() -> None:
    assert server._choice(editor.EditorChoice(theme="Red")) == {
        "theme": "Red",
        "background": None,
        "foreground": None,
        "area": "frame",
    }
