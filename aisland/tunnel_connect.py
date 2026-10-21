"""A terminal on a remote machine through the VS Code tunnel it already runs (``aisland tunnel connect``).

``aisland tunnel start`` puts a VS Code Remote Tunnel on a box that has no inbound port and no sshd.
That tunnel is enough for a terminal too, without the editor, because of two things the VS Code CLI
does (read in its source, ``cli/src/tunnels/control_server.rs``, and measured on a remote box):

* the tunnel publishes a **control port** (31545) whose ``spawn`` request starts any program on the
  machine and streams its stdin, stdout and stderr back — the way the editor runs commands there;
* a connection that arrives **through the tunnel** needs no handshake of its own
  (``AuthRequired::None``): the relay already admitted only the account that owns the tunnel.

So this side needs a way through the relay to that port, and a client of its messages:

* **The way through is Microsoft's ``devtunnel connect``**, on THIS machine only: signed in with the
  account the box's tunnel runs under, it sees that tunnel (labelled ``vscode-server-launcher``) and
  forwards its ports to ``localhost`` — on the SAME numbers, it has no option to choose. Nothing is
  added on the box and no second tunnel exists.
* **The client is ours**, MessagePack over that port (``msgpack``, the ``[connect]`` extra — only this
  side needs it): ``spawn`` the helper :mod:`aisland.tunnel_shell` from its own source, then relay.
  ``spawn`` gives a program pipes, not a terminal, so the helper opens one there and a frame on its
  stdin carries either keystrokes or a window size.

Three refusals follow from the fixed port: a port already taken (a forward started by hand) reaches
an unknown machine, so ``connect`` refuses rather than guess which; and the forward it starts is
always stopped when the shell ends, so the next ``connect`` finds the port free.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import platform
import select
import shutil
import signal
import socket
import subprocess
import termios
import threading
import time
import tty
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import click

from aisland import tunnel_shell
from aisland.tunnel import state_dir

#: The VS Code tunnel's control port (the CLI's ``CONTROL_PORT``); ``devtunnel connect`` forwards it
#: to the same number here.
CONTROL_PORT = 31545

#: The label the VS Code CLI gives every tunnel it hosts; the others on the account are not ours.
VSCODE_LABEL = "vscode-server-launcher"

#: Labels the CLI adds for itself (``protocolv6``, ``_flag4``); what is left is the tunnel's name.
_SERVICE_LABEL_PREFIXES = ("protocolv", "_flag")

#: How long ``devtunnel connect`` may take to open the forwarded port.
FORWARD_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.1

#: How long ``devtunnel user show`` / ``list`` may take: both ask the service over the network.
_RUN_TIMEOUT_S = 60.0

#: How long a forward gets to stop on its own before it is killed.
_STOP_GRACE_S = 5.0

#: The spawn request's id; the session sends no other request.
_SPAWN_ID = 1

#: Starts the helper with whichever Python the remote machine has; the helper's source is ``$0``.
_LAUNCHER = (
    'for p in python3 python; do command -v "$p" >/dev/null 2>&1 && exec "$p" -c "$0" "$@"; done; '
    'echo "aisland: no python3 on the remote machine" >&2; exit 127'
)


@dataclass(frozen=True)
class Tunnel:
    """One VS Code tunnel on the account: its name, its id, and whether its machine is hosting it."""

    tunnel_id: str
    name: str
    online: bool


# --------------------------------------------------------------------------------------------------
# devtunnel
# --------------------------------------------------------------------------------------------------


def install_hint() -> str:
    """How to install ``devtunnel`` on this system."""
    if platform.system() == "Darwin":
        return "brew install --cask devtunnel"
    return "curl -sL https://aka.ms/DevTunnelCliInstall | bash"


def devtunnel() -> str:
    """The ``devtunnel`` program, or a refusal saying how to install it."""
    found = shutil.which("devtunnel")
    if found is None:
        raise click.ClickException(f"No 'devtunnel' command found. Install it with:\n    {install_hint()}")
    return found


def _run(*args: str) -> str:
    """``devtunnel <args>``'s standard output; a refusal quoting it when it fails or hangs."""
    try:
        completed = subprocess.run([devtunnel(), *args], capture_output=True, text=True, timeout=_RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException(f"`devtunnel {' '.join(args)}` gave no answer within {_RUN_TIMEOUT_S:g} s") from exc
    if completed.returncode != 0:
        said = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise click.ClickException(f"`devtunnel {' '.join(args)}` failed: {said[-1] if said else completed.returncode}")
    return completed.stdout


def account() -> Optional[str]:
    """``Logged in as … using …`` when ``devtunnel`` is signed in, else None."""
    for line in _run("user", "show").splitlines():
        if line.startswith("Logged in as"):
            return line.strip()
    return None


def _require_account() -> None:
    if account() is None:
        raise click.ClickException(
            "devtunnel is not signed in. Sign in with the account the remote tunnel runs under:\n"
            "    devtunnel user login        (Microsoft)\n"
            "    devtunnel user login -g     (GitHub)"
        )


def _name(labels: List[str]) -> str:
    for label in labels:
        if label != VSCODE_LABEL and not label.startswith(_SERVICE_LABEL_PREFIXES):
            return str(label)
    return ""


def list_tunnels() -> List[Tunnel]:
    """The account's VS Code tunnels, as ``devtunnel list`` reports them; remembered for TAB."""
    _require_account()
    text = _run("list", "-j")
    try:
        payload = json.loads(text[text.index("{") :])
    except ValueError as exc:
        raise click.ClickException(f"`devtunnel list -j` printed no tunnel list: {text.strip()[:200]}") from exc
    tunnels = [
        Tunnel(
            tunnel_id=str(entry["tunnelId"]),
            name=_name(entry.get("labels") or []) or str(entry["tunnelId"]),
            online=int(entry.get("hostConnections") or 0) > 0,
        )
        for entry in payload.get("tunnels", [])
        if VSCODE_LABEL in (entry.get("labels") or [])
    ]
    _save_cache(tunnels)
    return tunnels


def render_tunnels(tunnels: List[Tunnel]) -> str:
    """The tunnels as a table, and how to connect to one."""
    if not tunnels:
        return (
            "No VS Code tunnel on this account. Start one on the remote machine:\n"
            "    aisland tunnel start --provider microsoft"
        )
    name_width = max(len("NAME"), *(len(t.name) for t in tunnels))
    id_width = max(len("TUNNEL ID"), *(len(t.tunnel_id) for t in tunnels))
    lines = [f"{'NAME':<{name_width}}  {'TUNNEL ID':<{id_width}}  MACHINE"]
    for entry in tunnels:
        state = "online" if entry.online else "offline"
        lines.append(f"{entry.name:<{name_width}}  {entry.tunnel_id:<{id_width}}  {state}")
    lines.append("Connect with: aisland tunnel connect <name>   (TAB completes)")
    return "\n".join(lines)


def tunnels_payload(tunnels: List[Tunnel]) -> List[Dict[str, Any]]:
    """The tunnels as JSON-serializable records."""
    return [asdict(entry) for entry in tunnels]


def resolve(target: str, tunnels: List[Tunnel]) -> Tunnel:
    """The running tunnel ``target`` names — by name or by id — or a refusal saying why there is none."""
    matches = [t for t in tunnels if target in (t.name, t.tunnel_id)]
    if not matches:
        known = ", ".join(sorted({t.name for t in tunnels})) or "none"
        raise click.ClickException(
            f"No tunnel '{target}' on this account. Its tunnels: {known} (`aisland tunnel connect` lists them)."
        )
    running = [t for t in matches if t.online]
    if not running:
        raise click.ClickException(
            f"Tunnel '{target}' is not running on its machine. Start it there: aisland tunnel start"
        )
    if len(running) > 1:
        ids = ", ".join(t.tunnel_id for t in running)
        raise click.ClickException(f"Several running tunnels are named '{target}'; name one by its id: {ids}")
    return running[0]


# --------------------------------------------------------------------------------------------------
# The names TAB offers
# --------------------------------------------------------------------------------------------------


def cache_path() -> Path:
    """The last list of tunnels, kept so TAB answers without the network."""
    return state_dir() / "devtunnels.json"


def _save_cache(tunnels: List[Tunnel]) -> None:
    try:
        cache_path().parent.mkdir(parents=True, exist_ok=True)
        cache_path().write_text(json.dumps(tunnels_payload(tunnels), indent=2) + "\n")
    except OSError:  # pragma: no cover - a cache that cannot be written only costs TAB its answer
        pass


def complete_tunnel(ctx: Any, param: Any, incomplete: str) -> List[Any]:
    """Shell completion for ``connect``: the names of the last list, with id and state as help.

    Like :func:`aisland.tunnel.complete_workspace` it answers from a file only — no network on a TAB —
    and an unreadable file offers nothing rather than breaking the shell's TAB.
    """
    try:
        from click.shell_completion import CompletionItem

        items = []
        for entry in json.loads(cache_path().read_text()):
            if str(entry["name"]).startswith(incomplete):
                state = "online" if entry["online"] else "offline"
                items.append(CompletionItem(entry["name"], help=f"{entry['tunnel_id']} — {state}"))
        return items
    except Exception:  # a completion callback must never break the shell
        return []


# --------------------------------------------------------------------------------------------------
# The forward
# --------------------------------------------------------------------------------------------------


def log_path() -> Path:
    """What the last ``devtunnel connect`` printed."""
    return state_dir() / "connect.log"


def used_here() -> bool:
    """Whether ``connect`` has been used on this machine: its list or its forward left a file."""
    return cache_path().exists() or log_path().exists()


def port_holder(port: int) -> Optional[str]:
    """Who listens on ``port`` here — ``pid 123 (devtunnel)`` — or None when nobody does."""
    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) != 0:
            return None
    lsof = shutil.which("lsof")
    if lsof is not None:
        listed = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True
        ).stdout
        fields = {line[0]: line[1:] for line in listed.splitlines() if line}
        if "p" in fields:
            return f"pid {fields['p']} ({fields.get('c', '?')})"
    return "a process lsof could not name"


def _port_answers(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _tail(path: Path, lines: int = 5) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").strip().splitlines()[-lines:])
    except OSError:
        return ""


@contextlib.contextmanager
def forward(target: Tunnel, timeout: float = FORWARD_TIMEOUT_S) -> Iterator[None]:
    """Run ``devtunnel connect`` for ``target`` while the block runs, and stop it after.

    Refuses when the control port is already taken: ``devtunnel connect`` always forwards to the
    same number, so a listener there may reach another machine and nothing says which.
    """
    port = CONTROL_PORT
    holder = port_holder(port)
    if holder is not None:
        raise click.ClickException(
            f"Port {port} on this machine is already taken by {holder} — a `devtunnel connect` started "
            "by hand forwards that same port, to a machine aisland cannot tell. Stop it and run this again."
        )
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as handle:
        process = subprocess.Popen(
            [devtunnel(), "connect", target.tunnel_id],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + timeout
        while not _port_answers(port):
            if process.poll() is not None:
                raise click.ClickException(
                    f"`devtunnel connect {target.tunnel_id}` stopped (exit {process.returncode}):\n{_tail(log)}"
                )
            if time.monotonic() >= deadline:
                raise click.ClickException(
                    f"`devtunnel connect {target.tunnel_id}` opened no port {port} within {timeout:g} s:\n{_tail(log)}"
                )
            time.sleep(_POLL_INTERVAL_S)
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_STOP_GRACE_S)
            except subprocess.TimeoutExpired:  # pragma: no cover - devtunnel ignored SIGTERM
                process.kill()
                process.wait()


# --------------------------------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------------------------------


def _msgpack() -> ModuleType:
    """The MessagePack library, or a refusal naming the extra that installs it."""
    try:
        return importlib.import_module("msgpack")  # untyped; import_module gives it a module's type
    except ImportError as exc:
        raise click.ClickException("`aisland tunnel connect` needs msgpack: pip install 'aisland[connect]'") from exc


def remote_command(rows: int, cols: int, folder: Optional[str]) -> Tuple[str, List[str]]:
    """The program the remote machine spawns: the helper, from its own source, under its Python."""
    source = Path(tunnel_shell.__file__).read_text()
    return "sh", ["-c", _LAUNCHER, source, str(rows), str(cols), folder or ""]


def _write_all(fd: int, data: bytes) -> None:
    while data:
        written = os.write(fd, data)
        data = data[written:]


def run_shell(
    sock: socket.socket,
    *,
    rows: int,
    cols: int,
    folder: Optional[str],
    term: str,
    stdin: int,
    stdout: int,
    stderr: int,
    wake: Optional[int] = None,
    size: Optional[Callable[[], Tuple[int, int]]] = None,
    idle_timeout: Optional[float] = None,
) -> int:
    """Run the remote shell over a connection to the control port; its exit status.

    Args:
        sock: A connection to the forwarded control port.
        rows: The terminal's height to start with.
        cols: Its width.
        folder: Where on the remote machine the shell starts; None for the home folder there.
        term: ``TERM`` for the remote terminal.
        stdin: What is typed (raw mode is the caller's).
        stdout: Where the terminal's output goes.
        stderr: Where the helper's own messages go.
        wake: Readable whenever the window was resized; ``size`` then gives the new size.
        size: The window's ``(rows, cols)`` now.
        idle_timeout: Fail when nothing happens for this long; None waits as long as the person
            does — a terminal is quiet whenever they are.
    """
    msgpack = _msgpack()
    unpacker = msgpack.Unpacker(raw=False)

    def send(message: Dict[str, Any]) -> None:
        sock.sendall(msgpack.packb(message, use_bin_type=True))

    command, args = remote_command(rows, cols, folder)
    send({"id": _SPAWN_ID, "method": "spawn", "params": {"command": command, "args": args, "env": {"TERM": term}}})

    streams: Optional[List[int]] = None
    pending: List[bytes] = []
    input_open = True
    input_ended = False

    def to_box(frame: bytes) -> None:
        if streams is None:
            pending.append(frame)
        else:
            send({"id": None, "method": "stream_data", "params": {"stream": streams[0], "segment": frame}})

    def end_input() -> None:
        if streams is not None:
            send({"id": None, "method": "stream_ended", "params": {"stream": streams[0]}})

    while True:
        watched = [sock] + ([stdin] if input_open else []) + ([wake] if wake is not None else [])
        ready, _, _ = select.select(watched, [], [], idle_timeout)
        if not ready:
            raise click.ClickException(f"The remote machine sent nothing for {idle_timeout:g} s.")
        if wake is not None and wake in ready:
            os.read(wake, 4096)
            if size is not None:
                to_box(tunnel_shell.encode_resize(*size()))
        if sock in ready:
            data = sock.recv(65536)
            if not data:
                raise click.ClickException("The tunnel closed the connection before the shell ended.")
            unpacker.feed(data)
            for message in unpacker:
                method = message.get("method")
                params = message.get("params") or {}
                if method == "streams_started" and params.get("for_request_id") == _SPAWN_ID:
                    streams = list(params["stream_ids"])
                    for frame in pending:
                        to_box(frame)
                    pending.clear()
                    if input_ended:
                        end_input()
                elif method == "stream_data":
                    target = stderr if streams is not None and params["stream"] == streams[2] else stdout
                    _write_all(target, params["segment"])
                elif method is None and message.get("id") == _SPAWN_ID:
                    if message.get("error"):
                        reason = message["error"].get("message", message["error"])
                        raise click.ClickException(f"The remote machine refused to start the shell: {reason}")
                    return int(message["result"]["exit_code"])
        if input_open and stdin in ready:
            typed = os.read(stdin, 65536)
            if typed:
                to_box(tunnel_shell.encode_data(typed))
            else:
                input_open = False
                input_ended = True
                end_input()


def _window_size(fd: int) -> Tuple[int, int]:
    try:
        size = os.get_terminal_size(fd)
    except OSError:
        size = shutil.get_terminal_size()
    return size.lines, size.columns


@contextlib.contextmanager
def _raw(fd: int) -> Iterator[None]:
    """The terminal on ``fd`` in raw mode — every key goes to the remote shell — and put back after."""
    if not os.isatty(fd):
        yield
        return
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd, termios.TCSADRAIN)  # its default, TCSAFLUSH, throws away what was typed ahead
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


@contextlib.contextmanager
def _resizes() -> Iterator[Optional[int]]:
    """A descriptor that turns readable on every window resize (SIGWINCH); None off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield None
        return
    read_end, write_end = os.pipe()
    os.set_blocking(write_end, False)

    def handler(signum: int, frame: Any) -> None:
        with contextlib.suppress(BlockingIOError):
            os.write(write_end, b"r")

    previous = signal.signal(signal.SIGWINCH, handler)
    try:
        yield read_end
    finally:
        signal.signal(signal.SIGWINCH, previous)
        os.close(read_end)
        os.close(write_end)


def connect(
    target: str,
    folder: Optional[str] = None,
    stdin: int = 0,
    stdout: int = 1,
    stderr: int = 2,
    idle_timeout: Optional[float] = None,
) -> int:
    """Open a shell on the machine whose tunnel ``target`` names; the shell's exit status.

    Args:
        target: The tunnel's name (as ``aisland tunnel start`` gave it) or its id.
        folder: Where the shell starts on the remote machine; None for the home folder there.
        stdin: The local terminal's input.
        stdout: Where the remote terminal's output goes.
        stderr: Where the remote helper's own messages go.
        idle_timeout: See :func:`run_shell`.
    """
    _msgpack()
    chosen = resolve(target, list_tunnels())
    term = os.environ.get("TERM") or "xterm-256color"
    rows, cols = _window_size(stdin)
    with forward(chosen):
        with socket.create_connection(("127.0.0.1", CONTROL_PORT)) as sock, _resizes() as wake, _raw(stdin):
            return run_shell(
                sock,
                rows=rows,
                cols=cols,
                folder=folder,
                term=term,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                wake=wake,
                size=lambda: _window_size(stdin),
                idle_timeout=idle_timeout,
            )
