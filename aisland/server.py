"""aisland's MCP server, ``aisland-mcp``: the workspace's dev infrastructure as tools an AI client calls.

Installed with the ``[mcp]`` extra (``pip install 'aisland[mcp]'``) and listed in the workspace's ``.mcp.json`` by
``aisland setup`` (a ``<pkg>.server:main`` script whose module imports ``mcp.server``). Not navigaitor's: navigaitor
serves the ML libraries, aisland the workspace it runs in (user ruling 2026-10-21).

The tools are a chosen set, never the whole CLI (user choice 2026-10-21):

* reading — ``doctor``, ``tests_report``, ``editor_themes``, ``editor_colors``, ``sources``, ``services_status``,
  ``tunnel_list``: each the data its command prints with ``--json`` (or would), from the same function;
* changing this machine, never destroying — ``editor_set_theme``, ``editor_set_color``, ``editor_unset`` (the
  workspace window's theme and colours, :mod:`aisland.editor`) and ``service_start``/``stop``/``restart`` for any
  service of the workspace, each run as the ``aisland`` command itself so it behaves exactly as typed.

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
from typing import Callable, Dict, List, Literal, Optional, ParamSpec, TypeVar

import click

from aisland import cli, doctor, editor, editor_files, pytest_stats
from aisland import sources as sources_mod
from aisland import supervisor
from aisland import tunnel as tunnel_mod
from aisland.errors import AislandError
from aisland.projects import get_workspace_root
from aisland.services import registry as service_registry

try:
    import mcp.types as mcp_types
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError:  # the [mcp] extra is not installed: main() says so
    MCPServer = None  # type: ignore[assignment,misc]

MISSING_EXTRA = "Error: aisland-mcp needs the mcp extra: pip install 'aisland[mcp]'"

#: What ``editor_unset`` forgets: the theme, one colour, or both colours and the area.
EditorPart = Literal["theme", "background", "foreground", "colors"]
#: How long a service command may take: Jenkins' start waits for its readiness probe.
SERVICE_TIMEOUT = 300

INSTRUCTIONS = """\
aisland manages this machine's development workspace: its environment checks, test statistics, project sources,
the services (Jenkins, the Docker engine, SwiftBar and the ones its projects bring) and the VS Code window's theme
and colours.

Read first: `doctor` (what is broken and the command that fixes it), `services_status`, `sources`,
`tests_report(project)`. The editor tools change only this machine's workspace window: `editor_themes` and
`editor_colors` list the choices, `editor_set_theme` / `editor_set_color` / `editor_unset` apply them.
`service_start` / `service_stop` / `service_restart` take any service of the workspace by name. Setup, cleaning and resets are
commands the person runs themselves (`aisland setup`, `aisland clean <project>`).
"""


_Result = TypeVar("_Result")
_Params = ParamSpec("_Params")


class ToolRefusal(AislandError):
    """A refusal a tool anticipated (no such project, a command that failed): its words reach the client."""


def _refusals_reach_the_client(function: Callable[_Params, _Result]) -> Callable[_Params, _Result]:
    """Raise an anticipated refusal — a :class:`ToolRefusal`, or aisland's own ``Error:`` (a
    :class:`click.ClickException`) — as the SDK's ``ToolError``, the one exception whose words the client reads.

    From mcp 2.1 on, any other exception reaches the client as ``Error executing tool <name>`` alone, its text kept
    on the server: right for a crash, which is left to the SDK, wrong for "no installed VS Code theme is named …".
    """

    @functools.wraps(function)
    def wrapper(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        try:
            return function(*args, **kwargs)
        except (ToolRefusal, click.ClickException) as refusal:
            raise ToolError(str(refusal)) from refusal

    return wrapper


def _choice(choice: editor.EditorChoice) -> Dict[str, object]:
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


def _service_state(name: str, process: Optional[supervisor.ProcessState] = None) -> Dict[str, object]:
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


def _service_entry(name: str) -> service_registry.Entry:
    """The workspace's service called ``name``: aisland's, or one a project brings. A name no service has is refused
    in the command's own words (``no service 'x' (the services: …)``)."""
    return service_registry.chosen(service_registry.entries(get_workspace_root(), reserved=cli.main.commands), [name])[
        0
    ]


def _service_command(name: str, action: str) -> Dict[str, object]:
    """Run ``aisland NAME ACTION`` — ``start`` and ``restart`` in the background where the service runs under the
    supervisor (the container engine and the menu detach by themselves and take no ``--background``)."""
    entry = _service_entry(name)
    args = [action, "--background"] if action != "stop" and entry.takes_background else [action]
    result = _run_aisland([name, *args])
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        raise ToolRefusal(output or f"aisland {name} {' '.join(args)} exited with {result.returncode}")
    return {**_service_state(name), "output": output}


# --- the tools ----------------------------------------------------------------------------


def tool_doctor() -> Dict[str, object]:
    """Run `aisland doctor`'s environment checks: each check's name, whether it passed, what it found and the
    command that fixes it."""
    results = doctor.run_checks()
    return {"ok": all(r.ok for r in results), "python": sys.executable, "checks": [asdict(r) for r in results]}


def tool_tests_report(project: str, days: int = pytest_stats.DEFAULT_WINDOW_DAYS) -> Dict[str, object]:
    """A project's test runs over the last `days` days (`aisland tests report PROJECT --json`): its fast run
    against the 5-minute budget, the slowest tests, the tests that failed, and what to move between the runs."""
    if not (pytest_stats.WORKSPACE_ROOT / project / "pyproject.toml").is_file():
        raise ToolRefusal(f"no project {project!r} in the workspace {pytest_stats.WORKSPACE_ROOT}")
    report: Dict[str, object] = pytest_stats.build_report(project, days=days).to_dict()
    return report


def tool_editor_themes() -> Dict[str, object]:
    """The colour themes VS Code offers on this machine — name, kind, the extension that brings it; `current`
    marks the workspace window's. Empty without VS Code."""
    chosen = editor.read_choice(get_workspace_root()).theme
    return {"themes": [{**asdict(theme), "current": theme.name == chosen} for theme in editor.list_themes()]}


def tool_editor_colors(part: str = "") -> Dict[str, object]:
    """The workspace window's colour choice, and the CSS colour names (with their #rrggbb) holding `part` — all
    148 when `part` is empty."""
    return {
        "choice": _choice(editor.read_choice(get_workspace_root())),
        "colors": [{"name": name, "value": value} for name, value in editor.named_colors(part)],
    }


def tool_editor_set_theme(name: str) -> Dict[str, object]:
    """Show the workspace window in theme `name` (as `editor_themes` lists it, case ignored); this machine only,
    every other window keeps the user's theme. Returns the window's choice. On a machine without VS Code (a remote
    one: its window's themes are the local VS Code's) `name` is kept as typed and a `note` says it is unchecked."""
    root = get_workspace_root()
    theme = editor.set_theme(root, name)
    editor_files.write_code_workspace(root)
    choice = _choice(editor.read_choice(root))
    if theme is not None and not theme.checked:
        choice["note"] = editor.UNCHECKED_THEME
    return choice


def tool_editor_set_color(
    role: editor.ColorRole, color: str, area: Optional[editor.ColorArea] = None
) -> Dict[str, object]:
    """Colour the workspace window's background or foreground, over any theme: `color` a CSS name (navy, dark slate
    blue) or #RGB, #RGBA, #RRGGBB, #RRGGBBAA; `area` frame (title, activity and status bar), sidebar (also the
    side bar) or both (also the editor, side bar, panel and terminal), kept for later colours. Returns the window's choice.
    """
    root = get_workspace_root()
    editor.set_color(root, role, color, area)
    editor_files.write_code_workspace(root)
    return _choice(editor.read_choice(root))


def tool_editor_unset(what: EditorPart) -> Dict[str, object]:
    """Give the workspace window back to the user's setting: `theme`, one colour (`background`, `foreground`), or
    `colors` — both colours and the area. Returns the window's choice."""
    root = get_workspace_root()
    if what == "theme":
        editor.set_theme(root, None)
    elif what == "colors":
        editor.clear_colors(root)
    else:
        editor.set_color(root, what, None)
    editor_files.write_code_workspace(root)
    return _choice(editor.read_choice(root))


def tool_sources() -> Dict[str, object]:
    """Each managed project's source (`aisland workspace configure source`): committed, this machine's override, the effective one
    and the folder's state."""
    return {"sources": sources_mod.describe(get_workspace_root())}


def tool_services_status() -> Dict[str, object]:
    """Every service the supervisor knows: running or not, its PID, its ports and the supervisor's status. Empty
    when no supervisor runs."""
    processes = supervisor.list_processes()
    return {
        "services": [
            {**_service_state(name, process), "status": process.get("status")}
            for name, process in sorted(processes.items())
        ]
    }


def tool_tunnel_list() -> Dict[str, object]:
    """The workspaces opened through `aisland tunnel`, newest first (`aisland tunnel list --json`)."""
    return {"workspaces": tunnel_mod.store_payload(tunnel_mod.load_store())}


def tool_service_start(name: str) -> Dict[str, object]:
    """Start a service of the workspace by name — aisland's (docker, jenkins, swiftbar) or one a project brings, as
    `services_status` and `aisland services status` name them (`aisland <name> start`, in the background): its state
    after, and what the command printed. A name no service has is refused, listing the services."""
    return _service_command(name, "start")


def tool_service_stop(name: str) -> Dict[str, object]:
    """Stop a service of the workspace (`aisland <name> stop`): its state after, and what the command printed."""
    return _service_command(name, "stop")


def tool_service_restart(name: str) -> Dict[str, object]:
    """Restart a service of the workspace (`aisland <name> restart`, in the background): its state after, and what
    the command printed."""
    return _service_command(name, "restart")


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


def _build_server() -> "Optional[MCPServer]":
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
