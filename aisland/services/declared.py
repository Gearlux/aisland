"""A service a project declares in its own ``pyproject.toml``, run by aisland's supervisor like aisland's own.

    [tool.aisland.service.notes]
    command = ["notes-server", "--port", "8123"]   # what the supervisor runs
    port = 8123                                    # checked free before it starts …
    ready = "/health"                              # … and asked until it answers

The project writes no aisland code: :class:`DeclaredService` is the generic service the declaration fills —
:class:`aisland.services.base.BaseService` does the stopping, the status and the reset, as it does for every service.
"""

from __future__ import annotations

from typing import Optional

from aisland import console
from aisland.services import supervisor
from aisland.services.base import BaseService
from aisland.workspace.discovery import Service


class DeclaredService(BaseService):
    """The service ``declared`` describes: its command under the supervisor, its port checked free before it starts and
    probed when it has a ready path too."""

    def __init__(self, declared: Service) -> None:
        super().__init__(declared.name)
        self.declared = declared

    def launch(self, background: bool = False, **kwargs: object) -> None:
        """Run the declared command, refusing a port another process holds; with a ready path too, wait until
        ``http://localhost:<port><ready>`` answers."""
        port, ready = self.declared.port, self.declared.ready
        readiness: Optional[supervisor.Readiness] = (
            {"port": port, "path": ready} if port is not None and ready is not None else None
        )
        self.start(list(self.declared.command), background=background, readiness=readiness, port=port)

    def info(self) -> None:
        """What runs, where it is declared, and its address."""
        super().info()
        console.echo(f"  Command       : {' '.join(self.declared.command)}")
        console.echo(f"  Declared in   : {self.declared.where}")
        if self.declared.port is not None:
            console.echo(f"  URL           : http://localhost:{self.declared.port}")
