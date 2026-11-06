"""What a project's ``aisland.plugins`` entry point is handed, typed — the one module of aisland a project may name.

A project never needs aisland installed to bring a plugin: aisland checks the object by its ``kind`` and that kind's
methods (:mod:`aisland.workspace.discovery`), never by a base class. These protocols only let an author type the
plugin, imported under ``TYPE_CHECKING`` so nothing of aisland is loaded at runtime::

    from __future__ import annotations

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from aisland.plugins import ServiceContext


    class Viewer:
        kind = "service"
        aisland_api = 1

        def start(self, ctx: ServiceContext) -> None:
            ctx.run(["viewer-app", "--port", str(ctx.port or 5151)])

A plugin may declare the version of this contract it is written for, ``aisland_api = <whole number>`` (none: 1). An
aisland that serves an older one refuses the plugin at load and names the upgrade; a plugin written for an older
version always loads. The contract's version rises only when a release changes what a plugin gets or must have.

Standard library only, and no aisland import: a release keeps what this module names (``docs/stability.md``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, NoReturn, Optional, Protocol, Sequence

#: The version of the contract this aisland serves. A plugin declares the one it is written for as
#: ``aisland_api = <whole number>``, 1 when absent: an older or equal one loads, a newer one is refused naming the
#: upgrade. It rises only when a release changes what a plugin gets or must have, with the protocols below.
PLUGIN_API = 1
#: The class attribute a plugin declares its version in.
PLUGIN_API_ATTRIBUTE = "aisland_api"


class ServiceContext(Protocol):
    """What aisland hands a service plugin's methods (``start(ctx)``, and ``stop``, ``status``, ``info``, ``reset``
    when the class has them)."""

    @property
    def name(self) -> str:
        """The service's name: its entry point's."""

    @property
    def workspace(self) -> Path:
        """The workspace's root folder."""

    @property
    def workspace_name(self) -> str:
        """The workspace's name."""

    @property
    def data_dir(self) -> Path:
        """The service's own data folder."""

    @property
    def port(self) -> Optional[int]:
        """The port ``--port`` asked for; ``None`` when not given."""

    @property
    def background(self) -> bool:
        """Whether ``-b`` asked to return once it runs."""

    @property
    def options(self) -> Mapping[str, Optional[str]]:
        """Each option the class declares (``options``), mapped to the value given or ``None``."""

    def run(
        self,
        command: Sequence[str],
        env: Optional[Dict[str, str]] = None,
        port: Optional[int] = None,
        ready: Optional[str] = None,
    ) -> None:
        """Run ``command`` under aisland's supervisor; with ``port`` and ``ready``, wait until it answers there."""

    def write_env(self, values: Dict[str, str]) -> None:
        """Set each key in the workspace ``.env``, an existing one replaced in place."""

    def read_env(self) -> Dict[str, str]:
        """The workspace ``.env``'s keys and values."""

    def choose_command(self, selection: str, name: str) -> str:
        """The command that adds ``name`` to ``selection``, keeping what this machine chose."""

    def ports(self) -> List[int]:
        """The ports the supervisor knows for this service."""

    def echo(self, text: str) -> None:
        """Print a line."""

    def fail(self, message: str) -> NoReturn:
        """Stop the command with one ``Error: <message>`` line."""


class ServicePlugin(Protocol):
    """A plugin of kind ``service``: ``kind = "service"`` and ``start(ctx)``.

    Optional, read when present: ``aisland_api`` (the contract version, 1 when absent), ``options`` (each option's
    name mapped to its help), ``supervised`` (``False`` when Docker runs its processes), ``reset_question``, and
    ``stop``, ``status``, ``info`` and ``reset``, each taking the same ``ctx``."""

    kind: str

    def start(self, ctx: ServiceContext) -> object:
        """Start the service."""
