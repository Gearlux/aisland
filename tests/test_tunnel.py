"""`aisland tunnel` — the tunnel process itself.

What must hold, and why:

- the **name** is the one thing the relay validates (lowercase, ``a-z0-9-``, 4-20 characters), and
  it is normally derived rather than typed, so a hostname with a dot or a capital must not reach it;
- the spawned command carries **nothing but** the license flag and the name: an extra flag is how a
  tunnel silently stops being the thing the docs describe;
- the state (CLI, log, pidfile, remembered workspaces) stays under the service data dir, because
  that is what makes ``aisland clean`` and ``aisland backups`` cover it, and what lets a remote box
  keep its sign-in on a persistent mount;
- ``--cli`` never reaches the network — it exists for the box that cannot;
- the group runs **outside a workspace**: the remote machine it is meant for has none, and the CLI
  refuses every other command there.

No test here touches the network or spawns a real process.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from aisland import locations, tunnel
from aisland.cli import main


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the tunnel's state at a scratch folder, so no test touches this machine's own."""
    monkeypatch.setenv(tunnel.TUNNEL_HOME_ENV, str(tmp_path / "tunnel"))
    return tmp_path / "tunnel"


class _FakeProcess:
    """A spawned process that only has a pid — and writes what the real one writes to its log."""

    def __init__(self, pid: int = 4711) -> None:
        self.pid = pid


def _spawn(log_text: str = "", pid: int = 4711) -> Any:
    """A ``Popen`` replacement that records its argv and writes ``log_text`` to the log handle."""

    calls: List[List[str]] = []

    def fake_popen(command: List[str], **kwargs: Any) -> _FakeProcess:
        calls.append(command)
        handle = kwargs.get("stdout")
        if handle is not None and log_text:
            handle.write(log_text.encode())
            handle.flush()
        return _FakeProcess(pid)

    fake_popen.calls = calls  # type: ignore[attr-defined]
    return fake_popen


def _cli_archive(path: Path) -> Path:
    """A tar.gz holding a single executable ``code`` member, like the real CLI download."""
    archive = path / "vscode_cli.tar.gz"
    payload = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("code")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return archive


# --- 1. names ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("sandbox-4821", "sandbox-4821"),
        ("Sandbox_4821", "sandbox-4821"),
        ("box.example.com", "box-example-com"),
        ("a" * 40, "a" * 20),
        ("ab", "ab00"),
        ("--", "tunnel"),
    ],
)
def test_a_name_is_normalized_to_what_the_relay_accepts(raw: str, expected: str) -> None:
    assert tunnel.sanitize_name(raw) == expected
    assert tunnel.NAME_MIN <= len(tunnel.sanitize_name(raw)) <= tunnel.NAME_MAX


def test_the_default_name_is_the_hosts_workspace_name_then_the_hostname() -> None:
    assert tunnel.default_name({"WORKSPACE": "Sandbox_48"}) == "sandbox-48"
    with patch.object(tunnel.socket, "gethostname", return_value="my.box.local"):
        assert tunnel.default_name({}) == "my-box-local"


# --- 2, 8, 10. the spawned command ------------------------------------------------------------


def test_the_tunnel_is_never_passed_an_account_option(state: Path) -> None:
    """`code tunnel` takes no --provider: it exits on a usage error, so the tunnel never comes up.

    Measured against the real CLI (2026-10-20): `code tunnel --provider microsoft` answers
    "error: unexpected argument '--provider' found", and the pod it was tried on sat with no tunnel
    and no account while `aisland tunnel start` reported success.
    """
    popen = _spawn()
    state.mkdir(parents=True, exist_ok=True)
    tunnel.provider_path().write_text("microsoft\n")  # remembered from an earlier sign-in

    with (
        patch.object(tunnel, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "signed_in", return_value="user@example.invalid"),
    ):
        tunnel.start(name="box-1", timeout=0)

    assert "--provider" not in popen.calls[0]
    assert popen.calls[0] == [str(state / "code"), "tunnel", "--accept-server-license-terms", "--name", "box-1"]


def test_an_unsigned_machine_is_signed_in_before_the_tunnel_is_spawned(state: Path) -> None:
    """The sign-in cannot ride on the tunnel: unsigned, `code tunnel` asks which provider, and a
    detached process can never answer that question."""
    popen = _spawn()
    order: List[str] = []

    def fake_login(provider: Optional[str] = None) -> int:
        order.append(f"login:{provider}")
        return 0

    def record_popen(command: List[str], **kwargs: Any) -> Any:
        order.append("spawn")
        return popen(command, **kwargs)

    state.mkdir(parents=True, exist_ok=True)
    tunnel.provider_path().write_text("github\n")
    with (
        patch.object(tunnel, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", record_popen),
        patch.object(tunnel, "signed_in", return_value=None),
        patch.object(tunnel, "login", fake_login),
    ):
        tunnel.start(timeout=0)

    assert order == ["login:github", "spawn"]


def test_machine_readable_start_reports_the_signin_instead_of_asking(state: Path) -> None:
    popen = _spawn()
    with (
        patch.object(tunnel, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "signed_in", return_value=None),
    ):
        payload = tunnel.start(name="box-1", timeout=0, sign_in=False)

    assert payload["signin_required"] is True
    assert payload["command"] == "aisland tunnel login"
    assert popen.calls == []


def test_sign_in_state_is_read_from_the_output_not_the_exit_code(state: Path) -> None:
    """`code tunnel user show` exits 0 whether or not an account is held (measured 2026-10-20)."""
    state.mkdir(parents=True, exist_ok=True)
    tunnel.cli_path().write_text("")

    with patch.object(tunnel.subprocess, "run", return_value=CompletedProcess([], 0, "not logged in\n", "")):
        assert tunnel.signed_in() is None

    with patch.object(tunnel.subprocess, "run", return_value=CompletedProcess([], 0, "user@example.invalid\n", "")):
        assert tunnel.signed_in() == "user@example.invalid"


def test_status_says_which_name_the_tunnel_registered(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    tunnel.log_path().write_text(
        "[tunnels] waiting\nOpen this link in your browser https://vscode.dev/tunnel/box-1/mnt/workspace\n"
    )

    assert tunnel.name_in_log() == "box-1"
    assert tunnel.status()["name"] == "box-1"


def test_start_spawns_the_license_flag_and_the_name_and_nothing_else(state: Path) -> None:
    popen = _spawn(log_text="Open this link in your browser https://vscode.dev/tunnel/box/x\n")
    with (
        patch.object(tunnel, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "_alive", return_value=True),
        patch.object(tunnel, "signed_in", return_value="user@example.invalid"),
    ):
        payload = tunnel.start(name="Box_1", timeout=0)

    assert popen.calls == [[str(state / "code"), "tunnel", "--accept-server-license-terms", "--name", "box-1"]]
    assert payload["url"] == "https://vscode.dev/tunnel/box/x"
    assert payload["running"] is True
    assert tunnel.read_pid() == 4711


def test_start_reports_a_pending_signin_instead_of_waiting_for_it(state: Path) -> None:
    log = "To grant access to the server, please log into https://github.com/login/device and use code ABCD-1234\n"
    with (
        patch.object(tunnel, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", _spawn(log_text=log)),
        patch.object(tunnel, "signed_in", return_value="user@example.invalid"),
    ):
        payload = tunnel.start(timeout=0)

    assert payload["pending_signin"] == {"url": "https://github.com/login/device", "code": "ABCD-1234"}
    assert "url" not in payload


def test_a_second_start_does_not_spawn_a_second_tunnel(state: Path) -> None:
    popen = _spawn()
    with (
        patch.object(tunnel, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "_alive", return_value=True),
        patch.object(tunnel, "signed_in", return_value="user@example.invalid"),
    ):
        tunnel.start(timeout=0)
        again = tunnel.start(timeout=0)

    assert len(popen.calls) == 1
    assert again["already_running"] is True


def test_the_provider_is_remembered_for_the_sign_in_only(state: Path) -> None:
    """The provider belongs to the sign-in, never to the tunnel — see the usage-error test above."""
    popen = _spawn()
    logins: List[Optional[str]] = []

    def fake_login(provider: Optional[str] = None) -> int:
        logins.append(provider)
        return 0

    with (
        patch.object(tunnel, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "signed_in", return_value=None),
        patch.object(tunnel, "login", fake_login),
    ):
        tunnel.start(provider="microsoft", timeout=0)
        tunnel.stop()
        tunnel.start(timeout=0)  # no flag: the remembered account is used
        tunnel.stop()
        tunnel.start(provider="github", timeout=0)

    assert logins == ["microsoft", "microsoft", "github"]
    assert tunnel.read_provider() == "github"
    assert all("--provider" not in call for call in popen.calls)


# --- 3, 4. the CLI binary ---------------------------------------------------------------------


def test_installing_from_an_archive_never_reaches_the_network(state: Path, tmp_path: Path) -> None:
    archive = _cli_archive(tmp_path)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the downloader must not be called when --cli names an archive")

    with patch.object(tunnel.urllib.request, "urlopen", explode):
        installed = tunnel.install_cli(tarball=archive)

    assert installed == state / "code"
    assert installed.read_bytes().startswith(b"#!/bin/sh")
    assert installed.stat().st_mode & 0o111


def test_an_archive_without_the_binary_is_refused_by_name(tmp_path: Path) -> None:
    empty = tmp_path / "empty.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    with pytest.raises(click.ClickException, match="holds no 'code' binary"):
        tunnel.install_cli(tarball=empty)


@pytest.mark.parametrize(
    "machine, asset",
    [("x86_64", "cli-alpine-x64"), ("AMD64", "cli-alpine-x64"), ("aarch64", "cli-alpine-arm64")],
)
def test_the_download_identifier_follows_the_architecture(machine: str, asset: str) -> None:
    assert tunnel.cli_asset(machine) == asset


def test_an_unsupported_architecture_is_refused_and_named() -> None:
    with pytest.raises(click.ClickException, match="ppc64le"):
        tunnel.cli_asset("ppc64le")


# --- 5. where state lives ---------------------------------------------------------------------


def test_the_default_state_folder_is_per_machine_not_per_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deriving it from the working directory made `start` here and `status` there disagree."""
    monkeypatch.delenv(tunnel.TUNNEL_HOME_ENV, raising=False)
    monkeypatch.setattr(tunnel.Path, "home", classmethod(lambda cls: Path("/home/someone")))

    assert tunnel.state_dir() == Path("/home/someone/.aisland/tunnel")

    monkeypatch.setenv(tunnel.TUNNEL_HOME_ENV, "/mnt/persistent/tunnel")
    assert tunnel.state_dir() == Path("/mnt/persistent/tunnel")


def test_every_path_is_in_the_one_state_folder(state: Path, tmp_path: Path) -> None:
    paths = [tunnel.cli_path(), tunnel.log_path(), tunnel.pid_path(), tunnel.provider_path(), tunnel.store_path()]
    assert all(path.parent == state for path in paths)
    assert {path.name for path in paths} == {"code", "tunnel.log", "tunnel.pid", "provider", "workspaces.json"}
    home = Path.home()
    assert not any(str(path).startswith(str(home / ".vscode")) for path in paths)


# --- 6, 7. status and stop --------------------------------------------------------------------


def test_status_json_carries_the_documented_keys(state: Path) -> None:
    result = CliRunner().invoke(main, ["tunnel", "status", "--json"])
    assert result.exit_code == 0
    payload: Dict[str, Any] = json.loads(result.output)
    assert set(payload) >= {"running", "pid", "name", "provider", "cli", "log", "workspaces"}
    assert payload["running"] is False


def test_status_without_a_tunnel_says_so_and_succeeds() -> None:
    result = CliRunner().invoke(main, ["tunnel", "status"])
    assert result.exit_code == 0
    assert "No tunnel running" in result.output


def test_stop_without_a_tunnel_is_not_an_error(state: Path) -> None:
    result = CliRunner().invoke(main, ["tunnel", "stop"])
    assert result.exit_code == 0
    assert "No tunnel was running" in result.output


def test_stop_signals_the_recorded_pid_and_clears_the_pidfile(state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    tunnel.pid_path().write_text("4711\n")
    with patch.object(tunnel, "_alive", return_value=True), patch.object(tunnel.os, "kill") as kill:
        payload = tunnel.stop()

    assert payload == {"running": False, "stopped": True, "pid": 4711}
    assert kill.call_args[0][0] == 4711
    assert not tunnel.pid_path().exists()


# --- 9. the tunnel runs where there is no workspace -------------------------------------------


def test_the_group_runs_outside_a_workspace_while_other_commands_are_refused(
    monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    monkeypatch.setattr(locations, "workspace_found", lambda: False)
    runner = CliRunner()

    allowed = runner.invoke(main, ["tunnel", "status"])
    refused = runner.invoke(main, ["doctor"])

    assert allowed.exit_code == 0, allowed.output
    assert refused.exit_code != 0
    assert "No workspace here" in refused.output or "is not a folder" in refused.output


def test_the_log_is_shown_when_there_is_one_and_named_when_there_is_not(state: Path) -> None:
    runner = CliRunner()
    assert "No log yet" in runner.invoke(main, ["tunnel", "logs"]).output

    state.mkdir(parents=True, exist_ok=True)
    tunnel.log_path().write_text("line one\nline two\n")
    assert "line two" in runner.invoke(main, ["tunnel", "logs"]).output


def test_the_relay_probe_counts_any_http_answer_as_reachable() -> None:
    class _Response:
        status = 404

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc: Optional[object]) -> None:
            return None

    with patch.object(tunnel.urllib.request, "urlopen", return_value=_Response()):
        ok, detail = tunnel.relay_reachable()
    assert ok is True
    assert "404" in detail

    with patch.object(tunnel.urllib.request, "urlopen", side_effect=OSError("no route to host")):
        ok, detail = tunnel.relay_reachable()
    assert ok is False
    assert "unreachable" in detail
