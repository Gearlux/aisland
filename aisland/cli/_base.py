"""The root group of `aisland` and what every command group shares.

`main` (the `--version` and `--debug` options), the group and command classes every group derives from (an empty word
after a command offers its options; outside a workspace a workspace command is refused, and a failure a command went
on past exits 1), the groups of the services a project brings, `aisland --debug`'s log, and the completers several
groups use. :mod:`aisland.cli` adds every command group to `main`.
"""

import logging
import subprocess
import traceback
from pathlib import Path
from typing import Callable, Dict, List, Optional, cast

import click
from click.shell_completion import CompletionItem

from aisland import discovery, failures, locations, tracking
from aisland.errors import AislandError, quiet_completion
from aisland.projects import get_workspace_root
from aisland.services import registry as service_registry
from aisland.workspace import file as workspace_file

#: The commands that run outside a workspace: creating one, installing a project's wheels elsewhere, the tunnel —
#: which exists to be run on a remote box that has no workspace at all —, the doctor, which checks the machine and
#: the Python it runs on, and the notebook cleaner, a git filter reading standard input in any repository.
_WORKSPACE_FREE = {"workspace", "wheels", "tunnel", "doctor", "notebooks"}


class _Command(click.Command):
    """A command whose TAB on an empty word offers its options.

    Click offers a command's options only once the word starts with `-`; it reaches this method only when no
    argument is left to fill (an argument's values come first), so `aisland wheels build models <TAB>` lists
    build's options — those not typed yet — as `-<TAB>` would.
    """

    def shell_complete(self, ctx: click.Context, incomplete: str) -> List[CompletionItem]:
        return super().shell_complete(ctx, incomplete or "-")


class _Group(click.Group):
    """A group whose commands are :class:`_Command` and whose subgroups are its own kind, so every aisland
    command gets the empty-word completion of its options. A group itself still offers its commands only."""

    command_class = _Command


_Group.group_class = _Group


class _WorkspaceGroup(_Group):
    """The top-level group, refusing a command that needs a workspace when there is none.

    From a wheel the workspace is where aisland runs (:mod:`aisland.locations`); outside one, every command that
    works on a workspace is refused before it writes anything into the folder it was run in. From a checkout there
    is always one. Checked on the whole command line (`parse_args`): a `--help` anywhere, a workspace-free command,
    and shell completion run anywhere.
    """

    def parse_args(self, ctx: click.Context, args: List[str]) -> List[str]:
        rest = super().parse_args(ctx, args)
        if ctx.resilient_parsing or "--help" in args or (args and args[0] in _WORKSPACE_FREE):
            return rest
        if not locations.workspace_found():
            raise AislandError(locations.missing_workspace_message())
        return rest

    def invoke(self, ctx: click.Context) -> object:
        """Run the command; a run that reported a failure it went on past (:mod:`aisland.failures`) exits 1, even
        when the command itself ended with exit 0."""
        failures.reset()
        try:
            result = super().invoke(ctx)
        except click.ClickException:
            if ctx.meta.get(_DEBUG):
                traceback.print_exc()
            raise
        except (OSError, subprocess.SubprocessError) as error:
            # What the machine refused (a file, a permission, a program that failed or ran past its time limit): one
            # Error line, like aisland's own refusals; --debug keeps the traceback. Anything else is a bug in aisland
            # and keeps its traceback.
            if ctx.meta.get(_DEBUG):
                raise
            raise AislandError(f"{error} (run `aisland --debug …` for the traceback)") from error
        except SystemExit as stop:
            if stop.code in (0, None) and failures.reported():
                raise SystemExit(1) from None
            raise
        except click.exceptions.Exit as stop:
            if stop.exit_code == 0 and failures.reported():
                raise SystemExit(1) from None
            raise
        if failures.reported():
            raise SystemExit(1)
        return result

    def _declared(self, ctx: click.Context) -> List["service_registry.Entry"]:
        """The services the projects bring, worked out once per run: `--help` asks each listed group for its help,
        and every lookup reads each project's declarations and the installed plugins again."""
        key = "aisland.declared-services"
        if key not in ctx.meta:
            ctx.meta[key] = service_registry.declared(get_workspace_root(), reserved=self.commands)
        return cast(List["service_registry.Entry"], ctx.meta[key])

    def list_commands(self, ctx: click.Context) -> List[str]:
        """aisland's commands, then a group per service a project declares."""
        names = super().list_commands(ctx)
        if not locations.workspace_found():
            return names
        try:
            declared = self._declared(ctx)
        except discovery.DeclarationError:
            return names  # the listing (--help, completion) stays usable; every command that reads them says why
        return names + sorted(entry.name for entry in declared if entry.shortcut)

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        command = super().get_command(ctx, cmd_name)
        if command is not None or not locations.workspace_found():
            return command
        for entry in self._declared(ctx):
            if entry.name == cmd_name and entry.shortcut:
                return _declared_service_group(entry)
        return None


def _service_entries() -> List["service_registry.Entry"]:
    """Every service of this workspace: aisland's own, then the ones its projects declare."""
    return service_registry.entries(get_workspace_root(), reserved=main.commands)


class _ServiceGroup(_Group):
    """`aisland <name> start|stop|restart|status|info|reset` for a service a project brings.

    Its commands are built the first time they are asked for: they carry the class's own options, and reading those
    imports the project's code — which listing the services (`aisland --help`) must not do."""

    def __init__(self, entry: "service_registry.Entry") -> None:
        super().__init__(name=entry.name, help=f"Manage {entry.name}, a service {entry.project} brings.")
        self._entry = entry
        self._built = False

    def _build(self) -> None:
        if not self._built:
            self._built = True
            _add_service_commands(self, self._entry)

    def list_commands(self, ctx: click.Context) -> List[str]:
        self._build()
        return super().list_commands(ctx)

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        self._build()
        return super().get_command(ctx, cmd_name)


def _declared_service_group(entry: "service_registry.Entry") -> click.Group:
    """The group of a service a project brings (:class:`_ServiceGroup`)."""
    return _ServiceGroup(entry)


def _add_service_commands(group: click.Group, entry: "service_registry.Entry") -> None:
    """The commands of a project's service, with its class's own options."""
    port = click.option("--port", type=int, default=None, help="The port to serve on (default: the service's own).")

    def with_port(command: Callable[..., None]) -> Callable[..., None]:
        """`--port` and the class's own options for a service a project implements; a declared one's port is in its
        declaration."""
        for name, help_text in reversed(list(entry.options.items())):
            command = click.option(f"--{name}", name, default=None, help=help_text)(command)
        return port(command) if entry.takes_port else command

    def ports(value: Optional[int]) -> Dict[str, int]:
        return {"port": value} if value is not None else {}

    @group.command(name="start")
    @click.option("--background", "-b", is_flag=True, help="Run in the background.")
    @with_port
    def start(background: bool, port: Optional[int] = None, **options: Optional[str]) -> None:
        """Start it."""
        entry.make().launch(background=background, **ports(port), **options)

    @group.command(name="stop")
    def stop() -> None:
        """Stop it."""
        entry.make().stop()

    @group.command(name="restart")
    @click.option("--background", "-b", is_flag=True, help="Run in the background.")
    @with_port
    def restart(background: bool, port: Optional[int] = None, **options: Optional[str]) -> None:
        """Stop and start it."""
        entry.make().restart(background=background, **ports(port), **options)

    @group.command(name="status")
    def status() -> None:
        """Its state."""
        entry.make().status()

    @group.command(name="info")
    def info() -> None:
        """What runs, where it is declared, its address, its log."""
        entry.make().info()

    # A class whose reset wipes no data says what it removes, in the question AND the help.
    @group.command(name="reset", help=entry.reset_question or "Stop it and wipe its data.")
    @click.confirmation_option(prompt=entry.reset_question or f"Stop {entry.name} and wipe its data?")
    def reset() -> None:
        entry.make().reset()


#: `ctx.meta` key: `aisland --debug` was given.
_DEBUG = "aisland.debug"


#: The parent of every module's logger (`logging.getLogger(__name__)`): `aisland --debug` shows its debug records.
_LOGGER = logging.getLogger("aisland")


class _DebugLog(logging.Handler):
    """aisland's debug log on stderr, one `aisland: <module>: <message>` line a record.

    Written through click, so the line reaches the stream click writes to — a test runner's too — and never mixes
    into stdout, which holds the command's result."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            click.echo(f"aisland: {record.name.removeprefix('aisland.')}: {record.getMessage()}", err=True)
        except (OSError, TypeError, ValueError):  # a closed stream, a message whose arguments do not fit it
            self.handleError(record)


#: One handler for the process, so a second run in it (a test's runner) never adds a second.
_debug_log = _DebugLog()


def _quiet_debug_log(level: int) -> None:
    """Take the debug log off again: the handler removed, the logger's level put back to ``level``."""
    _LOGGER.removeHandler(_debug_log)
    _LOGGER.setLevel(level)


def _show_debug_log(ctx: click.Context, param: click.Parameter, value: bool) -> bool:
    """`--debug`'s callback: the `aisland` logger at DEBUG with :data:`_debug_log`, until the run ends.

    Run while the options are parsed, so what the group decides before the command (the workspace root) is logged
    too. Without `--debug` nothing is configured: a record below WARNING goes nowhere and the output is unchanged."""
    if value and _debug_log not in _LOGGER.handlers:
        before = _LOGGER.level
        _LOGGER.addHandler(_debug_log)
        _LOGGER.setLevel(logging.DEBUG)
        ctx.call_on_close(lambda: _quiet_debug_log(before))
    return value


@click.group(cls=_WorkspaceGroup)
# The installed distribution's version (aisland.__version__ reads the same metadata); eager, so it answers before the
# workspace check, outside a workspace too.
@click.version_option(package_name="aisland", prog_name="aisland")
@click.option(
    "--debug",
    is_flag=True,
    callback=_show_debug_log,
    help="Show an error's traceback, not only its Error line, and aisland's debug log on stderr.",
)
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """One workspace of many Python projects, set up the same way on every machine: a shared venv, local services,
    generated CI and git aliases."""
    ctx.meta[_DEBUG] = debug


@quiet_completion
def _complete_submodules(ctx: click.Context, param: click.Parameter, incomplete: str) -> List[CompletionItem]:
    """The workspace's submodules whose name starts with ``incomplete``; nothing for a workspace it cannot read."""
    names = tracking.submodule_projects(get_workspace_root())
    return [CompletionItem(name) for name in names if name.startswith(incomplete)]


@quiet_completion
def _complete_workspace_projects(ctx: click.Context, param: click.Parameter, incomplete: str) -> List[CompletionItem]:
    """The projects of ``workspace.toml`` whose name starts with ``incomplete``; nothing for a file it cannot read."""
    names = list(workspace_file.load(get_workspace_root()).projects)
    return [CompletionItem(name) for name in names if name.startswith(incomplete)]


def _configure_root() -> Path:
    """The workspace a `configure` command reads or changes (`workspace` is workspace-free)."""
    if not locations.workspace_found():
        raise AislandError(locations.missing_workspace_message())
    return get_workspace_root()


def _editor_root() -> Path:
    """The workspace whose editor choice a `theme`/`color` command changes (`workspace` is workspace-free)."""
    if not locations.workspace_found():
        raise AislandError(locations.missing_workspace_message())
    return get_workspace_root()
