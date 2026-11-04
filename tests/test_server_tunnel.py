"""aisland's MCP server drives the remote tunnel: `tunnel_status` reads this machine's tunnel, `tunnel_start` and
`tunnel_stop` run `aisland tunnel start|stop --json` itself and answer what it printed.

What must hold is the seam, not the tunnel: the status is the data `aisland tunnel status --json` prints, from the
same function; a start or stop is the command as a person types it, its JSON handed back unchanged, so the agent and
the user agree about what runs; a start whose sign-in is pending answers the code to hand over (the command never
waits for a person with `--json`); a refusal of the command reaches the client in its own words. No test runs the
tunnel or the command.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any, Dict, List, Optional

import pytest
from mcp.client import Client

from aisland import server
from aisland.tunnel import host as tunnel_mod


def _result(name: str, args: Optional[Dict[str, Any]] = None) -> Any:
    built = server.mcp_server
    assert built is not None

    async def go() -> Any:
        async with Client(built) as connection:
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


class _Commands:
    """Stands in for `aisland …`: records each command line, answers `stdout` and `code`."""

    def __init__(self) -> None:
        self.calls: List[List[str]] = []
        self.stdout = json.dumps({"running": True, "pid": 41, "name": "box", "url": "https://vscode.dev/tunnel/box"})
        self.stderr = ""
        self.code = 0

    def __call__(self, args: List[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.code, stdout=self.stdout, stderr=self.stderr)


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> _Commands:
    fake = _Commands()
    monkeypatch.setattr(server, "_run_aisland", fake)
    return fake


def test_the_status_is_what_the_status_command_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"running": False, "pid": None, "name": None, "account": None, "provider": None, "workspaces": 2}
    monkeypatch.setattr(tunnel_mod, "status", lambda: dict(payload))
    assert _call("tunnel_status") == payload


@pytest.mark.parametrize(
    ("args", "line"),
    [
        ({}, ["tunnel", "start", "--json"]),
        (
            {"name": "box", "provider": "github", "cli": "/tmp/vscode_cli.tar.gz"},
            ["tunnel", "start", "--name", "box", "--provider", "github", "--cli", "/tmp/vscode_cli.tar.gz", "--json"],
        ),
    ],
)
def test_a_start_runs_the_start_command_and_answers_what_it_printed(
    commands: _Commands, args: Dict[str, Any], line: List[str]
) -> None:
    assert _call("tunnel_start", args) == json.loads(commands.stdout)
    assert commands.calls == [line]


def test_a_start_waiting_for_a_sign_in_answers_the_code_to_hand_over(commands: _Commands) -> None:
    commands.stdout = json.dumps({"pending_signin": {"url": "https://github.com/login/device", "code": "AB12-CD34"}})
    assert _call("tunnel_start")["pending_signin"]["code"] == "AB12-CD34"


def test_a_stop_runs_the_stop_command(commands: _Commands) -> None:
    commands.stdout = json.dumps({"running": False, "stopped": True, "pid": 41})
    assert _call("tunnel_stop") == {"running": False, "stopped": True, "pid": 41}
    assert commands.calls == [["tunnel", "stop", "--json"]]


def test_a_refused_command_reaches_the_client_in_its_own_words(commands: _Commands) -> None:
    commands.code, commands.stdout, commands.stderr = 1, "", "Error: Could not stop the tunnel (pid 41): denied\n"
    assert "Error: Could not stop the tunnel (pid 41): denied" in _error("tunnel_stop")


def test_a_command_that_printed_no_json_is_refused_saying_so(commands: _Commands) -> None:
    commands.stdout = "Tunnel stopped.\n"
    assert "aisland tunnel stop --json printed no JSON: Tunnel stopped." in _error("tunnel_stop")


def test_a_provider_aisland_does_not_offer_is_refused_before_anything_runs(commands: _Commands) -> None:
    assert _result("tunnel_start", {"provider": "gitlab"}).is_error
    assert commands.calls == []


def test_the_status_only_reads_and_a_start_or_stop_destroys_nothing() -> None:
    assert server.mcp_server is not None
    hints = {tool.name: tool.annotations for tool in asyncio.run(server.mcp_server.list_tools())}
    status = hints["tunnel_status"]
    assert status is not None and status.read_only_hint is True
    for name in ("tunnel_start", "tunnel_stop"):
        changing = hints[name]
        assert changing is not None and changing.read_only_hint is False and changing.destructive_hint is False
