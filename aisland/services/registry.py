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

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Collection, List, Protocol, Sequence, Tuple

import click

from aisland.services.aim import AimService
from aisland.services.clearml import ClearMLService
from aisland.services.docker import DockerService
from aisland.services.iqengine import IQEngineService
from aisland.services.jenkins import JenkinsService
from aisland.services.labelstudio import LabelStudioService
from aisland.services.mlflow import MLflowService
from aisland.services.swiftbar import SwiftBarService


class Service(Protocol):
    """What the list asks of a service. Each ``launch``/``restart`` takes options of its own (a port, mounts), so
    they are typed as any arguments; the list passes ``background`` only where ``Entry.takes_background`` says."""

    name: str

    def launch(self, *args: Any, **kwargs: Any) -> None:
        """Start it."""

    def restart(self, *args: Any, **kwargs: Any) -> None:
        """Stop and start it."""

    def stop(self) -> None:
        """Stop it."""

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


ENTRIES: Tuple[Entry, ...] = (
    Entry("docker", DockerService, by_default=True, takes_background=False),
    Entry("jenkins", JenkinsService, by_default=True, takes_background=True),
    Entry("mlflow", MLflowService, by_default=True, takes_background=True),
    Entry("labelstudio", LabelStudioService, by_default=True, takes_background=True),
    Entry("swiftbar", SwiftBarService, by_default=True, takes_background=False),
    Entry("aim", AimService, by_default=False, takes_background=True),
    Entry("clearml", ClearMLService, by_default=False, takes_background=True),
    Entry("iqengine", IQEngineService, by_default=False, takes_background=True),
)


def declared(root: Path, reserved: Collection[str] = ()) -> List[Entry]:
    """The services the projects of ``root`` bring, after aisland's own: the ones they declare whose ``needs`` are
    chosen, then the ones they implement whose project is installed.

    A name of one of aisland's services, or of an aisland command (``reserved``), is refused naming the line: the
    project's service would hide it."""
    from aisland import discovery, selection
    from aisland.services.declared import DeclaredService
    from aisland.services.plugin import PluginService

    own = {entry.name for entry in ENTRIES}

    def free(name: str, where: object) -> None:
        if name in own:
            raise discovery.DeclarationError(
                f"{where}: service {name!r} is one of aisland's own — give it another name"
            )
        if name in reserved:
            raise discovery.DeclarationError(
                f"{where}: service {name!r} would hide the command `aisland {name}` — give it another name"
            )

    found = discovery.discover(root)
    entries: List[Entry] = []
    for name, service in found.services.items():
        free(name, service.where)
        if any(choice not in (selection.effective(root, needed) or []) for needed, choice in service.needs.items()):
            continue
        entries.append(Entry(name, partial(DeclaredService, service), by_default=False, takes_background=True))
    for name, loaded in discovery.load(found).items():
        if loaded.state != "installed" or loaded.kind != "service":
            continue
        free(name, loaded.plugin.where)
        entries.append(
            Entry(
                name, partial(PluginService, name, loaded.obj), by_default=False, takes_background=True, takes_port=True
            )
        )
    return entries


def entries(root: Path, reserved: Collection[str] = ()) -> List[Entry]:
    """aisland's services, then the projects' (:func:`declared`)."""
    return list(ENTRIES) + declared(root, reserved)


def chosen(entries: Sequence[Entry], names: Sequence[str], every: bool = False) -> List[Entry]:
    """The entries ``names`` name, in the order given; with no name, every entry (``every``) or the default ones.

    A name no service has is refused before anything is done."""
    by_name = {entry.name: entry for entry in entries}
    for name in names:
        if name not in by_name:
            raise click.ClickException(f"no service {name!r} (the services: {', '.join(sorted(by_name))})")
    if names:
        return [by_name[name] for name in names]
    return [entry for entry in entries if every or entry.by_default]


def start(entries: Sequence[Entry], names: Sequence[str], background: bool) -> None:
    for entry in chosen(entries, names):
        service = entry.make()
        if entry.takes_background:
            service.launch(background=background)
        else:
            service.launch()


def restart(entries: Sequence[Entry], names: Sequence[str], background: bool) -> None:
    for entry in chosen(entries, names):
        service = entry.make()
        if entry.takes_background:
            service.restart(background=background)
        else:
            service.restart()


def stop(entries: Sequence[Entry], names: Sequence[str]) -> None:
    for entry in _last_first(entries, names):
        entry.make().stop()


def reset(entries: Sequence[Entry], names: Sequence[str]) -> None:
    for entry in _last_first(entries, names):
        entry.make().reset()


def status(entries: Sequence[Entry], names: Sequence[str]) -> None:
    for entry in chosen(entries, names, every=True):
        entry.make().status()


def info(entries: Sequence[Entry], names: Sequence[str]) -> None:
    for entry in chosen(entries, names, every=True):
        entry.make().info()


def _last_first(entries: Sequence[Entry], names: Sequence[str]) -> List[Entry]:
    """Named: in the order given. Unnamed: the default services, the last started first."""
    picked = chosen(entries, names)
    return picked if names else list(reversed(picked))
