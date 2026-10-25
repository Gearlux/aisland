"""A service a project implements in its own code: an ``aisland.plugins`` entry point of kind ``service``.

The project imports nothing of aisland (:mod:`aisland.discovery`, stage 2): its class needs only ``start(ctx)``, and
``stop``, ``status``, ``info`` and ``reset`` are aisland's own unless the class has them — ``info(ctx)`` adds lines
after aisland's. :class:`ServiceContext` is all the class gets::

    ctx.name, ctx.workspace, ctx.data_dir        the service's name, the workspace, its data folder
    ctx.port, ctx.background                     `--port` (None when not given), `-b`
    ctx.run(command, env=, port=, ready=)        run under the supervisor; probed with a port and a ready path
    ctx.write_env({KEY: value})                  set keys in the workspace .env
    ctx.ports()                                  the ports the supervisor knows for it
    ctx.echo(text), ctx.fail(message)            print; stop with one `Error:` line
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from aisland import manager, supervisor
from aisland.manager import BaseService


class ServiceContext:
    """What aisland hands a project's service class: where it lives, what was asked, and how to act."""

    def __init__(self, service: "PluginService", port: Optional[int] = None, background: bool = False) -> None:
        self._service = service
        self.name = service.name
        self.workspace: Path = manager.get_workspace_root()
        self.data_dir: Path = service.service_data_dir
        self.port = port
        self.background = background

    def run(
        self,
        command: List[str],
        env: Optional[Dict[str, str]] = None,
        port: Optional[int] = None,
        ready: Optional[str] = None,
    ) -> None:
        """Run ``command`` under the supervisor; with ``port`` and ``ready``, wait until it answers there."""
        readiness = {"port": port, "path": ready} if port is not None and ready is not None else None
        self._service.start(list(command), env=env, background=self.background, readiness=readiness)

    def write_env(self, values: Dict[str, str]) -> None:
        """Set each key in the workspace ``.env``, an existing one replaced in place."""
        supervisor.write_workspace_env(values)

    def ports(self) -> List[int]:
        """The ports the supervisor knows for this service (none while it is not registered)."""
        return self._service.get_ports()

    def echo(self, text: str) -> None:
        click.echo(text)

    def fail(self, message: str) -> None:
        """Stop the command with one ``Error: <message>`` line."""
        raise click.ClickException(message)


class PluginService(BaseService):
    """The service a project's class implements; what the class leaves out is :class:`BaseService`'s."""

    # The class is whatever the project's code defines — aisland only calls the methods it has.
    def __init__(self, name: str, implementation: Any) -> None:
        super().__init__(name)
        self.implementation = implementation

    def _own(self, method: str) -> Optional[Any]:
        found = getattr(self.implementation, method, None)
        return found if callable(found) else None

    def launch(self, background: bool = False, port: Optional[int] = None, **kwargs: Any) -> None:
        self.implementation.start(ServiceContext(self, port=port, background=background))

    def restart(self, **kwargs: Any) -> None:
        click.echo(f"Restarting {self.name}...")
        self.stop()
        self.launch(**kwargs)

    def stop(self) -> None:
        own = self._own("stop")
        if own:
            own(ServiceContext(self))
        else:
            super().stop()

    def status(self) -> None:
        own = self._own("status")
        if own:
            own(ServiceContext(self))
        else:
            super().status()

    def info(self) -> None:
        super().info()
        own = self._own("info")
        if own:
            own(ServiceContext(self))

    def reset(self) -> None:
        own = self._own("reset")
        if own:
            own(ServiceContext(self))
        else:
            super().reset()
