"""A VS Code Remote Tunnel to the machine aisland runs on: the tunnel process, its sign-in, and opening an editor on it.

A remote dev box is often published through an HTTPS ingress that carries HTTP and WebSocket but
not raw TCP — a browser IDE under a path prefix, nothing else. There is no port for Remote-SSH to
attach to, and asking for one is a platform change. A Remote Tunnel needs none: the remote machine
dials *out* to the relay and the local editor attaches to that tunnel, so nothing is exposed
through the ingress and no inbound route is needed.

Three decisions worth knowing, because they are what makes this work on a bare remote box:

* **No supervisor.** Every other aisland service runs under process-compose
  (:mod:`aisland.services.supervisor`), which a remote container does not have — and the remote box is
  precisely where the least is installed. The tunnel is one detached process with a pidfile.
* **One state folder per machine, not per workspace** (:func:`aisland.tunnel.state.state_dir`):
  ``~/.aisland/tunnel/`` holds the downloaded CLI (:mod:`aisland.tunnel.binary`), the log, the pidfile,
  the account and the remembered workspaces (:mod:`aisland.tunnel.store`). ``AISLAND_TUNNEL_HOME``
  overrides it, which is what a box whose ``$HOME`` is not on a persistent mount sets, once, to keep
  its sign-in across restarts.
* **The sign-in happens before the tunnel is spawned.** ``code tunnel`` takes no account options at
  all, and without an account it asks interactively which provider to use — a question a detached
  process cannot answer. So :func:`start` signs in first (or says what to run), then spawns.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from aisland import atomic
from aisland.errors import AislandError
from aisland.tunnel import binary, store
from aisland.tunnel.state import sanitize_name, state_dir

#: The relay the tunnel dials out to; also what the doctor check probes.
RELAY_HOST = "global.rel.tunnels.api.visualstudio.com"

#: Env var a remote workspace commonly uses to name itself; the default tunnel name when set.
WORKSPACE_NAME_ENV = "WORKSPACE"

#: What ``code tunnel`` prints to its log, and what each line means.
_READY_MARKER = "vscode.dev/tunnel"
_SIGNIN_URL = re.compile(r"https://\S*(?:login/device|microsoft\.com/devicelogin)\S*")
_SIGNIN_CODE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b")

#: How long :func:`start` waits for the log to say which of the two happened.
START_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 0.2
#: Seconds the VS Code CLI may take to say which account it holds.
ANSWER_SECONDS = 30

#: How long :func:`stop` waits for the tunnel to exit after asking it to, and again after killing it.
STOP_GRACE_S = 5.0


# --------------------------------------------------------------------------------------------------
# Where things live
# --------------------------------------------------------------------------------------------------


def log_path() -> Path:
    """Where the tunnel process's own output goes."""
    return state_dir() / "tunnel.log"


def pid_path() -> Path:
    """The pidfile of the running tunnel, written by :func:`start`."""
    return state_dir() / "tunnel.pid"


def provider_path() -> Path:
    """The account provider remembered after a sign-in (per machine, so not in ``workspace.toml``)."""
    return state_dir() / "provider"


def default_name(environ: Optional[Dict[str, str]] = None) -> str:
    """The name to use when none is given: what the host calls this workspace, else the hostname."""
    env = dict(os.environ if environ is None else environ)
    return sanitize_name(env.get(WORKSPACE_NAME_ENV) or socket.gethostname() or "tunnel")


# --------------------------------------------------------------------------------------------------
# The tunnel process
# --------------------------------------------------------------------------------------------------


def read_pid() -> Optional[int]:
    """The pid in the pidfile, or None when there is no pidfile or it holds no number."""
    try:
        return int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None


def _command_line(pid: int) -> Optional[str]:
    """``pid``'s command line, or None when it cannot be read: ``/proc/<pid>/cmdline`` where there is one (Linux; a
    bare container may have no ``ps``), else ``ps -ww``."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        pass
    try:
        listed = subprocess.run(
            # -ww: BSD ps cuts a line at the terminal's width, which could cut off the CLI's path
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return listed.stdout if listed.returncode == 0 else None


def _is_tunnel(pid: int) -> bool:
    """Whether ``pid`` is a live process of this machine's VS Code CLI (:func:`aisland.tunnel.binary.cli_path`).

    After a reboot the pidfile's number may belong to any other process of this user; only its command line tells
    them apart, and a process that is not the tunnel is never signalled. A process whose command line cannot be read
    is not taken for the tunnel: an unidentified pid is never signalled either."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    command = _command_line(pid)
    return command is not None and str(binary.cli_path()) in command


def running() -> bool:
    """Whether the tunnel this machine started is still running."""
    pid = read_pid()
    return pid is not None and _is_tunnel(pid)


def read_provider() -> Optional[str]:
    """The account provider remembered on this machine, if any."""
    try:
        provider = provider_path().read_text().strip()
    except OSError:
        return None
    return provider or None


def _scan_log(text: str) -> Dict[str, object]:
    """What the tunnel's log says so far: the tunnel address, or the sign-in the user must complete."""
    found: Dict[str, object] = {}
    if _READY_MARKER in text:
        for line in text.splitlines():
            if _READY_MARKER in line:
                found["url"] = line.split()[-1].strip()
                break
    url = _SIGNIN_URL.search(text)
    code = _SIGNIN_CODE.search(text)
    if url and "url" not in found:
        found["pending_signin"] = {"url": url.group(0), "code": code.group(1) if code else ""}
    return found


def _watch_log(timeout: float) -> Dict[str, object]:
    """Poll the log until it says the tunnel is up or that a sign-in is pending, or until ``timeout``."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = _scan_log(log_path().read_text())
        except OSError:
            found = {}
        if found:
            return found
        if time.monotonic() >= deadline or not running():
            return {}
        time.sleep(_POLL_INTERVAL_S)


def signed_in() -> Optional[str]:
    """The account the CLI holds, or None when it holds none.

    ``code tunnel user show`` prints ``not logged in`` and exits **0** either way, so the answer is
    in its output, not its status — reading the status alone reports every machine as signed in. A CLI
    that has not answered after :data:`ANSWER_SECONDS` is refused naming the command: `status`, `start`
    and their MCP tools wait on this question.
    """
    program = binary.cli_path()
    if not program.exists():
        return None
    try:
        completed = subprocess.run(
            [str(program), "tunnel", "user", "show"], capture_output=True, text=True, timeout=ANSWER_SECONDS
        )
    except subprocess.TimeoutExpired as late:
        raise AislandError(
            f"`code tunnel user show` did not answer within {ANSWER_SECONDS} s ({program}); run it yourself to see why"
        ) from late
    except OSError as refused:
        raise binary.cannot_run(program, refused) from refused
    text = (completed.stdout or "").strip()
    if completed.returncode != 0 or not text or "not logged in" in text.lower():
        return None
    return text.splitlines()[0].strip()


def start(
    name: Optional[str] = None,
    cli: Optional[Path] = None,
    provider: Optional[str] = None,
    timeout: float = START_TIMEOUT_S,
    sign_in: bool = True,
) -> Dict[str, Any]:  # Any: the --json payload; its reader indexes into pending_signin
    """Bring up a tunnel to this machine and report what the log said.

    Args:
        name: Tunnel name; defaults to :func:`default_name`.
        cli: Install the VS Code CLI from this archive instead of downloading it.
        provider: ``github`` or ``microsoft`` — remembered, and used for the sign-in.
        timeout: How long to wait for the log to report the tunnel address or a pending sign-in.
        sign_in: Whether an unsigned-in machine may be signed in here, in the foreground. False
            reports what to run instead (``--json`` and the MCP tool, where nobody can answer a
            device code).

    The sign-in happens BEFORE the tunnel is spawned, not through it: ``code tunnel`` takes no
    account options at all (it has no ``--provider``; passing one makes it exit on a usage error),
    and when it has no account it asks which provider to use — a question a detached process can
    never answer.

    Returns:
        The same payload :func:`status` returns, plus ``url`` once the tunnel is up, or
        ``signin_required`` with the command to run when it has no account and may not ask.
    """
    tunnel_name = sanitize_name(name) if name else default_name()
    if running():
        return {**status(), "already_running": True}

    program = binary.install_cli(tarball=cli)
    if provider:
        state_dir().mkdir(parents=True, exist_ok=True)
        atomic.write_text(provider_path(), f"{provider}\n")

    chosen_provider = provider or read_provider()
    if signed_in() is None:
        if not sign_in:
            flag = f" --provider {chosen_provider}" if chosen_provider else ""
            return {
                **status(),
                "name": tunnel_name,
                "signin_required": True,
                "command": f"aisland tunnel login{flag}",
            }
        if login(chosen_provider) != 0:
            raise AislandError("Sign-in did not complete, so no tunnel was started.")

    command = [str(program), "tunnel", "--accept-server-license-terms", "--name", tunnel_name]

    # This machine's start lock (`start.lock` in the state folder) makes the check that no tunnel runs, the spawn and
    # the pidfile one step: two starts at once otherwise both spawn, and the pidfile keeps the last.
    with atomic.locked(state_dir() / "start"):
        # Asked again under the lock: another start (a terminal's, an MCP tool's) may have spawned one meanwhile.
        if running():
            return {**status(), "already_running": True}
        log_path().write_text("")
        with open(log_path(), "ab") as log:
            try:
                process = subprocess.Popen(
                    command, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True
                )
            except OSError as refused:
                raise binary.cannot_run(program, refused) from refused
        atomic.write_text(pid_path(), f"{process.pid}\n")

    found = _watch_log(timeout)
    return {**status(), "name": tunnel_name, **found}


def _signal(pid: int, signum: int) -> bool:
    """Send ``signum`` to ``pid``; False when the process has already gone."""
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        return False
    except OSError as exc:  # pragma: no cover - a pid of this user's that cannot be signalled
        raise AislandError(f"Could not stop the tunnel (pid {pid}): {exc}") from exc
    return True


def _gone(pid: int, timeout: float) -> bool:
    """Whether the tunnel ``pid`` has exited within ``timeout`` seconds, polling :func:`_is_tunnel`."""
    deadline = time.monotonic() + timeout
    while _is_tunnel(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_S)
    return True


def stop() -> Dict[str, object]:
    """Stop the tunnel this machine started; saying so is not an error when none runs. A pidfile naming no tunnel
    (its process gone, or the number another process's since a reboot) is removed, and nothing is signalled.

    The pidfile goes only once the process has exited — :data:`STOP_GRACE_S` after SIGTERM, then as long again after
    SIGKILL — so a ``start`` right after it never brings up a second tunnel beside one still shutting down. A process
    that outlives both keeps its pidfile and is refused naming it."""
    pid = read_pid()
    if pid is None or not _is_tunnel(pid):
        pid_path().unlink(missing_ok=True)
        return {"running": False, "stopped": False}
    if _signal(pid, signal.SIGTERM) and not _gone(pid, STOP_GRACE_S):
        if _signal(pid, signal.SIGKILL) and not _gone(pid, STOP_GRACE_S):
            raise AislandError(
                f"The tunnel (pid {pid}) is still running {2 * STOP_GRACE_S:g} s after it was stopped; "
                f"its pidfile {pid_path()} is kept."
            )
    pid_path().unlink(missing_ok=True)
    return {"running": False, "stopped": True, "pid": pid}


def status() -> Dict[str, object]:
    """What this machine's tunnel is doing, as JSON-serializable fields."""
    pid = read_pid()
    is_running = pid is not None and _is_tunnel(pid)
    payload: Dict[str, object] = {
        "running": is_running,
        "pid": pid if is_running else None,
        "name": name_in_log(),
        "account": signed_in(),
        "provider": read_provider(),
        "cli": str(binary.cli_path()) if binary.cli_path().exists() else None,
        "log": str(log_path()) if log_path().exists() else None,
        "workspaces": len(store.load_store()),
    }
    if is_running:
        payload.update({k: v for k, v in _scan_log(_read_log()).items() if k == "url"})
    return payload


def name_in_log() -> Optional[str]:
    """The name the tunnel registered under, read back from its own log.

    The name is what the editor connects to, and a machine that took its default (its workspace
    name, else its hostname) may not be named what anyone expects — so `status` has to be able to
    say it, not only whether something is running.
    """
    for line in _read_log().splitlines():
        if _READY_MARKER in line:
            tail = line.split(_READY_MARKER, 1)[1].lstrip("/")
            candidate = tail.split("/")[0].split("?")[0].strip()
            if candidate:
                return candidate
    return None


def _read_log() -> str:
    """The tunnel log's contents, empty when there is none."""
    try:
        return log_path().read_text()
    except OSError:
        return ""


def logs(lines: int = 80) -> str:
    """The last ``lines`` lines of the tunnel log."""
    return "\n".join(_read_log().splitlines()[-lines:])


def login(provider: Optional[str] = None) -> int:
    """Run the CLI's sign-in in the FOREGROUND, so the user sees the code and can complete it."""
    program = binary.install_cli()
    command = [str(program), "tunnel", "user", "login"]
    chosen = provider or read_provider()
    if chosen:
        command += ["--provider", chosen]
    try:
        completed = subprocess.run(command)
    except OSError as refused:
        raise binary.cannot_run(program, refused) from refused
    if completed.returncode == 0 and chosen:
        state_dir().mkdir(parents=True, exist_ok=True)
        atomic.write_text(provider_path(), f"{chosen}\n")
    return completed.returncode


def relay_reachable(timeout: float = 8.0) -> Tuple[bool, str]:
    """Whether the relay answers on 443 from here, and one line saying what happened.

    Any HTTP answer counts: the relay's root is not a page, so a 404 still proves the host is
    reachable. Only a connection failure means a tunnel cannot be established.
    """
    url = f"https://{RELAY_HOST}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return True, f"{RELAY_HOST} answers (HTTP {response.status})"
    except urllib.error.HTTPError as exc:
        return True, f"{RELAY_HOST} answers (HTTP {exc.code})"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{RELAY_HOST} is unreachable: {exc}"


# --------------------------------------------------------------------------------------------------
# The editor
# --------------------------------------------------------------------------------------------------


def open_editor(
    target: Optional[str] = None,
    folder: Optional[str] = None,
    name: Optional[str] = None,
    ask: bool = True,
) -> Dict[str, object]:
    """Attach the LOCAL editor to a tunnel, remembering the workspace for next time.

    Args:
        target: A workspace address, a remembered name, or None for the most recent one.
        folder: Open this path instead of the remembered one — a folder or a ``.code-workspace`` file.
        name: The tunnel name, when the address does not carry it.
        ask: Whether an ambiguous address may ask which name the tunnel has.

    Returns:
        ``{"name", "folder", "url", "command"}`` — what was opened and the command that opened it.
    """
    workspace = store.resolve_target(target, name=name, ask=ask)
    chosen_folder = folder or workspace.folder
    if chosen_folder:
        store.checked_remote_path(chosen_folder, "--path" if folder else f"The remembered workspace {workspace.name}")
    editor = shutil.which("code")
    if editor is None:
        raise AislandError(
            "No 'code' command found. Install VS Code's shell command (Command Palette → "
            "'Shell Command: Install code command in PATH'), then run this again."
        )
    command = [editor, "--remote", f"tunnel+{workspace.name}"]
    if chosen_folder:
        command.append(chosen_folder)
    store.remember(workspace.name, url=workspace.url, folder=chosen_folder)
    subprocess.run(command, check=False)
    return {"name": workspace.name, "folder": chosen_folder, "url": workspace.url, "command": command}
