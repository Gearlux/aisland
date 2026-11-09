"""The workspaces you have opened through a tunnel — remembered so ``aisland tunnel open <TAB>`` offers them.

The remembered workspaces (``workspaces.json`` in the tunnel's state folder) are a *cache*, not configuration: they
exist so ``open`` offers what you used last instead of asking you to remember a name or re-paste an address. Because
that file backs shell completion, every reader treats an unreadable or malformed file as an empty list — a completion
callback that raises breaks the shell's TAB, not just this command.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import click

from aisland import atomic, console
from aisland.errors import AislandError
from aisland.tunnel.state import sanitize_name, state_dir

#: Suffixes a published address often carries that the machine's own name does not: an ingress
#: serves `<workspace>-prod` while the box inside calls itself `<workspace>`. A name derived from an
#: address is therefore ambiguous, and `resolve_target` says so instead of guessing silently.
ENV_SUFFIXES = ("-prod", "-dev", "-staging", "-test", "-qa")

#: How many workspaces are remembered, newest first.
STORE_LIMIT = 20
STORE_VERSION = 1


def store_path() -> Path:
    """The remembered workspaces that back ``open``, ``list``, ``clear`` and completion."""
    return state_dir() / "workspaces.json"


def strip_env_suffix(name: str) -> str:
    """``name`` without a trailing environment suffix (``-prod``, ``-dev``, …); unchanged without one."""
    for suffix in ENV_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


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
    atomic.write_text(path, json.dumps(payload, indent=2) + "\n")
    return path


def store_payload(entries: Sequence[Workspace]) -> List[Dict[str, object]]:
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


def checked_remote_path(path: str, where: str) -> str:
    """``path`` — what to open on the remote machine — when it is an absolute path; else a refusal naming ``where`` it
    came from. It goes onto `code`'s command line, so a value of a pasted address or the store must never read as an
    option (``--install-extension …``)."""
    if not path.startswith("/"):
        raise AislandError(
            f"{where} names {path!r} to open, which is no absolute path on the remote machine (it must start with /)."
        )
    return path


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
    if folder:
        checked_remote_path(folder, f"The address {text}")
    return Workspace(name=sanitize_name(segments[-1]), url=text, folder=folder)


def _ask_name(exact: str, stripped: str) -> str:
    """Ask which of the two names the tunnel carries, defaulting to the machine's own form."""
    console.echo(
        f"The address names '{exact}', but a remote machine usually names its own tunnel "
        f"'{stripped}' (its workspace name, without the environment suffix)."
    )
    return str(console.prompt("Tunnel name", type=click.Choice([exact, stripped]), default=stripped))


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
