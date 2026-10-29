"""The MCP client files: ``.mcp.json`` (Claude Code) and ``.gemini/settings.json``, listing every MCP server
the workspace's projects ship, each started with the workspace's environment."""

import json
import shutil
import tomllib
from pathlib import Path
from typing import Dict, Optional

import click

from aisland import locations
from aisland.errors import AislandError
from aisland.projects import discover_projects, get_workspace_root


def discover_mcp_servers(workspace_root: Path, venv_bin: Path) -> dict[str, dict]:
    """Discover workspace MCP servers and how to launch them.

    A project is treated as an MCP server when its ``[project.scripts]`` declares
    an entry whose target is ``<pkg>.server:main`` AND that ``server.py`` imports the
    MCP SDK's server package (``mcp.server`` — ``mcp.server.mcpserver.MCPServer`` in
    SDK 2.x, ``mcp.server.fastmcp.FastMCP`` in 1.x; the check reads the PACKAGE, not a
    class name, because the class-name check went stale with the 2.x rename and a
    regeneration silently listed navigaitor alone, 2026-09-05). The script name is the
    console command installed into the venv; the server is keyed by the project name.
    Discovery (not a hand-maintained list) keeps the output tracking the live set
    — drop in a new ``*-mcp`` project and it appears automatically.

    Returns ``{server_name: {"command": "<venv_bin>/<script>"}}`` with ABSOLUTE
    commands resolved against the LOCAL venv. Every client launches each server as a
    subprocess from an arbitrary cwd, so the command must be absolute — which is
    exactly why the client files (``.gemini/settings.json``, ``.mcp.json``) are
    gitignored and regenerated per machine instead of committed.
    """
    servers: dict[str, dict] = {}
    for pyproject in discover_projects(workspace_root):
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):  # discovery reports a broken file; here it lists no server
            continue
        project = data.get("project", {})
        name = project.get("name")
        scripts = project.get("scripts", {})
        if not name or not isinstance(scripts, dict):
            continue
        for script_name, target in scripts.items():
            module, _, attr = str(target).partition(":")
            if not module.endswith(".server") or attr != "main":
                continue
            server_file = pyproject.parent.joinpath(*module.split(".")).with_suffix(".py")
            try:
                text = server_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if "mcp.server" not in text:
                continue  # a `.server:main` script that is not an MCP server (a web server, say)
            servers[name] = {"command": str(venv_bin / script_name)}
            break  # one MCP server per project
    return servers


def _warn_missing_commands(servers: Dict[str, dict]) -> None:
    """Name every discovered server whose console script is NOT in the venv — the tell of an
    editable install that predates the project's ``[project.scripts]`` entry (measured
    2026-09-05: annotaide's ``annotaide-mcp`` was discovered and written, and did not exist,
    so the client would have spawned a missing binary). The map is still written — the
    reinstall is the fix, and the next ``aisland setup`` performs it."""
    for name, entry in sorted(servers.items()):
        command = Path(entry["command"])
        if not command.exists():
            click.echo(
                f"  - Warning: {name}'s MCP command {command} is not installed — refresh the editable install"
                f" (`uv pip install -e ./{name}`) so the console script exists."
            )


#: The bash program a client runs to start a server: source the workspace shell init (its file is ``$0``) with its
#: output discarded — the server's stdout is its JSON-RPC stream — then become the server (``"$@"``).
_WITH_WORKSPACE_ENV = 'source "$0" >/dev/null; exec "$@"'


def _with_workspace_env(workspace_root: Path, servers: Dict[str, dict]) -> Dict[str, dict]:
    """Each server started through bash sourcing ``<workspace>/project.bashrc`` first.

    A client starts a server with ITS OWN environment, never the workspace's ``.env`` (measured
    2026-10-21: a desktop app's MCP server ran with ``DATA_ROOT`` unset, so a download that names
    no folder landed where the server happened to start). The shell init exports ``.env`` and sets
    ``PROJECT_ROOT`` the way a terminal gets them, so a server sees what a run from a shell sees;
    ``.env``'s values are never copied into the client file, which would put its secrets there.
    Without the shell init or without bash the server is started directly, as before.
    """
    bashrc = workspace_root / "project.bashrc"
    bash = shutil.which("bash")
    if bash is None or not bashrc.exists():
        return servers
    return {
        name: {"command": bash, "args": ["-c", _WITH_WORKSPACE_ENV, str(bashrc), entry["command"]]}
        for name, entry in servers.items()
    }


def write_gemini_settings(workspace_root: Path, venv_bin: Path) -> None:
    """Generate ``<workspace>/.gemini/settings.json`` registering workspace MCP servers.

    Unlike ``pyrightconfig.json`` (relative paths), Gemini CLI launches each
    server as a subprocess, so ``command`` must be an ABSOLUTE venv path. That
    machine-specific absolute is why this file is gitignored and regenerated on
    every ``aisland setup`` (and via ``aisland mcp gemini``) rather than
    committed. The server set is discovered, never hand-maintained.
    """
    servers = discover_mcp_servers(workspace_root, venv_bin)
    _warn_missing_commands(servers)
    gemini_dir = workspace_root / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    out = gemini_dir / "settings.json"
    out.write_text(json.dumps({"mcpServers": _with_workspace_env(workspace_root, servers)}, indent=2) + "\n")
    listed = ", ".join(sorted(servers)) or "none"
    click.echo(f"==> Wrote {out} ({len(servers)} MCP server(s): {listed})")


def write_mcp_json(workspace_root: Path, venv_bin: Path) -> None:
    """Generate ``<workspace>/.mcp.json`` — Claude Code's PROJECT-scoped MCP servers.

    Same discovered server map as the Gemini settings, same absolute venv commands,
    same reason to stay gitignored. Claude Code reads the file when a session starts in
    the workspace and asks once whether to use the project's servers; until this file
    existed no workspace server reached a Claude Code session (``claude mcp list`` showed
    only the user's own servers, measured 2026-09-05).
    """
    servers = discover_mcp_servers(workspace_root, venv_bin)
    _warn_missing_commands(servers)
    out = workspace_root / ".mcp.json"
    out.write_text(json.dumps({"mcpServers": _with_workspace_env(workspace_root, servers)}, indent=2) + "\n")
    listed = ", ".join(sorted(servers)) or "none"
    click.echo(f"==> Wrote {out} ({len(servers)} MCP server(s): {listed})")


def no_venv(workspace_root: Path) -> click.ClickException:
    """The refusal of a command that needs the workspace's ``.venv`` when it has none, with how to make one."""
    if locations.FROM_CHECKOUT:
        return AislandError(f"Virtual environment not found at {workspace_root / '.venv'}. Run aisland/setup.sh first.")
    return AislandError(
        f"Virtual environment not found at {workspace_root / '.venv'}. Create it and install aisland "
        "into it: uv venv --seed .venv && .venv/bin/pip install aisland"
    )


def _venv_bin_or_none(workspace_root: Path) -> Optional[Path]:
    venv_bin = workspace_root / ".venv" / "bin"
    if not venv_bin.exists():
        raise AislandError(f"Virtual environment not found at {workspace_root / '.venv'}. Run aisland/setup.sh first.")
    return venv_bin


def generate_gemini_settings() -> None:
    """Regenerate ``.gemini/settings.json`` for the local workspace + venv (standalone command)."""
    workspace_root = get_workspace_root()
    venv_bin = _venv_bin_or_none(workspace_root)
    if venv_bin is not None:
        write_gemini_settings(workspace_root, venv_bin)


def generate_mcp_json() -> None:
    """Regenerate ``.mcp.json`` (Claude Code) for the local workspace + venv (standalone command)."""
    workspace_root = get_workspace_root()
    venv_bin = _venv_bin_or_none(workspace_root)
    if venv_bin is not None:
        write_mcp_json(workspace_root, venv_bin)
