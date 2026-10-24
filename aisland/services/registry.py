"""Every service aisland ships, in one list: what ``aisland services [start|stop|restart|status|info|reset]`` acts on.

The list is in START order; stopping and resetting go the other way, so the container engine the Docker-run services
need is the last to go. With no name, ``status`` and ``info`` cover every service, and ``start``, ``stop``,
``restart`` and ``reset`` cover the services started by default (``by_default``) — the others start when named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Protocol, Sequence, Tuple

import click

from aisland.services.aim import AimService
from aisland.services.clearml import ClearMLService
from aisland.services.docker import DockerService
from aisland.services.fiftyone import FiftyOneService
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


ENTRIES: Tuple[Entry, ...] = (
    Entry("docker", DockerService, by_default=True, takes_background=False),
    Entry("jenkins", JenkinsService, by_default=True, takes_background=True),
    Entry("mlflow", MLflowService, by_default=True, takes_background=True),
    Entry("labelstudio", LabelStudioService, by_default=True, takes_background=True),
    Entry("fiftyone", FiftyOneService, by_default=True, takes_background=True),
    Entry("swiftbar", SwiftBarService, by_default=True, takes_background=False),
    Entry("aim", AimService, by_default=False, takes_background=True),
    Entry("clearml", ClearMLService, by_default=False, takes_background=True),
    Entry("iqengine", IQEngineService, by_default=False, takes_background=True),
)


def chosen(names: Sequence[str], every: bool = False) -> List[Entry]:
    """The entries ``names`` name, in the order given; with no name, every entry (``every``) or the default ones.

    A name no service has is refused before anything is done."""
    by_name = {entry.name: entry for entry in ENTRIES}
    for name in names:
        if name not in by_name:
            raise click.ClickException(f"no service {name!r} (the services: {', '.join(sorted(by_name))})")
    if names:
        return [by_name[name] for name in names]
    return [entry for entry in ENTRIES if every or entry.by_default]


def start(names: Sequence[str], background: bool) -> None:
    for entry in chosen(names):
        service = entry.make()
        if entry.takes_background:
            service.launch(background=background)
        else:
            service.launch()


def restart(names: Sequence[str], background: bool) -> None:
    for entry in chosen(names):
        service = entry.make()
        if entry.takes_background:
            service.restart(background=background)
        else:
            service.restart()


def stop(names: Sequence[str]) -> None:
    for entry in _last_first(names):
        entry.make().stop()


def reset(names: Sequence[str]) -> None:
    for entry in _last_first(names):
        entry.make().reset()


def status(names: Sequence[str]) -> None:
    for entry in chosen(names, every=True):
        entry.make().status()


def info(names: Sequence[str]) -> None:
    for entry in chosen(names, every=True):
        entry.make().info()


def _last_first(names: Sequence[str]) -> List[Entry]:
    """Named: in the order given. Unnamed: the default services, the last started first."""
    entries = chosen(names)
    return entries if names else list(reversed(entries))
