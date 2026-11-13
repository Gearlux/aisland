"""`aisland`, the command line: the root group (:mod:`aisland.cli._base`) and every command group added to it, one
module each.
"""

from aisland.cli import services as services_cli
from aisland.cli import toplevel as toplevel_cli
from aisland.cli import tunnel as tunnel_cli
from aisland.cli import wheels as wheels_cli
from aisland.cli import workspace as workspace_cli
from aisland.cli._base import main

__all__ = ["main"]

for _command in (
    workspace_cli.workspace_group,
    toplevel_cli.setup,
    toplevel_cli.check,
    toplevel_cli.doctor,
    toplevel_cli.clean,
    toplevel_cli.backups,
    toplevel_cli.tests,
    toplevel_cli.completion,
    toplevel_cli.mcp,
    toplevel_cli.notebooks,
    wheels_cli.wheels_group,
    services_cli.services,
    services_cli.docker,
    services_cli.jenkins,
    services_cli.swiftbar,
    services_cli.supervisor,
    tunnel_cli.tunnel,
):
    main.add_command(_command)
