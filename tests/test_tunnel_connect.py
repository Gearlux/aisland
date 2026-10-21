"""`aisland tunnel connect` — a terminal on a remote machine over the VS Code tunnel it runs.

What must hold, and why:

- the list is the account's **VS Code tunnels** only, named by the tunnel's own name (its label), so
  ``connect <TAB>`` offers what ``aisland tunnel start`` called each machine;
- every way it cannot work is **one line naming the fix**: no ``devtunnel``, not signed in, an
  unknown name, a machine whose tunnel is not running, the local control port already taken (a
  forward started by hand reaches an unknown machine), the forward failing, msgpack missing;
- a shell runs in a **real terminal** on the far side — its output and exit status come back, and a
  window resize reaches it;
- nothing outlives the session: the ``devtunnel connect`` it started is stopped and the local
  terminal is put back as it was.

The remote side is a fake ``devtunnel`` (``tests/fake_devtunnel.py``) whose control port speaks the
real tunnel's messages and runs the helper on this machine, in a real pseudo-terminal. No test
reaches the network.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import click
import pytest
from click.testing import CliRunner

from aisland import tunnel, tunnel_connect, tunnel_shell
from aisland.cli import main

FAKE = Path(__file__).with_name("fake_devtunnel.py")

#: The longest any one session in this file may stay silent before it fails instead of hanging.
DEADLINE_S = 30.0


def _tunnel(tunnel_id: str, name: str, hosts: int = 1, vscode: bool = True) -> Dict[str, Any]:
    """One entry of ``devtunnel list -j``, shaped as the real command prints it."""
    labels = [name, "protocolv6", "_flag4"] + (["vscode-server-launcher"] if vscode else [])
    return {"tunnelId": tunnel_id, "name": "", "hostConnections": hosts, "labels": labels, "portCount": 0}


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the tunnel's state at a scratch folder, so no test touches this machine's own."""
    monkeypatch.setenv(tunnel.TUNNEL_HOME_ENV, str(tmp_path / "tunnel"))
    return tmp_path / "tunnel"


@pytest.fixture
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, free_port: int) -> Path:
    """A signed-in ``devtunnel`` on PATH with one running and one stopped VS Code tunnel."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "devtunnel"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    script.chmod(0o755)
    fake_state = tmp_path / "fake"
    fake_state.mkdir()
    (fake_state / "account").write_text("someone@example.com")
    home = tmp_path / "home"
    home.mkdir()
    tunnels = [
        _tunnel("swift-dog-1.use", "box-one"),
        _tunnel("jolly-plane-2.use", "box-two", hosts=0),
        _tunnel("other-3.use", "not-vscode", vscode=False),
    ]
    (fake_state / "tunnels.json").write_text(json.dumps({"tunnels": tunnels}))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DT_STATE", str(fake_state))
    monkeypatch.setenv("FAKE_DT_HOME", str(home))
    monkeypatch.setenv("FAKE_DT_PORT", str(free_port))
    monkeypatch.setattr(tunnel_connect, "CONTROL_PORT", free_port)
    return fake_state


def _set_tunnels(fake_state: Path, tunnels: List[Dict[str, Any]]) -> None:
    (fake_state / "tunnels.json").write_text(json.dumps({"tunnels": tunnels}))


@pytest.fixture
def output(tmp_path: Path) -> Iterator[Any]:
    """Two files standing in for the local terminal's stdout and stderr."""
    out = open(tmp_path / "stdout", "wb+")
    err = open(tmp_path / "stderr", "wb+")
    yield out, err
    out.close()
    err.close()


def _typed(text: str) -> int:
    """A pipe holding ``text`` as if it were typed, left open as a terminal's input stays open."""
    read_end, write_end = os.pipe()
    os.write(write_end, text.encode())
    _typed.open_ends.append(write_end)  # type: ignore[attr-defined]
    return read_end


_typed.open_ends = []  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _close_typed() -> Iterator[None]:
    yield
    while _typed.open_ends:  # type: ignore[attr-defined]
        os.close(_typed.open_ends.pop())  # type: ignore[attr-defined]


def _text(handle: Any) -> str:
    handle.flush()
    handle.seek(0)
    return str(handle.read().decode(errors="replace"))


# --- 1. the list ----------------------------------------------------------------------------------


def test_the_list_is_the_accounts_vscode_tunnels_by_name(fake: Path) -> None:
    tunnels = tunnel_connect.list_tunnels()
    assert [(t.name, t.tunnel_id, t.online) for t in tunnels] == [
        ("box-one", "swift-dog-1.use", True),
        ("box-two", "jolly-plane-2.use", False),
    ]


def test_connect_without_a_name_shows_the_list(fake: Path) -> None:
    result = CliRunner().invoke(main, ["tunnel", "connect"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == ["NAME", "TUNNEL", "ID", "MACHINE"]
    assert lines[1].split() == ["box-one", "swift-dog-1.use", "online"]
    assert lines[2].split() == ["box-two", "jolly-plane-2.use", "offline"]
    assert "aisland tunnel connect <name>" in result.output
    assert "not-vscode" not in result.output


def test_the_list_as_json(fake: Path) -> None:
    result = CliRunner().invoke(main, ["tunnel", "connect", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {"name": "box-one", "tunnel_id": "swift-dog-1.use", "online": True},
        {"name": "box-two", "tunnel_id": "jolly-plane-2.use", "online": False},
    ]


def test_json_with_a_name_is_refused(fake: Path) -> None:
    result = CliRunner().invoke(main, ["tunnel", "connect", "box-one", "--json"])
    assert result.exit_code == 2
    assert "--json lists the tunnels" in result.output


def test_an_account_without_vscode_tunnels_says_where_one_comes_from(fake: Path) -> None:
    _set_tunnels(fake, [_tunnel("other-3.use", "not-vscode", vscode=False)])
    result = CliRunner().invoke(main, ["tunnel", "connect"])
    assert result.exit_code == 0
    assert "No VS Code tunnel on this account" in result.output
    assert "aisland tunnel start" in result.output


def test_tab_offers_the_names_of_the_last_list(fake: Path) -> None:
    tunnel_connect.list_tunnels()
    offered = tunnel_connect.complete_tunnel(None, None, "box")
    assert [(item.value, item.help) for item in offered] == [
        ("box-one", "swift-dog-1.use — online"),
        ("box-two", "jolly-plane-2.use — offline"),
    ]
    assert [item.value for item in tunnel_connect.complete_tunnel(None, None, "box-t")] == ["box-two"]


def test_tab_with_an_unreadable_list_offers_nothing(state: Path) -> None:
    state.mkdir(parents=True)
    tunnel_connect.cache_path().write_text("{not json")
    assert tunnel_connect.complete_tunnel(None, None, "") == []


# --- 2. refusals ----------------------------------------------------------------------------------


def test_without_devtunnel_the_error_names_the_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    result = CliRunner().invoke(main, ["tunnel", "connect"])
    assert result.exit_code == 1
    assert result.output.startswith("Error: No 'devtunnel' command found.")
    assert tunnel_connect.install_hint() in result.output


@pytest.mark.parametrize("system, hint", [("Darwin", "brew install --cask devtunnel"), ("Linux", "aka.ms")])
def test_the_install_hint_follows_the_system(monkeypatch: pytest.MonkeyPatch, system: str, hint: str) -> None:
    monkeypatch.setattr(tunnel_connect.platform, "system", lambda: system)
    assert hint in tunnel_connect.install_hint()


def test_not_signed_in_names_the_sign_in(fake: Path) -> None:
    (fake / "account").unlink()
    result = CliRunner().invoke(main, ["tunnel", "connect"])
    assert result.exit_code == 1
    assert "devtunnel is not signed in" in result.output
    assert "devtunnel user login" in result.output
    assert "devtunnel user login -g" in result.output


def test_an_unknown_name_names_the_known_ones(fake: Path) -> None:
    with pytest.raises(click.ClickException, match="No tunnel 'box-nine'.*box-one, box-two"):
        tunnel_connect.connect("box-nine")


def test_a_machine_whose_tunnel_is_not_running_is_refused(fake: Path) -> None:
    with pytest.raises(click.ClickException, match="'box-two' is not running on its machine"):
        tunnel_connect.connect("box-two")


def test_a_tunnel_id_is_accepted_like_a_name(fake: Path) -> None:
    tunnels = tunnel_connect.list_tunnels()
    assert tunnel_connect.resolve("swift-dog-1.use", tunnels).name == "box-one"


def test_of_two_tunnels_with_one_name_the_running_one_is_taken(fake: Path) -> None:
    tunnels = [
        tunnel_connect.Tunnel("old-1.use", "box-one", online=False),
        tunnel_connect.Tunnel("new-2.use", "box-one", online=True),
    ]
    assert tunnel_connect.resolve("box-one", tunnels).tunnel_id == "new-2.use"


def test_two_running_tunnels_with_one_name_ask_for_the_id() -> None:
    tunnels = [
        tunnel_connect.Tunnel("a-1.use", "box-one", online=True),
        tunnel_connect.Tunnel("b-2.use", "box-one", online=True),
    ]
    with pytest.raises(click.ClickException, match="a-1.use, b-2.use"):
        tunnel_connect.resolve("box-one", tunnels)


def test_a_taken_control_port_is_refused_naming_its_process(fake: Path, free_port: int) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", free_port))
        holder.listen()
        with pytest.raises(click.ClickException) as raised:
            tunnel_connect.connect("box-one")
    message = raised.value.message
    assert f"Port {free_port} on this machine is already taken" in message
    if tunnel_connect.shutil.which("lsof"):
        assert f"pid {os.getpid()}" in message
    assert not (fake / "connect.pid").exists(), "no forward may start while the port is taken"


def test_a_forward_that_fails_reports_what_devtunnel_said(fake: Path) -> None:
    (fake / "connect.fail").write_text("Tunnel not found.")
    with pytest.raises(click.ClickException, match="(?s)devtunnel connect swift-dog-1.use.*Tunnel not found"):
        tunnel_connect.connect("box-one")


def test_without_msgpack_the_error_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "msgpack", None)
    with pytest.raises(click.ClickException, match=r"pip install 'aisland\[connect\]'"):
        tunnel_connect.connect("box-one")


# --- 3. the session -------------------------------------------------------------------------------


def _connect(target: str, typed: str, output: Any, folder: Optional[str] = None, stdin: Optional[int] = None) -> int:
    out, err = output
    return tunnel_connect.connect(
        target,
        folder=folder,
        stdin=_typed(typed) if stdin is None else stdin,
        stdout=out.fileno(),
        stderr=err.fileno(),
        idle_timeout=DEADLINE_S,
    )


def test_the_shell_runs_in_a_terminal_and_its_status_comes_back(fake: Path, output: Any) -> None:
    code = _connect("box-one", "tty; echo from-the-box; exit 7\n", output)
    assert code == 7
    text = _text(output[0])
    assert "from-the-box" in text
    assert "not a tty" not in text
    assert (fake / "connect.args").read_text() == "swift-dog-1.use"


def test_the_shell_reads_its_interactive_start_up_file_and_is_no_login_shell(
    fake: Path, output: Any, tmp_path: Path
) -> None:
    """As an editor's terminal starts it: ``~/.bashrc`` (or ``~/.zshrc``) runs — the colour prompt and
    ``ls --color`` live there — whether or not the machine's ``~/.profile`` loads it."""
    home = tmp_path / "home"
    for rc in (".bashrc", ".zshrc"):
        (home / rc).write_text("export FROM_RC=read\n")
    (home / ".profile").write_text("export FROM_PROFILE=read\n")
    _connect("box-one", 'echo "zero=$0 rc=$FROM_RC profile=$FROM_PROFILE"; exit\n', output)
    line = next(text for text in _text(output[0]).splitlines() if text.startswith("zero="))
    assert "rc=read" in line
    assert "profile=read" not in line
    assert not line.startswith("zero=-"), line  # a login shell's $0 starts with a dash


def test_the_shell_starts_in_the_home_folder(fake: Path, output: Any) -> None:
    _, home = tunnel_shell.account_shell()
    _connect("box-one", "pwd; exit\n", output)
    assert os.path.realpath(home) in _text(output[0])


def test_cwd_starts_the_shell_in_that_folder(fake: Path, output: Any, tmp_path: Path) -> None:
    folder = tmp_path / "work"
    folder.mkdir()
    _connect("box-one", "pwd; exit\n", output, folder=str(folder))
    assert os.path.realpath(folder) in _text(output[0])


def test_a_missing_cwd_is_reported_by_the_remote_machine(fake: Path, output: Any, tmp_path: Path) -> None:
    code = _connect("box-one", "", output, folder=str(tmp_path / "absent"))
    assert code == tunnel_shell.EXIT_NO_FOLDER
    assert f"no such folder on the remote machine: {tmp_path / 'absent'}" in _text(output[1])


def test_a_refused_spawn_is_one_error(fake: Path, output: Any) -> None:
    (fake / "spawn.error").write_text("ProcessSpawnFailed")
    with pytest.raises(click.ClickException, match="refused to start the shell: ProcessSpawnFailed"):
        _connect("box-one", "", output)


def test_the_forward_is_stopped_when_the_shell_ends(fake: Path, output: Any) -> None:
    _connect("box-one", "exit\n", output)
    pid = int((fake / "connect.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_window_resize_reaches_the_shell(fake: Path, output: Any) -> None:
    out, err = output
    wake_read, wake_write = os.pipe()
    tunnels = tunnel_connect.list_tunnels()
    try:
        with tunnel_connect.forward(tunnel_connect.resolve("box-one", tunnels)):
            with socket.create_connection(("127.0.0.1", tunnel_connect.CONTROL_PORT)) as sock:
                os.write(wake_write, b"x")
                code = tunnel_connect.run_shell(
                    sock,
                    rows=24,
                    cols=80,
                    folder=None,
                    term="xterm",
                    stdin=_typed("stty size; exit\n"),
                    stdout=out.fileno(),
                    stderr=err.fileno(),
                    wake=wake_read,
                    size=lambda: (33, 111),
                    idle_timeout=DEADLINE_S,
                )
    finally:
        os.close(wake_read)
        os.close(wake_write)
    assert code == 0
    assert "33 111" in _text(out)


def test_the_local_terminal_is_put_back_as_it_was(fake: Path, output: Any) -> None:
    master, slave = os.openpty()
    screen = threading.Thread(target=_drain, args=(master,), daemon=True)
    screen.start()  # a terminal emulator reads what the terminal echoes; restoring it waits for that
    try:
        before = _settings(slave)
        os.write(master, b"exit 3\n")
        code = _connect("box-one", "", output, stdin=slave)
        assert code == 3
        assert _settings(slave) == before
    finally:
        os.close(slave)
        screen.join(timeout=DEADLINE_S)
        os.close(master)


def _settings(fd: int) -> List[Any]:
    """The terminal's settings, without PENDIN: the kernel sets that one itself when input is pending
    across a mode change, so it says what was typed, not what aisland left behind."""
    attributes = termios.tcgetattr(fd)
    attributes[3] &= ~termios.PENDIN
    return attributes


def _drain(fd: int) -> None:
    with contextlib.suppress(OSError):
        while os.read(fd, 4096):
            pass


def test_the_cli_exits_with_the_shells_status(fake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, Any] = {}

    def fake_connect(target: str, folder: Optional[str] = None) -> int:
        seen.update(target=target, folder=folder)
        return 5

    monkeypatch.setattr(tunnel_connect, "connect", fake_connect)
    result = CliRunner().invoke(main, ["tunnel", "connect", "box-one", "--cwd", "/mnt/work"])
    assert result.exit_code == 5
    assert seen == {"target": "box-one", "folder": "/mnt/work"}


# --- 4. the helper on the far side ----------------------------------------------------------------


def test_frames_split_across_reads_come_out_whole() -> None:
    stream = tunnel_shell.encode_data(b"hello") + tunnel_shell.encode_resize(40, 120)
    reader = tunnel_shell.FrameReader()
    frames = []
    for index in range(len(stream)):
        frames += reader.feed(stream[index : index + 1])
    assert frames == [(tunnel_shell.FRAME_DATA, b"hello"), (tunnel_shell.FRAME_RESIZE, b"\x00\x28\x00\x78")]


def _helper(frames: bytes, *args: str, close_input: bool = False) -> subprocess.CompletedProcess:
    """Run the helper the way the remote machine does — from its own source, through the launcher."""
    command, arguments = tunnel_connect.remote_command(24, 80, args[0] if args else None)
    process = subprocess.Popen(
        [command, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": "/nonexistent-home-for-rc-files"},
    )
    assert process.stdin is not None
    process.stdin.write(frames)
    process.stdin.flush()
    if close_input:
        process.stdin.close()
    deadline = time.monotonic() + DEADLINE_S
    while process.poll() is None and time.monotonic() < deadline:
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            continue
    if process.poll() is None:
        process.kill()
        pytest.fail(f"the helper did not end within {DEADLINE_S:g} s")
    stdout = process.stdout.read() if process.stdout else b""
    stderr = process.stderr.read() if process.stderr else b""
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def test_the_helper_relays_input_and_output() -> None:
    result = _helper(tunnel_shell.encode_data(b"echo relayed; exit 4\n"))
    assert result.returncode == 4
    assert b"relayed" in result.stdout


def test_when_the_input_ends_the_shell_runs_what_was_typed_and_leaves() -> None:
    result = _helper(tunnel_shell.encode_data(b"echo before-the-end\n"), close_input=True)
    assert b"before-the-end" in result.stdout


def test_a_program_that_ignores_end_of_file_is_hung_up() -> None:
    """``cat`` in raw mode treats Ctrl-D as a byte; the helper's hang-up ends it anyway."""
    source = Path(tunnel_shell.__file__).read_text().replace("HANGUP_AFTER_S = 10.0", "HANGUP_AFTER_S = 0.5")
    process = subprocess.run(
        [sys.executable, "-I", "-c", source, "24", "80", ""],
        input=tunnel_shell.encode_data(b"stty raw; exec cat\n"),
        capture_output=True,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent-home-for-rc-files"},
        timeout=DEADLINE_S,
    )
    assert process.returncode == 128 + 1  # SIGHUP


def test_the_launcher_says_so_when_the_machine_has_no_python() -> None:
    command, arguments = tunnel_connect.remote_command(24, 80, None)
    result = subprocess.run(
        ["/bin/sh", *arguments[0:]], capture_output=True, env={"PATH": "/nonexistent"}, timeout=DEADLINE_S
    )
    assert command == "sh"
    assert result.returncode == 127
    assert b"aisland: no python3 on the remote machine" in result.stderr


def test_the_helper_runs_on_a_python_without_site_packages() -> None:
    """The remote machine's Python has no aisland and maybe nothing else: the helper needs neither."""
    source = Path(tunnel_shell.__file__).read_text()
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", source, "24", "80", ""],
        input=tunnel_shell.encode_data(b"echo bare; exit 6\n"),
        capture_output=True,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent-home-for-rc-files"},
        timeout=DEADLINE_S,
    )
    assert result.returncode == 6, result.stderr
    assert b"bare" in result.stdout
