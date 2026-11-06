"""Every service aisland ships, in one list: what ``aisland services [start|stop|restart|status|info|reset]`` acts on.

The list is in START order; stopping and resetting go the other way, so the container engine the Docker-run services
need is the last to go. With no name, ``status`` and ``info`` cover every service, and ``start``, ``stop``,
``restart`` and ``reset`` cover the services started by default (``by_default``) — the others start when named.

A project's own services follow aisland's, started only when named: one it declares (``[tool.aisland.service.<name>]``,
:mod:`aisland.services.declared`), offered only while what it ``needs`` is chosen (``needs = { tracker = "aim" }``),
and one it implements in its own code (an ``aisland.plugins`` entry point of kind ``service``,
:mod:`aisland.services.plugin`), offered once its project is installed and given ``--port``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, List, Mapping, Optional, Protocol, Sequence, Tuple

import click

from aisland import failures
from aisland.errors import AislandError
from aisland.services import supervisor
from aisland.services.base import ServiceState
from aisland.services.declared import DeclaredService
from aisland.services.docker import docker_service
from aisland.services.jenkins import JenkinsService
from aisland.services.plugin import PluginService
from aisland.services.swiftbar import SwiftBarService
from aisland.workspace import discovery, selection

if TYPE_CHECKING:
    import importlib.metadata


class Service(Protocol):
    """What the list asks of a service. Each ``launch``/``restart`` takes options of its own (a port, mounts), so
    they are typed as any arguments; the list passes ``background`` only where ``Entry.takes_background`` says."""

    name: str

    def launch(self, *args: Any, **kwargs: Any) -> None:  # Any: each service's own options (a port, mounts)
        """Start it."""

    def restart(self, *args: Any, **kwargs: Any) -> None:  # Any: as launch's
        """Stop and start it."""

    def stop(self) -> None:
        """Stop it."""

    def state(self) -> ServiceState:
        """Its state, as data (``status`` prints it)."""

    def status(self) -> None:
        """Print its one-line state."""

    def info(self) -> None:
        """Print its URLs, credentials and paths."""

    def reset(self) -> None:
        """Stop it and wipe its data."""


@dataclass(frozen=True)
class Entry:
    """One service: its name, how to build it, whether it starts with no name given, and whether its ``launch`` and
    ``restart`` take ``background`` (the container engine and SwiftBar always detach on their own)."""

    name: str
    make: Callable[[], Service]
    by_default: bool
    takes_background: bool
    takes_port: bool = False
    #: The project that brings it (its help names it); empty for aisland's own.
    project: str = ""
    #: Whether it is also ``aisland <name> …``: not when an aisland command has that name, which wins.
    shortcut: bool = True
    #: What a project's class says of itself, read when first asked — its code is imported only then, so listing the
    #: services (`aisland --help`) imports no project's code.
    details: Callable[[], "Details"] = lambda: Details()

    @property
    def options(self) -> Mapping[str, str]:
        """A project's class's own options, by name, with their help (offered on its start and restart)."""
        return self.details().options

    @property
    def reset_question(self) -> Optional[str]:
        """What `aisland <name> reset` asks first, when a project's class says (its reset wipes no data)."""
        return self.details().reset_question


@dataclass(frozen=True)
class Details:
    """What a project's service class says of itself (none for aisland's own and the declared ones)."""

    options: Mapping[str, str] = field(default_factory=dict)
    reset_question: Optional[str] = None


class _Plugin:
    """A project's service class, imported and checked the first time it is needed, then kept."""

    def __init__(self, plugin: discovery.Plugin, entry: importlib.metadata.EntryPoint) -> None:
        self._plugin, self._entry = plugin, entry
        self._obj: Optional[Any] = None  # Any: a project's class, called by the methods its kind declares

    @property
    def obj(self) -> Any:  # Any: as _obj
        if self._obj is None:
            loaded = discovery.load_one(self._plugin, self._entry)
            if loaded.kind != "service":
                raise discovery.DeclarationError(
                    f"{self._plugin.where}: plugin {self._plugin.name!r} is a {loaded.kind}, not a service"
                )
            self._obj = loaded.obj
        return self._obj

    def details(self) -> Details:
        return Details(dict(getattr(self.obj, "options", {})), getattr(self.obj, "reset_question", None))

    def make(self) -> Service:
        return PluginService(self._plugin.name, self.obj)


ENTRIES: Tuple[Entry, ...] = (
    Entry("docker", docker_service, by_default=True, takes_background=False),
    Entry("jenkins", JenkinsService, by_default=True, takes_background=True),
    Entry("swiftbar", SwiftBarService, by_default=True, takes_background=False),
)


def declared(root: Path, reserved: Collection[str] = ()) -> List[Entry]:
    """The services the projects of ``root`` bring, after aisland's own: the ones they declare whose ``needs`` are
    chosen, then the ones they implement whose project is installed.

    A name of one of aisland's services is refused naming the line. A name of an aisland command (``reserved``) is
    not: the service has no ``aisland <name>`` shortcut (``Entry.shortcut``) and is reached as ``aisland services
    <verb> <name>``, so a command a newer aisland adds never breaks a workspace whose service has its name."""
    own = {entry.name for entry in ENTRIES}

    def free(name: str, where: object) -> None:
        if name in own:
            raise discovery.DeclarationError(
                f"{where}: service {name!r} is one of aisland's own — give it another name"
            )

    found = discovery.discover(root)
    entries: List[Entry] = []
    for name, service in found.services.items():
        free(name, service.where)
        if any(choice not in (selection.effective(root, needed) or []) for needed, choice in service.needs.items()):
            continue
        entries.append(
            Entry(
                name,
                partial(DeclaredService, service),
                by_default=False,
                takes_background=True,
                project=service.project,
                shortcut=name not in reserved,
            )
        )
    for name, entry in discovery.installed(found).items():
        if entry is None:  # its project is not installed yet
            continue
        plugin = found.plugins[name]
        free(name, plugin.where)
        lazy = _Plugin(plugin, entry)
        entries.append(
            Entry(
                name,
                lazy.make,
                by_default=False,
                takes_background=True,
                takes_port=True,
                project=plugin.project,
                details=lazy.details,
                shortcut=name not in reserved,
            )
        )
    return entries


def entries(root: Path, reserved: Collection[str] = ()) -> List[Entry]:
    """aisland's services, then the projects' (:func:`declared`) — every project's class imported and checked, so a
    command acting on the services (`aisland services …`) refuses a broken one before it does anything. Only the
    listing (`aisland --help`) leaves the classes unimported."""
    every = list(ENTRIES) + declared(root, reserved)
    for entry in every:
        entry.details()
    return every


def chosen(entries: Sequence[Entry], names: Sequence[str], every: bool = False) -> List[Entry]:
    """The entries ``names`` name, in the order given; with no name, every entry (``every``) or the default ones.

    A name no service has is refused before anything is done."""
    by_name = {entry.name: entry for entry in entries}
    for name in names:
        if name not in by_name:
            raise AislandError(f"no service {name!r} (the services: {', '.join(sorted(by_name))})")
    if names:
        return [by_name[name] for name in names]
    return [entry for entry in entries if every or entry.by_default]


def _each(picked: Sequence[Entry], action: Callable[[Entry], None]) -> None:
    """``action`` on every service in turn. Of several, one that fails is reported (an ``Error:`` line, the run exits
    1) and the others still get their turn — a missing container engine never keeps Jenkins down; a service run alone
    fails as itself."""
    for entry in picked:
        try:
            action(entry)
        except click.ClickException as error:
            if len(picked) == 1:
                raise
            failures.report(f"Error: {entry.name}: {error.format_message()}")


def _launch(entry: Entry, background: bool) -> None:
    service = entry.make()
    if entry.takes_background:
        service.launch(background=background)
    else:
        service.launch()


def _restart(entry: Entry, background: bool) -> None:
    service = entry.make()
    if entry.takes_background:
        service.restart(background=background)
    else:
        service.restart()


def start(entries: Sequence[Entry], names: Sequence[str], background: bool) -> None:
    _each(chosen(entries, names), lambda entry: _launch(entry, background))


def restart(entries: Sequence[Entry], names: Sequence[str], background: bool) -> None:
    _each(chosen(entries, names), lambda entry: _restart(entry, background))


def stop(entries: Sequence[Entry], names: Sequence[str]) -> None:
    _each(_last_first(entries, names), lambda entry: entry.make().stop())


def reset(entries: Sequence[Entry], names: Sequence[str]) -> None:
    for entry in _last_first(entries, names):
        entry.make().reset()


def status(entries: Sequence[Entry], names: Sequence[str]) -> None:
    with supervisor.snapshot():  # one `process list` for every service's state
        for entry in chosen(entries, names, every=True):
            entry.make().status()


def states(entries: Sequence[Entry], names: Sequence[str]) -> List[ServiceState]:
    """Each named service's state (default: every service): what ``aisland services status --json`` prints and the MCP
    tool ``services_status`` returns."""
    with supervisor.snapshot():
        return [entry.make().state() for entry in chosen(entries, names, every=True)]


def info(entries: Sequence[Entry], names: Sequence[str]) -> None:
    with supervisor.snapshot():
        for entry in chosen(entries, names, every=True):
            entry.make().info()


def _last_first(entries: Sequence[Entry], names: Sequence[str]) -> List[Entry]:
    """Named: in the order given. Unnamed: the default services, the last started first."""
    picked = chosen(entries, names)
    return picked if names else list(reversed(picked))
