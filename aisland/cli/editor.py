"""`aisland workspace theme|color …`: this machine's VS Code theme and colours (:mod:`aisland.editor`).
"""

from typing import List, Optional

import click
from click.shell_completion import CompletionItem

from aisland import editor
from aisland.cli._base import Group, workspace_root_found
from aisland.errors import quiet_completion
from aisland.setup import editor_files


@quiet_completion
def _complete_themes(ctx: click.Context, param: click.Parameter, incomplete: str) -> List[CompletionItem]:
    """The installed VS Code themes whose name starts with ``incomplete``, case ignored; nothing when they cannot be
    read."""
    themes = editor.list_themes()
    return [CompletionItem(t.name) for t in themes if t.name.casefold().startswith(incomplete.casefold())]


def _complete_colors(ctx: click.Context, param: click.Parameter, incomplete: str) -> List[CompletionItem]:
    return [CompletionItem(name) for name, _ in editor.named_colors() if name.startswith(incomplete.lower())]


@click.group("theme", cls=Group)
def theme_group() -> None:
    """This machine's VS Code theme for the workspace window, kept in .git/config (never committed).

    The workspace file's `workbench.colorTheme`: this window only — every other window keeps your user setting.
    """


@theme_group.command("list")
def theme_list() -> None:
    """The themes VS Code offers on this machine: name, kind, the extension that brings it; * marks the
    workspace's. Without VS Code here (a remote machine), a line saying the themes are your local VS Code's."""
    chosen = editor.read_choice(workspace_root_found()).theme
    themes = editor.list_themes()
    if not themes:
        click.echo(editor.NO_THEMES_HERE)
    width = max((len(t.name) for t in themes), default=0)
    kind_width = max((len(t.kind) for t in themes), default=0)
    for t in themes:
        mark = "*" if t.name == chosen else " "
        click.echo(f"{mark} {t.name:<{width}}  {t.kind:<{kind_width}}  {t.source}")


@theme_group.command("set")
@click.argument("name", shell_complete=_complete_themes)
def theme_set(name: str) -> None:
    """Show the workspace window in theme NAME (as `aisland workspace theme list` prints it, case ignored).

    On a machine without VS Code (a remote one: its window's themes are your local VS Code's) NAME is kept as
    typed, unchecked."""
    root = workspace_root_found()
    theme = editor.set_theme(root, name)
    editor_files.write_code_workspace(root)
    if theme is None or theme.checked:
        click.echo(f"==> Theme: {theme.name if theme else name} (this machine only)")
    else:
        click.echo(f"==> Theme: {theme.name} (this machine only; {editor.UNCHECKED_THEME})")


@theme_group.command("unset")
def theme_unset() -> None:
    """Forget the workspace's theme: the window shows your VS Code user setting again."""
    root = workspace_root_found()
    editor.set_theme(root, None)
    editor_files.write_code_workspace(root)
    click.echo("==> Theme: your VS Code user setting")


@click.group("color", cls=Group)
def color_group() -> None:
    """This machine's background and foreground colour for the workspace window, over any theme.

    Kept in .git/config (never committed). The colours go on the window frame — title bar, activity bar, status
    bar — and, with --area sidebar, on the side bar too, or, with --area both, on the code too: editor, side bar,
    panel and terminal.
    """


@color_group.command("list")
@click.argument("part", required=False, default="")
def color_list(part: str) -> None:
    """The colour names to choose from (CSS's 148), each with its swatch and value; PART keeps the names holding
    it. The first line is this workspace's choice; ← marks the chosen names."""
    choice = editor.read_choice(workspace_root_found())
    click.echo(
        f"This workspace: background: {editor.describe(choice.background)}, "
        f"foreground: {editor.describe(choice.foreground)}, area: {choice.area}"
    )
    width = max(len(name) for name, _ in editor.named_colors())
    for name, value in editor.named_colors(part):
        rgb = (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
        roles = [role for role in editor.COLOR_ROLES if choice.color(role) == name]
        mark = f"  ← {', '.join(roles)}" if roles else ""
        click.echo(f"{click.style(' ' * 6, bg=rgb)} {name:<{width}}  {value}{mark}")


@color_group.command("set")
@click.argument("role", type=click.Choice(editor.COLOR_ROLES))
@click.argument("color", shell_complete=_complete_colors)
@click.option(
    "--area",
    type=click.Choice(editor.COLOR_AREAS),
    default=None,
    help="frame: the title bar, activity bar and status bar; sidebar: those and the side bar; both: those and the "
    "editor, side bar, panel and terminal. Kept for later colours. Default: the kept one, else frame.",
)
def color_set(role: editor.ColorRole, color: str, area: Optional[editor.ColorArea]) -> None:
    """Colour the workspace window's ROLE (background or foreground) COLOR, whatever the theme.

    COLOR is a CSS name (`aisland workspace color list`: navy, dark slate blue) or #RGB, #RGBA, #RRGGBB,
    #RRGGBBAA: `aisland workspace color set background navy --area both`.
    """
    root = workspace_root_found()
    editor.set_color(root, role, color, area)
    editor_files.write_code_workspace(root)
    choice = editor.read_choice(root)
    where = {
        "frame": "the window frame",
        "sidebar": "the window frame and the side bar",
        "both": "the window frame and the code",
    }[choice.area]
    click.echo(f"==> {role.capitalize()}: {editor.describe(choice.color(role))}, on {where} (this machine only)")


@color_group.command("unset")
@click.argument("role", required=False, type=click.Choice(editor.COLOR_ROLES))
def color_unset(role: Optional[editor.ColorRole]) -> None:
    """Give ROLE's colour back to the theme; without ROLE, both colours (and the area)."""
    root = workspace_root_found()
    if role is None:
        editor.clear_colors(root)
    else:
        editor.set_color(root, role, None)
    editor_files.write_code_workspace(root)
    click.echo(f"==> {role.capitalize() if role else 'Background and foreground'}: the theme's")
