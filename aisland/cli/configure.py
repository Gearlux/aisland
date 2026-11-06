"""`aisland workspace configure …`: one set of commands built from each plugin's keys (:mod:`aisland.setup.configure`) —
the project-level built-ins and a workspace-level group per selection.
"""

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import click
from click.shell_completion import CompletionItem

from aisland import locations
from aisland.cli._base import _Command, _complete_workspace_projects, _configure_root, _Group
from aisland.errors import quiet_completion
from aisland.setup import configure
from aisland.workspace import check as workspace_check
from aisland.workspace import selection
from aisland.workspace.projects import get_workspace_root


def _plugin_group(plugin: configure.Plugin) -> click.Group:
    """`aisland workspace configure <plugin>`: its rows, and `set` / `unset` built from its keys."""

    @click.group(name=plugin.name, cls=_Group, invoke_without_command=True, help=plugin.summary)
    @click.option("--json", "as_json", is_flag=True, help="Print the rows and notes as one JSON object.")
    @click.pass_context
    def group(ctx: click.Context, as_json: bool) -> None:
        if ctx.invoked_subcommand is not None:
            return
        root = _configure_root()
        rows, notes = plugin.rows(root), plugin.notes(root)
        if as_json:
            click.echo(json.dumps({"rows": rows, "notes": notes}, indent=2))
            return
        for line in (configure.table(rows) if rows else plugin.empty.splitlines()) + notes:
            click.echo(line)

    def set_(project: str, machine_only: bool, **values: Optional[str]) -> None:
        value = values.pop(plugin.main.name)
        assert value is not None  # a required argument
        options = {key.name: values.get(key.name) for key in plugin.options}
        raise SystemExit(plugin.set(_configure_root(), project, value, options, machine_only))

    def unset(project: str, machine_only: bool) -> None:
        raise SystemExit(plugin.unset(_configure_root(), project, machine_only))

    choices = click.Choice(plugin.main.choices) if plugin.main.choices else click.STRING
    machine = click.Option(
        ["--machine-only"], is_flag=True, help="Change this machine only (.git/config); commit nothing."
    )
    group.add_command(
        _Command(
            "set",
            callback=set_,
            help=f"Set PROJECT's {plugin.main.name}: {plugin.main.help}",
            params=[
                click.Argument(["project"], shell_complete=_complete_workspace_projects),
                click.Argument([plugin.main.name], type=choices),
                *(click.Option([f"--{key.name}"], default=None, help=key.help) for key in plugin.options),
                machine,
            ],
        )
    )
    group.add_command(
        _Command(
            "unset",
            callback=unset,
            help=f"Remove PROJECT's {plugin.name} settings (with --machine-only, this machine's only).",
            params=[click.Argument(["project"], shell_complete=_complete_workspace_projects), machine],
        )
    )
    return group


def _selection_group(plugin: configure.SelectionPlugin) -> click.Group:
    """`aisland workspace configure <selection>`: its names and both layers, `set NAME…` and `unset`."""
    axis = plugin.axis

    @click.group(name=axis.name, cls=_Group, invoke_without_command=True, help=axis.question)
    @click.option("--json", "as_json", is_flag=True, help="Print the rows as one JSON object.")
    @click.pass_context
    def group(ctx: click.Context, as_json: bool) -> None:
        if ctx.invoked_subcommand is not None:
            return
        rows = selection.rows(_configure_root(), axis)
        if as_json:
            click.echo(json.dumps({"rows": rows, "notes": []}, indent=2))
            return
        for line in configure.table(rows) if rows else [f"No project offers a {axis.one}."]:
            click.echo(line)

    def completing(offer: Callable[[Path], Sequence[str]]) -> Callable[..., List[CompletionItem]]:
        """A completer of the names ``offer`` gives, those not typed yet that start with the word; nothing for a
        workspace it cannot read."""

        @quiet_completion
        def complete(ctx: click.Context, param: click.Parameter, incomplete: str) -> List[CompletionItem]:
            offered = offer(get_workspace_root())
            typed = set(ctx.params.get("names") or ())
            return [CompletionItem(name) for name in offered if name.startswith(incomplete) and name not in typed]

        return complete

    def set_(names: Tuple[str, ...], machine_only: bool) -> None:
        raise SystemExit(selection.set_selection(_configure_root(), axis, names, machine_only))

    def unset(names: Tuple[str, ...], machine_only: bool) -> None:
        raise SystemExit(selection.unset_selection(_configure_root(), axis, machine_only, names))

    machine = click.Option(
        ["--machine-only"], is_flag=True, help="Change this machine only (.git/config); commit nothing."
    )
    group.add_command(
        _Command(
            "set",
            callback=set_,
            help=f"Choose the {axis.one}s to install (the whole list) and reinstall the projects offering them.",
            params=[
                click.Argument(
                    ["names"], nargs=-1, shell_complete=completing(lambda root: selection.available(root, axis))
                ),
                machine,
            ],
        )
    )
    group.add_command(
        _Command(
            "unset",
            callback=unset,
            help=(
                f"Forget the {axis.name} choice (with --machine-only, this machine's only); setup asks again. "
                f"With NAME…, drop just those {axis.one}s from the choice and reinstall the projects offering them."
            ),
            params=[
                click.Argument(
                    ["names"],
                    nargs=-1,
                    shell_complete=completing(lambda root: selection.effective(root, axis.name) or []),
                ),
                machine,
            ],
        )
    )
    return group


def _configure_plugins() -> Dict[str, configure.AnyPlugin]:
    """The plugins of the workspace aisland runs in (the built-ins alone outside one)."""
    return configure.plugins(get_workspace_root() if locations.workspace_found() else None)


class _ConfigureGroup(_Group):
    """`aisland workspace configure`: one subgroup per plugin and selection, built when it is asked for."""

    def list_commands(self, ctx: click.Context) -> List[str]:
        return sorted(_configure_plugins())

    def get_command(self, ctx: click.Context, cmd_name: str) -> Optional[click.Command]:
        plugin = configure.PLUGINS.get(cmd_name) or _configure_plugins().get(cmd_name)
        if isinstance(plugin, configure.SelectionPlugin):
            return _selection_group(plugin)
        return _plugin_group(plugin) if plugin else None


@click.group("configure", cls=_ConfigureGroup, invoke_without_command=True)
@click.pass_context
def workspace_configure(ctx: click.Context) -> None:
    """Each plugin's settings of the workspace (workspace.toml) and of this machine (.git/config).

    \b
    aisland workspace configure                                  every plugin, and how to set it
    aisland workspace configure source                           where each project's package comes from
    aisland workspace configure source set models pypi
    aisland workspace configure source set models local --branch dev/main --machine-only
    aisland workspace configure source unset models
    aisland workspace configure extras set dev docs              the extras every project installs
    aisland workspace configure extras set dev --machine-only
    """
    if ctx.invoked_subcommand is None:
        root = get_workspace_root() if locations.workspace_found() else None
        for line in configure.overview(root):
            click.echo(line)
        for line in workspace_check.unknown_keys(root) if root else []:
            click.echo(line, err=True)
