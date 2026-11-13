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
import os
import signal
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Dict, List, Optional
from unittest.mock import call, patch

import click
import pytest
from click.testing import CliRunner

from aisland import downloads, locations
from aisland.cli import main
from aisland.errors import AislandError
from aisland.tunnel import binary as tunnel_binary
from aisland.tunnel import host as tunnel
from aisland.tunnel import state as tunnel_state
from aisland.tunnel import store as tunnel_store


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the tunnel's state at a scratch folder, so no test touches this machine's own."""
    monkeypatch.setenv(tunnel_state.TUNNEL_HOME_ENV, str(tmp_path / "tunnel"))
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
    assert tunnel_state.sanitize_name(raw) == expected
    assert tunnel_state.NAME_MIN <= len(tunnel_state.sanitize_name(raw)) <= tunnel_state.NAME_MAX


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
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
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
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", record_popen),
        patch.object(tunnel, "signed_in", return_value=None),
        patch.object(tunnel, "login", fake_login),
    ):
        tunnel.start(timeout=0)

    assert order == ["login:github", "spawn"]


def test_machine_readable_start_reports_the_signin_instead_of_asking(state: Path) -> None:
    popen = _spawn()
    with (
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
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
    tunnel_binary.cli_path().write_text("")

    with patch.object(tunnel.subprocess, "run", return_value=CompletedProcess([], 0, "not logged in\n", "")):
        assert tunnel.signed_in() is None

    with patch.object(tunnel.subprocess, "run", return_value=CompletedProcess([], 0, "user@example.invalid\n", "")):
        assert tunnel.signed_in() == "user@example.invalid"


def test_a_cli_that_does_not_answer_whether_it_is_signed_in_is_refused_naming_it(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tunnel status` and `tunnel start` (and their MCP tools) wait on this question: a CLI that hangs is named
    after `ANSWER_SECONDS`, never waited on."""
    state.mkdir(parents=True, exist_ok=True)
    tunnel_binary.cli_path().write_text("#!/bin/sh\nsleep 60\n")
    tunnel_binary.cli_path().chmod(0o755)
    monkeypatch.setattr(tunnel, "ANSWER_SECONDS", 1)
    with pytest.raises(AislandError, match=r"`code tunnel user show` did not answer within 1 s"):
        tunnel.signed_in()


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
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "_is_tunnel", return_value=True),
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
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", _spawn(log_text=log)),
        patch.object(tunnel, "signed_in", return_value="user@example.invalid"),
    ):
        payload = tunnel.start(timeout=0)

    assert payload["pending_signin"] == {"url": "https://github.com/login/device", "code": "ABCD-1234"}
    assert "url" not in payload


def test_a_second_start_does_not_spawn_a_second_tunnel(state: Path) -> None:
    popen = _spawn()
    with (
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "_is_tunnel", return_value=True),
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
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
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
        installed = tunnel_binary.install_cli(tarball=archive)

    assert installed == state / "code"
    assert installed.read_bytes().startswith(b"#!/bin/sh")
    assert installed.stat().st_mode & 0o111


_ARCHIVE_URL = "https://download.example/vscode_cli.tar.gz"


def _serve(archive: Path, fetched: List[str], digest: Optional[str] = None, release: Optional[str] = None) -> Any:
    """A stand-in for ``downloads.download``: the update service's document for the release url, else the archive."""

    def fetch(url: str, target: Path, timeout: float = downloads.TIMEOUT) -> None:
        fetched.append(url)
        if url.startswith("https://update.code.visualstudio.com/"):
            listed = digest if digest is not None else downloads.sha256(archive)
            target.write_text(
                release if release is not None else json.dumps({"url": _ARCHIVE_URL, "sha256hash": listed})
            )
        else:
            target.write_bytes(archive.read_bytes())

    return fetch


def test_the_cli_is_fetched_through_the_shared_download_and_installed(state: Path, tmp_path: Path) -> None:
    archive = _cli_archive(tmp_path)
    fetched: List[str] = []

    with patch.object(downloads, "download", _serve(archive, fetched)):
        installed = tunnel_binary.install_cli()

    assert fetched == [tunnel_binary.CLI_RELEASE_URL.format(asset=tunnel_binary.cli_asset()), _ARCHIVE_URL]
    assert installed == state / "code"
    assert installed.stat().st_mode & 0o111


def test_a_cli_whose_digest_differs_from_the_listed_one_is_refused_and_never_installed(
    state: Path, tmp_path: Path
) -> None:
    archive = _cli_archive(tmp_path)

    with patch.object(downloads, "download", _serve(archive, [], digest="0" * 64)):
        with pytest.raises(AislandError, match=f"has SHA-256 {downloads.sha256(archive)}, not the 0{{64}}"):
            tunnel_binary.install_cli()

    assert not (state / "code").exists()


@pytest.mark.parametrize(
    "release",
    [
        "not json",
        json.dumps({"url": _ARCHIVE_URL}),
        json.dumps({"url": "http://plain.example/x", "sha256hash": "a" * 64}),
    ],
)
def test_an_archive_the_update_service_gives_no_digest_or_https_url_for_is_never_fetched(
    state: Path, tmp_path: Path, release: str
) -> None:
    archive = _cli_archive(tmp_path)
    fetched: List[str] = []

    with patch.object(downloads, "download", _serve(archive, fetched, release=release)):
        with pytest.raises(AislandError, match="VS Code update service"):
            tunnel_binary.install_cli()

    assert _ARCHIVE_URL not in fetched
    assert not (state / "code").exists()


def test_a_cli_download_that_fails_names_the_url_and_the_way_around_it(state: Path) -> None:
    def fetch(url: str, target: Path, timeout: float = downloads.TIMEOUT) -> None:
        raise TimeoutError("timed out")

    with patch.object(downloads, "download", fetch):
        with pytest.raises(AislandError, match="Could not download the VS Code CLI from https://.*timed out") as raised:
            tunnel_binary.install_cli()

    assert "aisland tunnel start --cli" in str(raised.value)
    assert not (state / "code").exists()


def test_an_archive_without_the_binary_is_refused_by_name(tmp_path: Path) -> None:
    empty = tmp_path / "empty.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    with pytest.raises(click.ClickException, match="holds no 'code' binary"):
        tunnel_binary.install_cli(tarball=empty)


@pytest.mark.parametrize(
    "system, machine, asset",
    [
        ("Linux", "x86_64", "cli-alpine-x64"),
        ("Linux", "AMD64", "cli-alpine-x64"),
        ("Linux", "aarch64", "cli-alpine-arm64"),
        ("Darwin", "arm64", "cli-darwin-arm64"),
        ("Darwin", "x86_64", "cli-darwin-x64"),
    ],
)
def test_the_download_identifier_follows_the_system_and_the_architecture(system: str, machine: str, asset: str) -> None:
    """macOS gets macOS's CLI: Linux's alpine build there ran into `Exec format error` on the first `status`."""
    assert tunnel_binary.cli_asset(machine, system) == asset


@pytest.mark.parametrize("system, machine", [("Linux", "ppc64le"), ("FreeBSD", "amd64")])
def test_an_unsupported_system_or_architecture_is_refused_and_named(system: str, machine: str) -> None:
    with pytest.raises(click.ClickException, match=f"{system.lower()} {machine}"):
        tunnel_binary.cli_asset(machine, system)


def test_macos_s_zip_archive_installs_its_code_binary(state: Path, tmp_path: Path) -> None:
    archive = tmp_path / "vscode_cli_darwin_arm64_cli.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("code", b"#!/bin/sh\nexit 0\n")
    installed = tunnel_binary.install_cli(tarball=archive)
    assert installed.read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert installed.stat().st_mode & 0o111
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zipped:
        zipped.writestr("README", b"")
    with pytest.raises(click.ClickException, match="holds no 'code' binary"):
        tunnel_binary.install_cli(tarball=empty, force=True)


@pytest.mark.parametrize(
    "body, mode", [(b"\x7fELF\x02 another machine's build", 0o755), (b"#!/bin/sh\nexit 0\n", 0o644)]
)
def test_a_cli_the_system_will_not_run_is_refused_in_words(state: Path, body: bytes, mode: int) -> None:
    """A CLI left by another machine's build, or one that lost its execute bit: `status` names the file and what to do,
    never a traceback."""
    tunnel_binary.cli_path().parent.mkdir(parents=True, exist_ok=True)
    tunnel_binary.cli_path().write_bytes(body)
    tunnel_binary.cli_path().chmod(mode)
    with pytest.raises(AislandError, match=r"The VS Code CLI at .* cannot run here .*Remove it and run"):
        tunnel.signed_in()


# --- 5. where state lives ---------------------------------------------------------------------


def test_the_default_state_folder_is_per_machine_not_per_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deriving it from the working directory made `start` here and `status` there disagree."""
    monkeypatch.delenv(tunnel_state.TUNNEL_HOME_ENV, raising=False)
    monkeypatch.setattr(tunnel_state.Path, "home", classmethod(lambda cls: Path("/home/someone")))

    assert tunnel_state.state_dir() == Path("/home/someone/.aisland/tunnel")

    monkeypatch.setenv(tunnel_state.TUNNEL_HOME_ENV, "/mnt/persistent/tunnel")
    assert tunnel_state.state_dir() == Path("/mnt/persistent/tunnel")


def test_every_path_is_in_the_one_state_folder(state: Path, tmp_path: Path) -> None:
    paths = [
        tunnel_binary.cli_path(),
        tunnel.log_path(),
        tunnel.pid_path(),
        tunnel.provider_path(),
        tunnel_store.store_path(),
    ]
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
    # alive when stop looks, gone once it has been asked to stop
    with patch.object(tunnel, "_is_tunnel", side_effect=[True, False]), patch.object(tunnel.os, "kill") as kill:
        payload = tunnel.stop()

    assert payload == {"running": False, "stopped": True, "pid": 4711}
    assert kill.call_args_list == [call(4711, signal.SIGTERM)]
    assert not tunnel.pid_path().exists()


# --- 9. the tunnel runs where there is no workspace -------------------------------------------


def test_the_group_runs_outside_a_workspace_while_other_commands_are_refused(
    monkeypatch: pytest.MonkeyPatch, state: Path
) -> None:
    monkeypatch.setattr(locations, "workspace_found", lambda: False)
    runner = CliRunner()

    allowed = runner.invoke(main, ["tunnel", "status"])
    refused = runner.invoke(main, ["services", "status"])

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


def test_a_tunnel_another_start_spawned_meanwhile_is_never_spawned_twice(state: Path) -> None:
    """The check before the sign-in finds none; by the time this start holds the lock, another one has spawned it."""
    popen = _spawn()
    answers = iter([False, True])
    with (
        patch.object(tunnel_binary, "install_cli", return_value=state / "code"),
        patch.object(tunnel.subprocess, "Popen", popen),
        patch.object(tunnel, "running", lambda: next(answers)),
        patch.object(tunnel, "signed_in", return_value="user@example.invalid"),
    ):
        payload = tunnel.start(timeout=0)

    assert popen.calls == []
    assert payload["already_running"] is True


def test_two_starts_at_once_spawn_one_tunnel(state: Path, tmp_path: Path) -> None:
    """Two processes run `start` together against a CLI that takes a moment to come up: one tunnel, one pidfile."""
    cli = tmp_path / "code"
    cli.write_text("#!/bin/sh\nsleep 5\n")
    cli.chmod(0o755)
    script = (
        "import sys, time; from pathlib import Path; from unittest.mock import patch\n"
        "from aisland.tunnel import binary, host\n"
        "spawn = host.subprocess.Popen\n"
        "def slow(*args, **kwargs):\n"  # the window between the check and the pidfile, wide enough to land in
        "    time.sleep(0.5)\n"
        "    return spawn(*args, **kwargs)\n"
        "with patch.object(host.subprocess, 'Popen', slow), "
        "patch.object(binary, 'install_cli', return_value=Path(sys.argv[1])), "
        "patch.object(host, 'signed_in', return_value='user@example.invalid'), "
        "patch.object(host, '_is_tunnel', lambda pid: True):\n"
        "    print(host.start(timeout=0).get('already_running', False))\n"
    )
    environment = {**os.environ, tunnel_state.TUNNEL_HOME_ENV: str(state)}
    runs = [
        subprocess.Popen([sys.executable, "-c", script, str(cli)], env=environment, stdout=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    answers = sorted(run.communicate(timeout=60)[0].strip() for run in runs)
    try:
        assert answers == ["False", "True"]
    finally:
        pid = tunnel.read_pid()
        if pid:
            os.kill(pid, signal.SIGKILL)
