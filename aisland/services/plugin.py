"""A service a project implements in its own code: an ``aisland.plugins`` entry point of kind ``service``.

The project imports nothing of aisland (:mod:`aisland.workspace.discovery`, stage 2): its class needs only ``start(ctx)``, and
``stop``, ``status``, ``info`` and ``reset`` are aisland's own unless the class has them — ``info(ctx)`` adds lines
after aisland's, or stands alone for a class whose processes are not the supervisor's (``supervised = False``). A
class may declare options of its own (``options = {"repo": "help"}``), offered on ``start`` and ``restart``.
:class:`ServiceContext` is all the class gets::

    ctx.name, ctx.workspace, ctx.workspace_name   the service's name, the workspace and its name
    ctx.data_dir                                  its data folder
    ctx.port, ctx.background, ctx.options         `--port` (None when not given), `-b`, its own options' values
    ctx.run(command, env=, port=, ready=)         run under the supervisor; probed with a port and a ready path
    ctx.read_env(), ctx.write_env({KEY: value})   the workspace .env
    ctx.choose_command(selection, name)           the command that adds `name` to a selection, keeping the rest
    ctx.ports()                                   the ports the supervisor knows for it
    ctx.echo(text), ctx.fail(message)             print; stop with one `Error:` line

:mod:`aisland.plugins` types it (``ServiceContext``, ``ServicePlugin``) for a project to name under ``TYPE_CHECKING``,
and a class may declare the contract version it is written for, ``aisland_api`` (``aisland.plugins.PLUGIN_API``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, NoReturn, Optional, Sequence

from aisland import console, envfile, plugins
from aisland.errors import AislandError
from aisland.services import supervisor
from aisland.services.base import BaseService, ServiceState, service_state
from aisland.workspace import projects
from aisland.workspace.selection import add_command


class ServiceContext:
    """What aisland hands a project's service class: where it lives, what was asked, and how to act."""

    def __init__(
        self,
        service: "PluginService",
        port: Optional[int] = None,
        background: bool = False,
        options: Optional[Mapping[str, Optional[str]]] = None,
    ) -> None:
        self._service = service
        self.name = service.name
        self.workspace: Path = projects.get_workspace_root()
        self.workspace_name = projects.get_workspace_name()
        self.data_dir: Path = service.service_data_dir
        self.port = port
        self.background = background
        # Every option the class declares has a value here: the one given, else None.
        self.options: Dict[str, Optional[str]] = {name: None for name in service.options}
        self.options.update(options or {})

    def run(
        self,
        command: Sequence[str],
        env: Optional[Dict[str, str]] = None,
        port: Optional[int] = None,
        ready: Optional[str] = None,
    ) -> None:
        """Run ``command`` under the supervisor, refusing a ``port`` another process holds; with ``ready`` too, wait
        until it answers there."""
        readiness: Optional[supervisor.Readiness] = (
            {"port": port, "path": ready} if port is not None and ready is not None else None
        )
        self._service.start(list(command), env=env, background=self.background, readiness=readiness, port=port)

    def write_env(self, values: Dict[str, str]) -> None:
        """Set each key in the workspace ``.env``, an existing one replaced in place."""
        envfile.write(values)

    def read_env(self) -> Dict[str, str]:
        """The workspace ``.env``'s keys and values, as written."""
        return envfile.read(self.workspace)

    def choose_command(self, selection: str, name: str) -> str:
        """The command that adds ``name`` to ``selection`` keeping what this machine chose — ``set`` takes the whole
        list, so naming ``name`` alone would drop the rest."""

        return add_command(self.workspace, selection, name)

    def ports(self) -> List[int]:
        """The ports the supervisor knows for this service (none while it is not registered)."""
        return self._service.get_ports()

    def echo(self, text: str) -> None:
        console.echo(text)

    def fail(self, message: str) -> NoReturn:
        """Stop the command with one ``Error: <message>`` line."""
        raise AislandError(message)


if TYPE_CHECKING:  # mypy holds the context to what aisland.plugins tells a plugin's author it gets

    def _offers_what_plugins_are_told(ctx: ServiceContext) -> plugins.ServiceContext:
        return ctx


class PluginService(BaseService):
    """The service a project's class implements; what the class leaves out is :class:`BaseService`'s."""

    # The class is whatever the project's code defines — aisland only calls the methods it has.
    def __init__(self, name: str, implementation: Any) -> None:  # Any: a project's class, its methods its own
        super().__init__(name)
        self.implementation = implementation
        self.options: Dict[str, str] = dict(getattr(implementation, "options", {}))
        self.supervised = bool(getattr(implementation, "supervised", True))

    def _own(self, method: str) -> Optional[Callable[["ServiceContext"], object]]:
        found = getattr(self.implementation, method, None)
        return found if callable(found) else None

    def launch(self, background: bool = False, port: Optional[int] = None, **options: Optional[str]) -> None:
        self.service_data_dir.mkdir(parents=True, exist_ok=True)  # `ctx.data_dir`, which the class may write into
        self.implementation.start(ServiceContext(self, port=port, background=background, options=options))

    def restart(self, **kwargs: Any) -> None:  # Any: launch's options, each typed there
        console.echo(f"Restarting {self.name}...")
        self.stop()
        self.launch(**kwargs)

    def stop(self) -> None:
        own = self._own("stop")
        if own:
            own(ServiceContext(self))
        else:
            super().stop()

    def state(self) -> ServiceState:
        """The supervisor's answer for a supervised class; ``unknown`` for one whose processes are Docker's — its own
        ``status`` prints them, and a state aisland cannot read is never guessed."""
        return super().state() if self.supervised else service_state(self.name, "unknown")

    def status(self) -> None:
        own = self._own("status")
        if own:
            own(ServiceContext(self))
        else:
            super().status()

    def info(self) -> None:
        if self.supervised:  # a class whose processes are Docker's: the supervisor's lines would mislead
            super().info()
        own = self._own("info")
        if own:
            own(ServiceContext(self))

    def wipes(self) -> str:
        """The class's own words (``reset_question``) when its reset wipes something else, else the data folder."""
        said = getattr(self.implementation, "reset_question", None)
        return str(said) if said else super().wipes()

    def reset(self) -> None:
        own = self._own("reset")
        if own:
            own(ServiceContext(self))
        else:
            super().reset()
