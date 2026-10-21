"""aisland's MCP server, ``aisland-mcp``: the workspace's dev infrastructure as tools an AI client calls.

Installed with the ``[mcp]`` extra (``pip install 'aisland[mcp]'``) and listed in the workspace's ``.mcp.json`` by
``aisland setup`` (a ``<pkg>.server:main`` script whose module imports ``mcp.server``). Not navigaitor's: navigaitor
serves the ML libraries, aisland the workspace it runs in (user ruling 2026-10-21).

The tools are a chosen set, never the whole CLI (user choice 2026-10-21):

* reading — ``doctor``, ``tests_report``, ``editor_themes``, ``editor_colors``, ``sources``, ``services_status``,
  ``tunnel_list``: each the data its command prints with ``--json`` (or would), from the same function;
* changing this machine, never destroying — ``editor_set_theme``, ``editor_set_color``, ``editor_unset`` (the
  workspace window's theme and colours, :mod:`aisland.editor`) and ``service_start``/``stop``/``restart`` for
  Jenkins and MLflow, each run as the ``aisland`` command itself so it behaves exactly as typed.

What prompts (``setup``), runs for minutes or deletes (``clean``, ``backups prune``, ``* reset``, ``jenkins init``,
``source set``) stays a command. Every tool answers with one JSON object; a refusal is the tool's error, in the
command's own words.

A tool's code may print (``click.echo``): over stdio the SDK serves the protocol from a private copy of stdout and
points the process's stdout at stderr, so a print, or a subprocess's output, lands in the client's log, never in the
protocol.

NOTE: no ``from __future__ import annotations`` — the MCP server reads the live annotations to build each tool's
argument schema.
"""

import functools
import subprocess
import sys
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, TypeVar, get_args

import click

from aisland import doctor, editor, manager, pytest_stats
from aisland import sources as sources_mod
from aisland import supervisor
from aisland import tunnel as tunnel_mod
from aisland.manager import get_workspace_root

try:
    import mcp.types as mcp_types
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError:  # the [mcp] extra is not installed: main() says so
    MCPServer = None  # type: ignore[assignment,misc]

MISSING_EXTRA = "Error: aisland-mcp needs the mcp extra: pip install 'aisland[mcp]'"

#: The services a tool starts and stops (user choice 2026-10-21); the others stay commands.
Service = Literal["jenkins", "mlflow"]
SERVICES: Tuple[Service, ...] = get_args(Service)
#: What ``editor_unset`` forgets: the theme, one colour, or both colours and the area.
EditorPart = Literal["theme", "background", "foreground", "colors"]
#: How long a service command may take: Jenkins' start waits for its readiness probe.
SERVICE_TIMEOUT = 300

INSTRUCTIONS = """\
aisland manages this machine's development workspace: its environment checks, test statistics, project sources,
the supervised services (Jenkins, MLflow) and the VS Code window's theme and colours.

Read first: `doctor` (what is broken and the command that fixes it), `services_status`, `sources`,
`tests_report(project)`. The editor tools change only this machine's workspace window: `editor_themes` and
`editor_colors` list the choices, `editor_set_theme` / `editor_set_color` / `editor_unset` apply them.
`service_start` / `service_stop` / `service_restart` take `jenkins` or `mlflow`. Setup, cleaning and resets are
commands the person runs themselves (`aisland setup`, `aisland clean <project>`).
"""


_Result = TypeVar("_Result")


class ToolRefusal(Exception):
    """A refusal a tool anticipated (no such project, a command that failed): its words reach the client."""


def _refusals_reach_the_client(function: Callable[..., _Result]) -> Callable[..., _Result]:
    """Raise an anticipated refusal — a :class:`ToolRefusal`, or aisland's own ``Error:`` (a
    :class:`click.ClickException`) — as the SDK's ``ToolError``, the one exception whose words the client reads.

    From mcp 2.1 on, any other exception reaches the client as ``Error executing tool <name>`` alone, its text kept
    on the server: right for a crash, which is left to the SDK, wrong for "no installed VS Code theme is named …".
    """

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> _Result:
        try:
            return function(*args, **kwargs)
        except (ToolRefusal, click.ClickException) as refusal:
            raise ToolError(str(refusal)) from refusal

    return wrapper


def _choice(choice: editor.EditorChoice) -> Dict[str, Any]:
    """This machine's editor choice as the tools return it."""
    return asdict(choice)


def _run_aisland(args: List[str]) -> "subprocess.CompletedProcess[str]":
    """Run ``aisland ARGS`` with this interpreter, at the workspace root, its output captured.

    ``-P`` keeps the root's ``aisland/`` checkout off ``sys.path`` (it would shadow the package); the root is the
    working folder because aisland installed from a wheel finds its workspace from there.
    """
    return subprocess.run(
        [sys.executable, "-P", "-m", "aisland.cli", *args],
        cwd=get_workspace_root(),
        capture_output=True,
        text=True,
        timeout=SERVICE_TIMEOUT,
        check=False,
    )


def _service_state(name: str, process: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A service's state: from ``process`` (a supervisor listing's entry), else asked of the supervisor."""
    if process is None:
        process = supervisor.get_process(name) or {}
    running = bool(process.get("is_running"))
    return {
        "service": name,
        "running": running,
        "pid": int(process["pid"]) if running and process.get("pid") else None,
        "ports": supervisor.get_ports(name) if running else [],
    }


def _service_command(name: Service, args: List[str]) -> Dict[str, Any]:
    """Run ``aisland NAME ARGS`` (a name outside :data:`SERVICES` never gets here: the tool's schema refuses it)."""
    result = _run_aisland([name, *args])
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        raise ToolRefusal(output or f"aisland {name} {' '.join(args)} exited with {result.returncode}")
    return {**_service_state(name), "output": output}


# --- the tools ----------------------------------------------------------------------------


def tool_doctor() -> Dict[str, Any]:
    """Run `aisland doctor`'s environment checks: each check's name, whether it passed, what it found and the
    command that fixes it."""
    results = doctor.run_checks()
    return {"ok": all(r.ok for r in results), "python": sys.executable, "checks": [asdict(r) for r in results]}


def tool_tests_report(project: str, days: int = pytest_stats.DEFAULT_WINDOW_DAYS) -> Dict[str, Any]:
    """A project's test runs over the last `days` days (`aisland tests report PROJECT --json`): its fast run
    against the 5-minute budget, the slowest tests, the tests that failed, and what to move between the runs."""
    if not (pytest_stats.WORKSPACE_ROOT / project / "pyproject.toml").is_file():
        raise ToolRefusal(f"no project {project!r} in the workspace {pytest_stats.WORKSPACE_ROOT}")
    report: Dict[str, Any] = pytest_stats.build_report(project, days=days).to_dict()
    return report


def tool_editor_themes() -> Dict[str, Any]:
    """The colour themes VS Code offers on this machine — name, kind, the extension that brings it; `current`
    marks the workspace window's. Empty without VS Code."""
    chosen = editor.read_choice(get_workspace_root()).theme
    return {"themes": [{**asdict(theme), "current": theme.name == chosen} for theme in editor.list_themes()]}


def tool_editor_colors(part: str = "") -> Dict[str, Any]:
    """The workspace window's colour choice, and the CSS colour names (with their #rrggbb) holding `part` — all
    148 when `part` is empty."""
    return {
        "choice": _choice(editor.read_choice(get_workspace_root())),
        "colors": [{"name": name, "value": value} for name, value in editor.named_colors(part)],
    }


def tool_editor_set_theme(name: str) -> Dict[str, Any]:
    """Show the workspace window in theme `name` (as `editor_themes` lists it, case ignored); this machine only,
    every other window keeps the user's theme. Returns the window's choice."""
    root = get_workspace_root()
    editor.set_theme(root, name)
    manager.write_code_workspace(root)
    return _choice(editor.read_choice(root))


def tool_editor_set_color(
    role: editor.ColorRole, color: str, area: Optional[editor.ColorArea] = None
) -> Dict[str, Any]:
    """Colour the workspace window's background or foreground, over any theme: `color` a CSS name (navy, dark slate
    blue) or #RGB, #RGBA, #RRGGBB, #RRGGBBAA; `area` frame (title, activity and status bar), sidebar (also the
    side bar) or both (also the editor, side bar, panel and terminal), kept for later colours. Returns the window's choice.
    """
    root = get_workspace_root()
    editor.set_color(root, role, color, area)
    manager.write_code_workspace(root)
    return _choice(editor.read_choice(root))


def tool_editor_unset(what: EditorPart) -> Dict[str, Any]:
    """Give the workspace window back to the user's setting: `theme`, one colour (`background`, `foreground`), or
    `colors` — both colours and the area. Returns the window's choice."""
    root = get_workspace_root()
    if what == "theme":
        editor.set_theme(root, None)
    elif what == "colors":
        editor.clear_colors(root)
    else:
        editor.set_color(root, what, None)
    manager.write_code_workspace(root)
    return _choice(editor.read_choice(root))


def tool_sources() -> Dict[str, Any]:
    """Each managed project's source (`aisland source list`): committed, this machine's override, the effective one
    and the folder's state."""
    return {"sources": sources_mod.describe(get_workspace_root())}


def tool_services_status() -> Dict[str, Any]:
    """Every service the supervisor knows: running or not, its PID, its ports and the supervisor's status. Empty
    when no supervisor runs."""
    processes = supervisor.list_processes()
    return {
        "services": [
            {**_service_state(name, process), "status": process.get("status")}
            for name, process in sorted(processes.items())
        ]
    }


def tool_tunnel_list() -> Dict[str, Any]:
    """The workspaces opened through `aisland tunnel`, newest first (`aisland tunnel list --json`)."""
    return {"workspaces": tunnel_mod.store_payload(tunnel_mod.load_store())}


def tool_service_start(name: Service) -> Dict[str, Any]:
    """Start Jenkins or MLflow under the supervisor (`aisland <name> start --background`): its state after, and
    what the command printed."""
    return _service_command(name, ["start", "--background"])


def tool_service_stop(name: Service) -> Dict[str, Any]:
    """Stop Jenkins or MLflow (`aisland <name> stop`): its state after, and what the command printed."""
    return _service_command(name, ["stop"])


def tool_service_restart(name: Service) -> Dict[str, Any]:
    """Restart Jenkins or MLflow (`aisland <name> restart --background`): its state after, and what the command
    printed."""
    return _service_command(name, ["restart", "--background"])


_READING = (
    tool_doctor,
    tool_tests_report,
    tool_editor_themes,
    tool_editor_colors,
    tool_sources,
    tool_services_status,
    tool_tunnel_list,
)
_CHANGING = (
    tool_editor_set_theme,
    tool_editor_set_color,
    tool_editor_unset,
    tool_service_start,
    tool_service_stop,
    tool_service_restart,
)


# --- the server ---------------------------------------------------------------------------


def _build_server() -> Any:
    """The server with every tool registered; ``None`` without the ``[mcp]`` extra.

    Tools only: the request handlers for resources and prompts are dropped, so the capabilities advertise tools
    alone — an empty ``resources/list`` reads to some clients as a dead server. Each method name is derived from
    the request model's own ``method`` default, never written as a string.
    """
    if MCPServer is None:
        return None
    built = MCPServer("aisland", instructions=INSTRUCTIONS)
    for request_type in (
        mcp_types.ListResourcesRequest,
        mcp_types.ListResourceTemplatesRequest,
        mcp_types.ReadResourceRequest,
        mcp_types.SubscribeRequest,
        mcp_types.UnsubscribeRequest,
        mcp_types.ListPromptsRequest,
        mcp_types.GetPromptRequest,
    ):
        built._lowlevel_server._request_handlers.pop(request_type.model_fields["method"].default, None)
    reading = ToolAnnotations(read_only_hint=True)
    changing = ToolAnnotations(read_only_hint=False, destructive_hint=False)
    for tools, annotations in ((_READING, reading), (_CHANGING, changing)):
        for function in tools:
            built.tool(name=function.__name__[len("tool_") :], annotations=annotations)(
                _refusals_reach_the_client(function)
            )
    return built


mcp_server = _build_server()


def main() -> None:
    """``aisland-mcp``: serve the tools over stdio."""
    if mcp_server is None:
        sys.stderr.write(MISSING_EXTRA + "\n")
        raise SystemExit(1)
    mcp_server.run()


if __name__ == "__main__":
    main()
