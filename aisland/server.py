"""aisland's MCP server, ``aisland-mcp``: the workspace's dev infrastructure as tools an AI client calls.

Installed with the ``[mcp]`` extra (``pip install 'aisland[mcp]'``) and listed in the workspace's ``.mcp.json`` by
``aisland setup`` (a ``<pkg>.server:main`` script whose module imports ``mcp.server``). Not navigaitor's: navigaitor
serves the ML libraries, aisland the workspace it runs in.

The tools are a chosen set, never the whole CLI:

* reading — ``doctor``, ``tests_report``, ``editor_themes``, ``editor_colors``, ``sources``, ``services_status``,
  ``tunnel_list``, ``tunnel_status``, ``track_status``: each the data its command prints with ``--json`` (or would),
  from the same function;
* changing this machine, never destroying — ``editor_set_theme``, ``editor_set_color``, ``editor_unset`` (the
  workspace window's theme and colours, :mod:`aisland.editor`), ``service_start`` for any service of the workspace
  and ``tunnel_start`` (the remote tunnel to this machine);
* ending something running, marked destructive — ``service_stop``, ``service_restart`` and ``tunnel_stop``.

The service and tunnel tools each run the ``aisland`` command itself, so it behaves exactly as typed.

What prompts (``setup``), runs for minutes or deletes (``clean``, ``backups prune``, ``* reset``, ``jenkins init``,
``source set``) stays a command. Every tool answers with one JSON object; a refusal is the tool's error, in the
command's own words.

A tool's code says what it says through :mod:`aisland.console`, which each tool call collects
(:func:`aisland.console.collecting`): what it says on stderr — a warning — comes back in the tool's object as
``warnings``, the rest (the progress a person would watch) is dropped, and a question it asks is refused, since no one
is there to answer. A stray ``print`` or a subprocess's output is still kept off the protocol: over stdio the SDK
serves it from a private copy of stdout and points the process's stdout at stderr.

NOTE: no ``from __future__ import annotations`` — the MCP server reads the live annotations to build each tool's
argument schema.
"""

import functools
import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional, ParamSpec, TypeVar, cast

import click

from aisland import console, doctor, editor
from aisland.ci import stats_report
from aisland.errors import AislandError
from aisland.services import registry as service_registry
from aisland.setup import editor_files
from aisland.tunnel import host as tunnel_mod
from aisland.tunnel import store as tunnel_store
from aisland.workspace import sources as sources_mod
from aisland.workspace import tracking
from aisland.workspace.projects import get_workspace_root

try:
    import mcp.types as mcp_types
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError:  # the [mcp] extra is not installed: main() says so
    HAS_SDK = False
else:
    HAS_SDK = True

MISSING_EXTRA = "Error: aisland-mcp needs the mcp extra: pip install 'aisland[mcp]'"
#: The refusal when the SDK keeps its request handlers elsewhere: the one private attribute aisland reaches into.
UNSUPPORTED_SDK = (
    "aisland-mcp cannot drop the MCP SDK's resources and prompts handlers: this mcp release has no "
    "`MCPServer._lowlevel_server._request_handlers` table. Install a release aisland supports: pip install 'mcp>=2.0,<3'"
)

#: What ``editor_unset`` forgets: the theme, one colour, or both colours and the area.
EditorPart = Literal["theme", "background", "foreground", "colors"]
#: The accounts ``aisland tunnel start`` signs in with (its ``--provider`` choices).
TunnelProvider = Literal["github", "microsoft"]
#: How long a command a tool runs may take at least; a service whose start may take longer (a first Colima start
#: boots its VM) is given its own limit, ``START_SECONDS`` (twice that for a restart: a stop, then a start).
SERVICE_TIMEOUT = 300
#: How long a command past its limit has to end after SIGTERM before its whole process group is killed.
STOP_GRACE_SECONDS = 10

INSTRUCTIONS = """\
aisland manages this machine's development workspace: its environment checks, test statistics, project sources,
the services (Jenkins, the Docker engine, SwiftBar and the ones its projects bring), the VS Code window's theme
and colours, and the remote-editing tunnel to this machine.

Read first: `doctor` (what is broken and the command that fixes it), `services_status`, `sources`,
`tests_report(project)`. The editor tools change only this machine's workspace window: `editor_themes` and
`editor_colors` list the choices, `editor_set_theme` / `editor_set_color` / `editor_unset` apply them.
`service_start` / `service_stop` / `service_restart` take any service of the workspace by name. `tunnel_status` and
`tunnel_list` read the tunnel and the workspaces opened through it; `tunnel_start` / `tunnel_stop` bring it up or down
(a machine holding no account yet answers `signin_required` with the `aisland tunnel login` command the person runs
in a terminal; a sign-in the started tunnel asks for answers `pending_signin` with the code to hand the user).
`track_status` says which projects the root
repository records the commits of; `aisland workspace track set|unset` change it, as commands the person runs.
Setup, cleaning and resets are commands the person runs themselves (`aisland setup`, `aisland clean <project>`).
"""


_Result = TypeVar("_Result")
_Params = ParamSpec("_Params")


class ToolRefusal(AislandError):
    """A refusal a tool anticipated (no such project, a command that failed): its words reach the client."""


def _refusals_reach_the_client(function: Callable[_Params, _Result]) -> Callable[_Params, _Result]:
    """Run the tool inside a collecting console, its warnings added to its object as ``warnings``; raise an
    anticipated refusal — a :class:`ToolRefusal`, or aisland's own ``Error:`` (a :class:`click.ClickException`, a
    question no one can answer among them) — as the SDK's ``ToolError``, the one exception whose words the client reads.

    From mcp 2.1 on, any other exception reaches the client as ``Error executing tool <name>`` alone, its text kept
    on the server: right for a crash, which is left to the SDK, wrong for "no installed VS Code theme is named …".
    """

    @functools.wraps(function)
    def wrapper(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        with console.collecting() as said:
            try:
                result = function(*args, **kwargs)
            except (ToolRefusal, click.ClickException) as refusal:
                raise ToolError(str(refusal)) from refusal
        if said.err and isinstance(result, dict):
            return cast(_Result, {**result, "warnings": said.err})
        return result

    return wrapper


def _choice(choice: editor.EditorChoice) -> Dict[str, object]:
    """This machine's editor choice as the tools return it."""
    return asdict(choice)


def _run_as_a_group(command: List[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess[str]":
    """Run ``command`` in a session of its own, its output captured and its stdin empty; past ``timeout`` seconds its
    whole process group is ended — SIGTERM, then SIGKILL after :data:`STOP_GRACE_SECONDS` — so nothing it started
    (`colima start`'s VM helpers, a download) runs on unseen, and :class:`subprocess.TimeoutExpired` is raised."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _end_group(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _end_group(process: "subprocess.Popen[str]") -> None:
    """End ``process``'s group — SIGTERM, then SIGKILL after :data:`STOP_GRACE_SECONDS` — and reap it, never waiting
    on its pipes: a descendant in a session of its own may hold them open (and stay), and a group left with only the
    reaped-to-be leader may refuse the signal (EPERM on macOS)."""
    for signum in (signal.SIGTERM, signal.SIGKILL):  # the second for whatever of the group outlived the first
        try:
            os.killpg(process.pid, signum)
        except OSError:  # the group has gone, or holds only the exited leader
            pass
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def _run_aisland(args: List[str], timeout: float = SERVICE_TIMEOUT) -> "subprocess.CompletedProcess[str]":
    """Run ``aisland ARGS`` with this interpreter, at the workspace root, its output captured.

    ``-P`` keeps the root's ``aisland/`` checkout off ``sys.path`` (it would shadow the package); the root is the
    working folder because aisland installed from a wheel finds its workspace from there. Its stdin is empty, never
    the server's: that is the client's JSON-RPC stream, and a question the command asked (or a git credential
    prompt) would eat the protocol's bytes. A command that needs an answer gets none and refuses; one still running
    after ``timeout`` seconds is stopped with every process it started (:func:`_run_as_a_group`), and the tool
    refuses saying so.
    """
    try:
        return _run_as_a_group([sys.executable, "-P", "-m", "aisland.cli", *args], get_workspace_root(), timeout)
    except subprocess.TimeoutExpired as exc:
        raise ToolRefusal(
            f"aisland {' '.join(args)} was still running after {timeout:g} s, so it was stopped, with every process "
            "it had started; run it in a terminal to see where it waits"
        ) from exc


def _service_entry(name: str) -> service_registry.Entry:
    """The workspace's service called ``name``: aisland's, or one a project brings. A name no service has is refused
    in the command's own words (``no service 'x' (the services: …)``)."""
    return service_registry.chosen(service_registry.entries(get_workspace_root()), [name])[0]


def _service_command(name: str, action: str) -> Dict[str, object]:
    """Run ``aisland services ACTION NAME`` — ``start`` and ``restart`` in the background where the service runs under
    the supervisor (the container engine and the menu detach by themselves and take no ``--background``). Never the
    ``aisland NAME`` shortcut: a project's service named like an aisland command has none, and the command would run."""
    entry = _service_entry(name)
    args = ["services", action, *(["--background"] if action != "stop" and entry.takes_background else []), name]
    # A service's own start limit, when it has one longer than the default (a project's class may have none).
    limit = max(SERVICE_TIMEOUT, float(getattr(entry.make(), "START_SECONDS", SERVICE_TIMEOUT)))
    result = _run_aisland(args, timeout=2 * limit if action == "restart" else limit)
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        raise ToolRefusal(output or f"aisland {' '.join(args)} exited with {result.returncode}")
    return {**entry.make().state(), "output": output}


# --- the tools ----------------------------------------------------------------------------


def tool_doctor() -> Dict[str, object]:
    """Run `aisland doctor`'s environment checks: each check's name, whether it passed, what it found and the
    command that fixes it."""
    results = doctor.run_checks()
    return {"ok": all(r.ok for r in results), "python": sys.executable, "checks": [asdict(r) for r in results]}


def tool_tests_report(project: str, days: int = stats_report.DEFAULT_WINDOW_DAYS) -> Dict[str, object]:
    """A project's test runs over the last `days` days (`aisland tests report PROJECT --json`): its fast run
    against the 5-minute budget, the slowest tests, the tests that failed, and what to move between the runs."""
    folder = stats_report.project_folder(project)
    report: Dict[str, object] = stats_report.build_report(project, days=days, project_dir=folder).to_dict()
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
    """Every service of the workspace — aisland's and the ones its projects bring — with its state (`running`,
    `stopped`, `not installed`, `not supported`, `unknown`), PID, ports and readiness (`aisland services status
    --json`)."""
    entries = service_registry.entries(get_workspace_root())
    return {"services": service_registry.states(entries, ())}


def tool_tunnel_list() -> Dict[str, object]:
    """The workspaces opened through `aisland tunnel`, newest first (`aisland tunnel list --json`)."""
    return {"workspaces": tunnel_store.store_payload(tunnel_store.load_store())}


def tool_tunnel_status() -> Dict[str, object]:
    """Whether a remote-editing tunnel to this machine is running, under which name and account, and its address
    once it is up (`aisland tunnel status --json`)."""
    return tunnel_mod.status()


def tool_track_status(projects: Optional[List[str]] = None) -> Dict[str, object]:
    """Whether the root repository records each project's commits (`aisland workspace track status --json`): per
    project `tracked`, whose choice that is (`source`: this machine's, the project's own `track`, the workspace's or the
    default) and why in words (`reason`), and whether git's `ignore` agrees (`git`: `applied`, `not applied` — `aisland
    setup` writes it — or `yours`, a setting aisland leaves alone). `projects` names some; an unknown one is refused,
    listing the submodules. Changing it is the command `aisland workspace track set|unset`."""
    return tracking.status_json(get_workspace_root(), projects or [])


def _tunnel_command(args: List[str]) -> Dict[str, object]:
    """Run ``aisland tunnel ARGS --json`` and answer the JSON it printed, unchanged; its refusal in its own words."""
    line = ["tunnel", *args, "--json"]
    result = _run_aisland(line)
    if result.returncode != 0:
        said = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part.strip())
        raise ToolRefusal(said or f"aisland {' '.join(line)} exited with {result.returncode}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ToolRefusal(f"aisland {' '.join(line)} printed no JSON: {result.stdout.strip()}") from error
    if not isinstance(payload, dict):
        raise ToolRefusal(f"aisland {' '.join(line)} printed no JSON object: {result.stdout.strip()}")
    return payload


def tool_tunnel_start(name: Optional[str] = None, provider: Optional[TunnelProvider] = None) -> Dict[str, object]:
    """Bring up a remote-editing tunnel to THIS machine (`aisland tunnel start --json`): its status, with `url` once
    it is up. A machine holding no account yet starts nothing and answers `signin_required: true` with `command`
    (`aisland tunnel login [--provider …]`): the sign-in shows a device code in a terminal, so hand the person that
    command, then call this again. When the started tunnel's own log asks for a sign-in, it answers `pending_signin`
    (`url`, `code`) at once instead of waiting — hand the code to the user, then ask `tunnel_status`. A tunnel already
    running is reported, never a second started. `name` defaults to $WORKSPACE, else the hostname; `provider` is the
    account to sign in with, remembered afterwards. (A VS Code CLI archive of one's own, `aisland tunnel start --cli`, is installed and run
    unchecked, so it is a person's choice at the command line, never a tool's argument.)"""
    args = ["start"]
    for flag, value in (("--name", name), ("--provider", provider)):
        if value:
            args += [flag, value]
    return _tunnel_command(args)


def tool_tunnel_stop() -> Dict[str, object]:
    """Stop the remote-editing tunnel to this machine (`aisland tunnel stop --json`): `stopped` false when none ran.
    The remembered workspaces and the sign-in are kept."""
    return _tunnel_command(["stop"])


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
    tool_tunnel_status,
    tool_track_status,
)
_CHANGING = (
    tool_editor_set_theme,
    tool_editor_set_color,
    tool_editor_unset,
    tool_service_start,
    tool_tunnel_start,
)
#: The tools that end something running: stopping the container engine stops every container in it, and a stopped
#: tunnel drops every remote session — a client asks before it calls these.
_STOPPING = (
    tool_service_stop,
    tool_service_restart,
    tool_tunnel_stop,
)


# --- the server ---------------------------------------------------------------------------


def _build_server() -> "Optional[MCPServer]":
    """The server with every tool registered; ``None`` without the ``[mcp]`` extra.

    Tools only: the request handlers for resources and prompts are dropped, so the capabilities advertise tools
    alone — an empty ``resources/list`` reads to some clients as a dead server. Each method name is derived from
    the request model's own ``method`` default, never written as a string.
    """
    if not HAS_SDK:
        return None
    built = MCPServer("aisland", instructions=INSTRUCTIONS)
    # The SDK offers no way to leave a capability out, so its private table is edited — checked first, so a release
    # that renames it fails saying so, never with an AttributeError.
    handlers = getattr(getattr(built, "_lowlevel_server", None), "_request_handlers", None)
    if not isinstance(handlers, dict):
        raise AislandError(UNSUPPORTED_SDK)
    for request_type in (
        mcp_types.ListResourcesRequest,
        mcp_types.ListResourceTemplatesRequest,
        mcp_types.ReadResourceRequest,
        mcp_types.SubscribeRequest,
        mcp_types.UnsubscribeRequest,
        mcp_types.ListPromptsRequest,
        mcp_types.GetPromptRequest,
    ):
        handlers.pop(request_type.model_fields["method"].default, None)
    reading = ToolAnnotations(read_only_hint=True)
    changing = ToolAnnotations(read_only_hint=False, destructive_hint=False)
    stopping = ToolAnnotations(read_only_hint=False, destructive_hint=True)
    for tools, annotations in ((_READING, reading), (_CHANGING, changing), (_STOPPING, stopping)):
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
