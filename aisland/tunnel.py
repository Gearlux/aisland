"""A VS Code Remote Tunnel to the machine aisland runs on, and the workspaces you have opened.

A remote dev box is often published through an HTTPS ingress that carries HTTP and WebSocket but
not raw TCP — a browser IDE under a path prefix, nothing else. There is no port for Remote-SSH to
attach to, and asking for one is a platform change. A Remote Tunnel needs none: the remote machine
dials *out* to the relay and the local editor attaches to that tunnel, so nothing is exposed
through the ingress and no inbound route is needed.

Three decisions worth knowing, because they are what makes this work on a bare remote box:

* **No supervisor.** Every other aisland service runs under process-compose
  (:mod:`aisland.supervisor`), which a remote container does not have — and the remote box is
  precisely where the least is installed. The tunnel is one detached process with a pidfile.
* **One state folder per machine, not per workspace** (:func:`state_dir`): ``~/.aisland/tunnel/``
  holds the downloaded CLI, the log, the pidfile, the account and the remembered workspaces.
  Everything else aisland keeps is workspace state, found from the workspace it runs on — but a
  tunnel belongs to the machine, and deriving its folder from the working directory meant
  ``start`` in one directory and ``status`` in another disagreed about whether a tunnel existed
  (measured on a remote box, 2026-10-20). ``AISLAND_TUNNEL_HOME`` overrides it, which is what a box
  whose ``$HOME`` is not on a persistent mount sets, once, to keep its sign-in across restarts.
* **The sign-in happens before the tunnel is spawned.** ``code tunnel`` takes no account options at
  all, and without an account it asks interactively which provider to use — a question a detached
  process cannot answer. So :func:`start` signs in first (or says what to run), then spawns.

The remembered workspaces (``workspaces.json``) are a *cache*, not configuration: they exist so
``aisland tunnel open <TAB>`` offers what you used last instead of asking you to remember a name or
re-paste an address. Because that file backs shell completion, every reader treats an unreadable or
malformed file as an empty list — a completion callback that raises breaks the shell's TAB, not just
this command.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import click

from aisland.errors import AislandError

#: The relay the tunnel dials out to; also what the doctor check probes.
RELAY_HOST = "global.rel.tunnels.api.visualstudio.com"

#: Where the standalone VS Code CLI comes from. The alpine builds are statically linked, so the
#: same binary runs on a glibc image; there is no ``cli-linux-*`` identifier (it answers 404).
CLI_DOWNLOAD_URL = "https://code.visualstudio.com/sha/download?build=stable&os={asset}"
CLI_ASSETS = {
    "x86_64": "cli-alpine-x64",
    "amd64": "cli-alpine-x64",
    "aarch64": "cli-alpine-arm64",
    "arm64": "cli-alpine-arm64",
}

#: The relay's rules for a tunnel name: lowercase letters, digits and dashes, 4 to 20 characters.
NAME_MIN = 4
NAME_MAX = 20
_NAME_DISALLOWED = re.compile(r"[^a-z0-9-]+")

#: Env var a remote workspace commonly uses to name itself; the default tunnel name when set.
WORKSPACE_NAME_ENV = "WORKSPACE"

#: Names another folder for the tunnel's state (default ``~/.aisland/tunnel``): what a machine sets
#: when its ``$HOME`` is not on storage that survives a restart.
TUNNEL_HOME_ENV = "AISLAND_TUNNEL_HOME"

#: Suffixes a published address often carries that the machine's own name does not: an ingress
#: serves `<workspace>-prod` while the box inside calls itself `<workspace>`. A name derived from an
#: address is therefore ambiguous, and `resolve_target` says so instead of guessing silently.
ENV_SUFFIXES = ("-prod", "-dev", "-staging", "-test", "-qa")

#: How many workspaces are remembered, newest first.
STORE_LIMIT = 20
STORE_VERSION = 1

#: What ``code tunnel`` prints to its log, and what each line means.
_READY_MARKER = "vscode.dev/tunnel"
_SIGNIN_URL = re.compile(r"https://\S*(?:login/device|microsoft\.com/devicelogin)\S*")
_SIGNIN_CODE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b")

#: How long :func:`start` waits for the log to say which of the two happened.
START_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 0.2


# --------------------------------------------------------------------------------------------------
# Where things live
# --------------------------------------------------------------------------------------------------


def state_dir() -> Path:
    """The folder holding the CLI, the log, the pidfile, the account and the remembered workspaces.

    One folder per machine — ``~/.aisland/tunnel`` — so every command finds the same tunnel
    wherever it is run from. ``AISLAND_TUNNEL_HOME`` names another folder, for a machine whose
    ``$HOME`` does not survive a restart: point it at a persistent mount and the sign-in lasts.
    """
    named = os.environ.get(TUNNEL_HOME_ENV, "").strip()
    if named:
        return Path(named).expanduser()
    return Path.home() / ".aisland" / "tunnel"


def cli_path() -> Path:
    """The standalone VS Code CLI this feature downloads for the REMOTE side."""
    return state_dir() / "code"


def log_path() -> Path:
    """Where the tunnel process's own output goes."""
    return state_dir() / "tunnel.log"


def pid_path() -> Path:
    """The pidfile of the running tunnel, written by :func:`start`."""
    return state_dir() / "tunnel.pid"


def provider_path() -> Path:
    """The account provider remembered after a sign-in (per machine, so not in ``workspace.toml``)."""
    return state_dir() / "provider"


def store_path() -> Path:
    """The remembered workspaces that back ``open``, ``list``, ``clear`` and completion."""
    return state_dir() / "workspaces.json"


# --------------------------------------------------------------------------------------------------
# Names and architectures
# --------------------------------------------------------------------------------------------------


def sanitize_name(raw: str) -> str:
    """``raw`` as a name the relay accepts: lowercased, stripped of anything but ``a-z0-9-``, 4-20 long."""
    name = _NAME_DISALLOWED.sub("-", raw.strip().lower())[:NAME_MAX].strip("-")
    while len(name) < NAME_MIN:
        name = f"{name}0" if name else "tunnel"
    return name


def default_name(environ: Optional[Dict[str, str]] = None) -> str:
    """The name to use when none is given: what the host calls this workspace, else the hostname."""
    env = dict(os.environ if environ is None else environ)
    return sanitize_name(env.get(WORKSPACE_NAME_ENV) or socket.gethostname() or "tunnel")


def strip_env_suffix(name: str) -> str:
    """``name`` without a trailing environment suffix (``-prod``, ``-dev``, …); unchanged without one."""
    for suffix in ENV_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def cli_asset(machine: Optional[str] = None) -> str:
    """The download identifier for this architecture; a refusal naming the architecture for any other."""
    key = (machine or platform.machine()).lower()
    asset = CLI_ASSETS.get(key)
    if asset is None:
        raise AislandError(
            f"No VS Code CLI build for this architecture ({key}). Supported: " f"{', '.join(sorted(set(CLI_ASSETS)))}."
        )
    return asset


# --------------------------------------------------------------------------------------------------
# The CLI binary
# --------------------------------------------------------------------------------------------------


def _extract_cli(tarball: Path, destination: Path) -> None:
    """Write the archive's ``code`` member to ``destination``, executable.

    The single member is copied out by name rather than unpacked: an archive cannot then write
    anywhere but the one path this function chose.
    """
    with tarfile.open(tarball) as archive:
        try:
            member = archive.extractfile("code")
        except KeyError:  # pragma: no cover - a truncated or wrong archive
            member = None
        if member is None:
            raise AislandError(f"{tarball} holds no 'code' binary — is it the VS Code CLI archive?")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with member, open(destination, "wb") as out:
            shutil.copyfileobj(member, out)
    destination.chmod(0o755)


def install_cli(tarball: Optional[Path] = None, force: bool = False) -> Path:
    """Make the VS Code CLI available and return its path.

    Args:
        tarball: Install from this archive instead of downloading — for a box that cannot reach the
            download host (upload it the way an aisland wheels zip is uploaded).
        force: Re-install even when the binary is already there.
    """
    target = cli_path()
    if target.exists() and not force:
        return target

    state_dir().mkdir(parents=True, exist_ok=True)
    if tarball is not None:
        source = Path(tarball).expanduser()
        if not source.is_file():
            raise AislandError(f"No such file: {source}")
        _extract_cli(source, target)
        return target

    asset = cli_asset()
    url = CLI_DOWNLOAD_URL.format(asset=asset)
    click.echo(f"Fetching the VS Code CLI ({asset})…")
    with tempfile.TemporaryDirectory() as tmp:
        downloaded = Path(tmp) / "vscode_cli.tar.gz"
        try:
            with urllib.request.urlopen(url, timeout=180) as response, open(downloaded, "wb") as out:
                shutil.copyfileobj(response, out)
        except (urllib.error.URLError, OSError) as exc:
            raise AislandError(
                f"Could not download the VS Code CLI from {url}: {exc}\n"
                "  Fetch it on a machine that can, upload it here, and install it with:\n"
                "      aisland tunnel start --cli <the downloaded .tar.gz>"
            ) from exc
        _extract_cli(downloaded, target)
    return target


# --------------------------------------------------------------------------------------------------
# The tunnel process
# --------------------------------------------------------------------------------------------------


def read_pid() -> Optional[int]:
    """The pid in the pidfile, or None when there is no pidfile or it holds no number."""
    try:
        return int(pid_path().read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a process this user can signal."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def running() -> bool:
    """Whether the tunnel this machine started is still running."""
    pid = read_pid()
    return pid is not None and _alive(pid)


def read_provider() -> Optional[str]:
    """The account provider remembered on this machine, if any."""
    try:
        provider = provider_path().read_text().strip()
    except OSError:
        return None
    return provider or None


def _scan_log(text: str) -> Dict[str, Any]:
    """What the tunnel's log says so far: the tunnel address, or the sign-in the user must complete."""
    found: Dict[str, Any] = {}
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


def _watch_log(timeout: float) -> Dict[str, Any]:
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
    in its output, not its status — reading the status alone reports every machine as signed in.
    """
    binary = cli_path()
    if not binary.exists():
        return None
    completed = subprocess.run([str(binary), "tunnel", "user", "show"], capture_output=True, text=True)
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
) -> Dict[str, Any]:
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

    binary = install_cli(tarball=cli)
    if provider:
        state_dir().mkdir(parents=True, exist_ok=True)
        provider_path().write_text(f"{provider}\n")

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

    command = [str(binary), "tunnel", "--accept-server-license-terms", "--name", tunnel_name]

    state_dir().mkdir(parents=True, exist_ok=True)
    log_path().write_text("")
    with open(log_path(), "ab") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    pid_path().write_text(f"{process.pid}\n")

    found = _watch_log(timeout)
    return {**status(), "name": tunnel_name, **found}


def stop() -> Dict[str, Any]:
    """Stop the tunnel this machine started; saying so is not an error when none runs."""
    pid = read_pid()
    if pid is None or not _alive(pid):
        pid_path().unlink(missing_ok=True)
        return {"running": False, "stopped": False}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:  # pragma: no cover - the process died between the check and the signal
        raise AislandError(f"Could not stop the tunnel (pid {pid}): {exc}") from exc
    pid_path().unlink(missing_ok=True)
    return {"running": False, "stopped": True, "pid": pid}


def status() -> Dict[str, Any]:
    """What this machine's tunnel is doing, as JSON-serializable fields."""
    pid = read_pid()
    is_running = pid is not None and _alive(pid)
    payload: Dict[str, Any] = {
        "running": is_running,
        "pid": pid if is_running else None,
        "name": name_in_log(),
        "account": signed_in(),
        "provider": read_provider(),
        "cli": str(cli_path()) if cli_path().exists() else None,
        "log": str(log_path()) if log_path().exists() else None,
        "workspaces": len(load_store()),
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
    binary = install_cli()
    command = [str(binary), "tunnel", "user", "login"]
    chosen = provider or read_provider()
    if chosen:
        command += ["--provider", chosen]
    completed = subprocess.run(command)
    if completed.returncode == 0 and chosen:
        state_dir().mkdir(parents=True, exist_ok=True)
        provider_path().write_text(f"{chosen}\n")
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
# The remembered workspaces
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Workspace:
    """One workspace you have opened: what to open, where, and when you last did.

    Args:
        name: The tunnel name, which is what ``code --remote tunnel+<name>`` takes.
        url: The address this was learned from, if any — kept so it can be re-opened in a browser.
        folder: What to open on the remote machine — a folder, or a ``.code-workspace`` file.
        last_used: ISO-8601 UTC timestamp of the last open.
    """

    name: str
    url: str = ""
    folder: str = ""
    last_used: str = ""

    def age(self, now: Optional[datetime] = None) -> str:
        """How long ago this was used, in words ("just now", "2 minutes ago", "yesterday")."""
        return relative_age(self.last_used, now=now)


def _now_iso() -> str:
    """This moment, ISO-8601 in UTC, seconds resolution."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def relative_age(stamp: str, now: Optional[datetime] = None) -> str:
    """``stamp`` as an age in words; "unknown" when it is missing or unparseable."""
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "unknown"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (now or datetime.now(timezone.utc)) - when
    total = int(seconds.total_seconds())
    if total < 60:
        return "just now"
    if total < 3600:
        minutes = total // 60
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    if total < 86400:
        hours = total // 3600
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = total // 86400
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def load_store() -> List[Workspace]:
    """The remembered workspaces, newest first — an empty list when the file is absent or unreadable.

    This is the reader shell completion goes through, so it never raises: a half-written or
    hand-edited file reads as "nothing remembered" rather than breaking TAB.
    """
    try:
        raw = json.loads(store_path().read_text())
    except (OSError, ValueError):
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    workspaces: List[Workspace] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        workspaces.append(
            Workspace(
                name=str(entry["name"]),
                url=str(entry.get("url") or ""),
                folder=str(entry.get("folder") or ""),
                last_used=str(entry.get("last_used") or ""),
            )
        )
    return workspaces


def save_store(entries: Sequence[Workspace]) -> Path:
    """Write the remembered workspaces, newest first and capped at :data:`STORE_LIMIT`."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": STORE_VERSION, "entries": [asdict(entry) for entry in entries[:STORE_LIMIT]]}
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def store_payload(entries: Sequence[Workspace]) -> List[Dict[str, Any]]:
    """The remembered workspaces as JSON-serializable dicts — what ``--json`` and the MCP tool emit."""
    return [asdict(entry) for entry in entries]


def remember(name: str, url: str = "", folder: str = "") -> List[Workspace]:
    """Record a workspace as the most recently used one, replacing any earlier entry of that name."""
    previous = {entry.name: entry for entry in load_store()}
    earlier = previous.get(name)
    entry = Workspace(
        name=name,
        url=url or (earlier.url if earlier else ""),
        folder=folder or (earlier.folder if earlier else ""),
        last_used=_now_iso(),
    )
    rest = [item for item in load_store() if item.name != name]
    entries = [entry, *rest]
    save_store(entries)
    return entries[:STORE_LIMIT]


def forget(targets: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Drop the named workspaces; returns (cleared, unknown), each name as it was given.

    A target matches an entry's name or its URL, so a pasted address clears what it refers to.
    """
    entries = load_store()
    cleared: List[str] = []
    unknown: List[str] = []
    for target in targets:
        matches = [entry for entry in entries if target in (entry.name, entry.url)]
        if not matches:
            unknown.append(target)
            continue
        entries = [entry for entry in entries if entry not in matches]
        cleared.append(target)
    if cleared:
        save_store(entries)
    return cleared, unknown


def forget_all() -> int:
    """Forget every workspace; returns how many were remembered."""
    count = len(load_store())
    save_store([])
    return count


def parse_workspace_url(text: str) -> Workspace:
    """A pasted browser-IDE address as a workspace: what to open, and the name to tunnel to.

    The address carries what to open in one of two query parameters, and a browser IDE writes
    whichever the user opened: ``workspace`` for a ``.code-workspace`` file, ``folder`` for a plain
    directory. ``workspace`` wins when both are present, because that is the more specific choice.
    The last non-empty path segment names the workspace. The address itself is kept verbatim, so it
    can be opened in a browser again.
    """
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AislandError(f"Not a workspace address: {text}")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise AislandError(f"{text} names no workspace: its path is empty, so there is nothing to tunnel to.")
    query = parse_qs(parsed.query)
    folder = (query.get("workspace") or query.get("folder") or [""])[0]
    return Workspace(name=sanitize_name(segments[-1]), url=text, folder=folder)


def _ask_name(exact: str, stripped: str) -> str:
    """Ask which of the two names the tunnel carries, defaulting to the machine's own form."""
    click.echo(
        f"The address names '{exact}', but a remote machine usually names its own tunnel "
        f"'{stripped}' (its workspace name, without the environment suffix)."
    )
    return str(click.prompt("Tunnel name", type=click.Choice([exact, stripped]), default=stripped))


def resolve_target(target: Optional[str] = None, name: Optional[str] = None, ask: bool = True) -> Workspace:
    """What to open, and under which tunnel name.

    Args:
        target: A workspace address, a remembered name, or None for the most recent workspace.
        name: The tunnel name, when you know it — it always wins over the one in the address.
        ask: Whether an ambiguous address may ask; False takes the machine's own form silently
            (what ``--json`` and the MCP tool do, where there is nobody to ask).

    An address can only suggest a name: an ingress serving ``<workspace>-prod`` says nothing about
    what the box inside calls itself. So the name comes from ``name`` when given, else from a
    remembered workspace matching either form, else — when the two forms differ — from the question
    :func:`_ask_name` puts, whose answer is then remembered and never asked again.
    """
    remembered = load_store()

    if target is None:
        if name:
            chosen = sanitize_name(name)
            for entry in remembered:
                if entry.name == chosen:
                    return entry
            return Workspace(name=chosen)
        if not remembered:
            raise AislandError(
                "No workspace remembered yet. Open one by its address:\n"
                "      aisland tunnel open 'https://<host>/<workspace>/?folder=/mnt/workspace'"
            )
        return remembered[0]

    if "://" not in target:
        chosen = sanitize_name(name or target)
        for entry in remembered:
            if entry.name == chosen:
                return entry
        return Workspace(name=chosen)

    candidate = parse_workspace_url(target)
    if name:
        return replace(candidate, name=sanitize_name(name))

    stripped = strip_env_suffix(candidate.name)
    known = {entry.name for entry in remembered}
    if candidate.name in known or stripped == candidate.name:
        return candidate
    if stripped in known:
        return replace(candidate, name=stripped)
    return replace(candidate, name=_ask_name(candidate.name, stripped) if ask else stripped)


def complete_workspace(ctx: Any, param: Any, incomplete: str) -> List[Any]:
    """Shell completion for a workspace argument: the remembered names, with folder and age as help.

    Completion runs inside the user's shell on every TAB, so this answers from the cache only — no
    workspace needed, no network, and every failure answers "nothing to offer" instead of raising.
    """
    try:
        from click.shell_completion import CompletionItem

        items = []
        for entry in load_store():
            if entry.name.startswith(incomplete):
                detail = " — ".join(part for part in (entry.folder, entry.age()) if part)
                items.append(CompletionItem(entry.name, help=detail))
        return items
    except Exception:  # pragma: no cover - a completion callback must never break the shell
        return []


def open_editor(
    target: Optional[str] = None,
    folder: Optional[str] = None,
    name: Optional[str] = None,
    ask: bool = True,
) -> Dict[str, Any]:
    """Attach the LOCAL editor to a tunnel, remembering the workspace for next time.

    Args:
        target: A workspace address, a remembered name, or None for the most recent one.
        folder: Open this path instead of the remembered one — a folder or a ``.code-workspace`` file.
        name: The tunnel name, when the address does not carry it.
        ask: Whether an ambiguous address may ask which name the tunnel has.

    Returns:
        ``{"name", "folder", "url", "command"}`` — what was opened and the command that opened it.
    """
    workspace = resolve_target(target, name=name, ask=ask)
    chosen_folder = folder or workspace.folder
    editor = shutil.which("code")
    if editor is None:
        raise AislandError(
            "No 'code' command found. Install VS Code's shell command (Command Palette → "
            "'Shell Command: Install code command in PATH'), then run this again."
        )
    command = [editor, "--remote", f"tunnel+{workspace.name}"]
    if chosen_folder:
        command.append(chosen_folder)
    remember(workspace.name, url=workspace.url, folder=chosen_folder)
    subprocess.run(command, check=False)
    return {"name": workspace.name, "folder": chosen_folder, "url": workspace.url, "command": command}


def render_store(entries: Sequence[Workspace]) -> str:
    """The remembered workspaces as a table, newest first."""
    if not entries:
        return "No workspace remembered yet."
    name_width = max(len("NAME"), *(len(entry.name) for entry in entries))
    folder_width = max(len("OPENS"), *(len(entry.folder or "-") for entry in entries))
    age_width = max(len("LAST USED"), *(len(entry.age()) for entry in entries))
    lines = [f"{'NAME':<{name_width}}  {'OPENS':<{folder_width}}  {'LAST USED':<{age_width}}  URL"]
    for entry in entries:
        lines.append(
            f"{entry.name:<{name_width}}  {entry.folder or '-':<{folder_width}}  "
            f"{entry.age():<{age_width}}  {entry.url or '-'}"
        )
    return "\n".join(lines)
