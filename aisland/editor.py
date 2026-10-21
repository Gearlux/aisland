"""This machine's editor theme and colours, laid over the generated ``<workspace>.code-workspace``.

``aisland workspace theme|color`` record a choice per machine, in ``git config --local aisland.editor.*`` — never
in a committed file — and :func:`aisland.manager.write_code_workspace` adds it to the file on every rewrite, so
``aisland setup`` keeps it. VS Code reads the file's ``workbench.colorTheme`` for this window only (the user setting
stays every other window's theme) and lays ``workbench.colorCustomizations`` written without a ``[Theme]`` scope over
whichever theme is active, so the colours hold whatever the theme.

The themes are read from VS Code's own extension folder and the user's installed extensions (each
``package.json``'s ``contributes.themes``); without VS Code there is none.
"""

import difflib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, get_args

import click

from aisland.css_colors import NAMED_COLORS

ColorRole = Literal["background", "foreground"]
COLOR_ROLES: Tuple[ColorRole, ...] = get_args(ColorRole)
#: Where a colour goes: the window frame (title bar, activity bar, status bar — what the Peacock extension colours),
#: or the frame and the code (editor, side bar, panel, terminal).
ColorArea = Literal["frame", "both"]
COLOR_AREAS: Tuple[ColorArea, ...] = get_args(ColorArea)
DEFAULT_AREA: ColorArea = "frame"

ThemeKind = Literal["light", "dark", "high contrast", "high contrast light"]
#: A theme's ``uiTheme`` in its ``package.json`` -> what the list calls it.
_UI_THEMES: Dict[str, ThemeKind] = {
    "vs": "light",
    "vs-dark": "dark",
    "hc-black": "high contrast",
    "hc-light": "high contrast light",
}

#: The ``workbench.colorCustomizations`` keys each role sets, per area (VS Code's theme colour reference:
#: https://code.visualstudio.com/api/references/theme-color).
_FRAME: Dict[ColorRole, Tuple[str, ...]] = {
    "background": (
        "titleBar.activeBackground",
        "titleBar.inactiveBackground",
        "activityBar.background",
        "statusBar.background",
    ),
    "foreground": (
        "titleBar.activeForeground",
        "titleBar.inactiveForeground",
        "activityBar.foreground",
        "statusBar.foreground",
    ),
}
_CODE: Dict[ColorRole, Tuple[str, ...]] = {
    "background": (
        "editor.background",
        "editorGutter.background",
        "sideBar.background",
        "panel.background",
        "terminal.background",
    ),
    "foreground": ("foreground", "editor.foreground", "sideBar.foreground", "terminal.foreground"),
}

#: VS Code's own extension folder where it is installed without a ``code`` command on PATH (macOS installs none).
INSTALL_FOLDERS: Tuple[Path, ...] = (
    Path("/Applications/Visual Studio Code.app/Contents/Resources/app/extensions"),
    Path.home() / "Applications/Visual Studio Code.app/Contents/Resources/app/extensions",
    Path("/usr/share/code/resources/app/extensions"),
    Path("/usr/lib/code/resources/app/extensions"),
    Path("/snap/code/current/usr/share/code/resources/app/extensions"),
)

_KEY_PREFIX = "aisland.editor."
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


class EditorChoiceError(click.ClickException, ValueError):
    """A theme or colour that cannot be chosen; the CLI prints it as one ``Error:`` line."""


@dataclass(frozen=True)
class Theme:
    """One colour theme VS Code offers on this machine.

    Args:
        name: What ``workbench.colorTheme`` holds: the theme's ``id``, else its label.
        kind: Light, dark or high contrast, from its ``uiTheme``.
        source: ``built-in``, or the ``publisher.name`` of the extension that brings it.
    """

    name: str
    kind: ThemeKind
    source: str


@dataclass(frozen=True)
class EditorChoice:
    """This machine's choice, as ``.git/config`` holds it: each colour a CSS name or a ``#hex``."""

    theme: Optional[str] = None
    background: Optional[str] = None
    foreground: Optional[str] = None
    area: ColorArea = DEFAULT_AREA

    def color(self, role: ColorRole) -> Optional[str]:
        return self.background if role == "background" else self.foreground


# --- the themes ---------------------------------------------------------------------------


def extension_folders(
    code_command: Optional[str] = None,
    install_folders: Optional[Sequence[Path]] = None,
    home: Optional[Path] = None,
) -> List[Path]:
    """The folders VS Code's themes come from on this machine: its own, then the user's — those that exist.

    Its own folder is found from the ``code`` command (``code_command``, default the one on PATH) followed to the
    installation — macOS ``<app>/bin/code`` beside ``<app>/extensions``, Linux ``<code>/bin/code`` beside
    ``<code>/resources/app/extensions`` — else at the first of ``install_folders`` (default
    :data:`INSTALL_FOLDERS`) that exists. The user's is ``~/.vscode/extensions``. Nothing found, nothing listed.
    """
    command = code_command if code_command is not None else shutil.which("code")
    candidates: List[Path] = []
    if command and Path(command).exists():
        install = Path(command).resolve().parent.parent
        candidates += [install / "extensions", install / "resources" / "app" / "extensions"]
    candidates += list(INSTALL_FOLDERS if install_folders is None else install_folders)
    folders = [next((folder for folder in candidates if folder.is_dir()), None)]
    folders.append((home if home is not None else Path.home()) / ".vscode" / "extensions")
    return [folder for folder in folders if folder is not None and folder.is_dir()]


def _installed(folder: Path) -> List[Path]:
    """The extension folders in ``folder``: those its ``extensions.json`` lists when it has one (VS Code leaves an
    uninstalled or replaced version on disk until it cleans up), else every one."""
    listing = folder / "extensions.json"
    if listing.is_file():
        try:
            entries = json.loads(listing.read_text())
        except (OSError, json.JSONDecodeError):
            entries = None
        if isinstance(entries, list):
            names = {entry.get("relativeLocation") for entry in entries if isinstance(entry, dict)}
            return sorted(folder / name for name in names if isinstance(name, str) and (folder / name).is_dir())
    return sorted(child for child in folder.iterdir() if child.is_dir())


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _themes_of(extension: Path, built_in: bool) -> List[Theme]:
    manifest = _read_json(extension / "package.json")
    contributes = manifest.get("contributes")
    entries = contributes.get("themes") if isinstance(contributes, dict) else None
    if not isinstance(entries, list) or not entries:
        return []
    nls = _read_json(extension / "package.nls.json") if (extension / "package.nls.json").is_file() else {}
    source = "built-in" if built_in else f"{manifest.get('publisher', '')}.{manifest.get('name', extension.name)}"
    themes: List[Theme] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id") or entry.get("label")
        placeholder = re.fullmatch(r"%(.+)%", name) if isinstance(name, str) else None
        if placeholder is not None:
            name = nls.get(placeholder.group(1))
        if not isinstance(name, str) or not name:
            continue
        themes.append(Theme(name=name, kind=_UI_THEMES.get(entry.get("uiTheme", ""), "dark"), source=source))
    return themes


def list_themes() -> List[Theme]:
    """Every theme VS Code offers here, by name (case ignored); a name two extensions bring, the first's."""
    found: Dict[str, Theme] = {}
    for folder in extension_folders():
        built_in = folder.parent.name != ".vscode"  # the user's is ~/.vscode/extensions
        for extension in _installed(folder):
            for theme in _themes_of(extension, built_in):
                found.setdefault(theme.name, theme)
    return sorted(found.values(), key=lambda theme: theme.name.casefold())


def find_theme(typed: str, themes: Sequence[Theme]) -> Theme:
    """The theme named ``typed``, whatever its case; refused naming the closest when there is none."""
    if not themes:
        raise EditorChoiceError("VS Code is not installed on this machine: there is no theme to choose")
    by_folded = {theme.name.casefold(): theme for theme in themes}
    match = by_folded.get(typed.casefold())
    if match is not None:
        return match
    close = difflib.get_close_matches(typed.casefold(), list(by_folded), n=1)
    hint = f" (did you mean '{by_folded[close[0]].name}'?)" if close else ""
    raise EditorChoiceError(
        f"no installed VS Code theme is named '{typed}'{hint}. List them: aisland workspace theme list"
    )


# --- the colours --------------------------------------------------------------------------


def color_key(typed: str) -> str:
    """How a colour is kept: a CSS name folded to its table spelling (``Dark Slate Blue`` -> ``darkslateblue``),
    a ``#hex`` in lower case. Refused when it is neither."""
    folded = re.sub(r"[\s_-]+", "", typed).lower()
    if folded in NAMED_COLORS:
        return folded
    if _HEX.fullmatch(typed.strip()):
        return typed.strip().lower()
    raise EditorChoiceError(
        f"'{typed}' is not a colour: write a CSS name (navy, dark slate blue) or #RGB, #RGBA, #RRGGBB, #RRGGBBAA. "
        "List the names: aisland workspace color list"
    )


def hex_value(key: str) -> str:
    """A kept colour as VS Code reads it: a name's ``#rrggbb``, a hex as written."""
    return NAMED_COLORS.get(key, key)


def describe(key: Optional[str]) -> str:
    """``navy (#000080)``, ``#1e2a38``, or ``theme's`` for no colour."""
    if key is None:
        return "theme's"
    return f"{key} ({NAMED_COLORS[key]})" if key in NAMED_COLORS else key


def named_colors(part: str = "") -> List[Tuple[str, str]]:
    """``(name, #rrggbb)`` of every CSS name holding ``part`` (case, spaces and hyphens ignored), in the table's
    order."""
    folded = re.sub(r"[\s_-]+", "", part).lower()
    return [(name, value) for name, value in NAMED_COLORS.items() if folded in name]


# --- this machine's choice: git config --local aisland.editor.* ---------------------------


def _git_config(workspace_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "config", "--local", *args], cwd=workspace_root, capture_output=True, text=True)


def _require_repository(workspace_root: Path) -> None:
    if not (workspace_root / ".git").exists():
        raise EditorChoiceError(
            f"{workspace_root} is not a git repository: this machine's editor choice is kept in its .git/config"
        )


def read_choice(workspace_root: Path) -> EditorChoice:
    """This machine's choice; a value that cannot be read is ignored with a warning, never an error (setup runs on)."""
    if not (workspace_root / ".git").exists():
        return EditorChoice()
    listing = _git_config(workspace_root, "--get-regexp", r"^aisland\.editor\.").stdout
    values: Dict[str, Any] = {}
    for line in listing.splitlines():
        key, _, value = line.partition(" ")
        field = key[len(_KEY_PREFIX) :]
        try:
            if field in COLOR_ROLES:
                values[field] = color_key(value)
            elif field == "area":
                if value not in COLOR_AREAS:
                    raise EditorChoiceError(f"'{value}' is not one of {', '.join(COLOR_AREAS)}")
                values[field] = value
            elif field == "theme" and value:
                values[field] = value
        except EditorChoiceError as error:
            click.echo(f"  - Warning: ignoring {key}: {error.message}")
    return EditorChoice(**values)


def _store(workspace_root: Path, field: str, value: Optional[str]) -> None:
    _require_repository(workspace_root)
    if value is None:
        _git_config(workspace_root, "--unset", _KEY_PREFIX + field)
    else:
        _git_config(workspace_root, _KEY_PREFIX + field, value)


def set_theme(workspace_root: Path, typed: Optional[str]) -> Optional[Theme]:
    """Keep the theme ``typed`` names (``None`` forgets it); returns the theme, as VS Code spells it."""
    _require_repository(workspace_root)
    theme = find_theme(typed, list_themes()) if typed is not None else None
    _store(workspace_root, "theme", theme.name if theme else None)
    return theme


def set_color(workspace_root: Path, role: ColorRole, typed: Optional[str], area: Optional[ColorArea] = None) -> None:
    """Keep the colour ``typed`` for ``role`` (``None`` forgets it), and ``area`` when given."""
    _require_repository(workspace_root)
    key = color_key(typed) if typed is not None else None
    _store(workspace_root, role, key)
    if area is not None:
        _store(workspace_root, "area", area)


def clear_colors(workspace_root: Path) -> None:
    """Forget both colours and the area: the theme colours every part again."""
    _require_repository(workspace_root)
    for field in (*COLOR_ROLES, "area"):
        _store(workspace_root, field, None)


def workspace_settings(choice: EditorChoice) -> Dict[str, Any]:
    """The settings the choice adds to the workspace file; nothing for no choice."""
    settings: Dict[str, Any] = {}
    if choice.theme:
        settings["workbench.colorTheme"] = choice.theme
    customizations: Dict[str, str] = {}
    for role in COLOR_ROLES:
        key = choice.color(role)
        if key is None:
            continue
        keys = _FRAME[role] + (_CODE[role] if choice.area == "both" else ())
        customizations.update({name: hex_value(key) for name in keys})
    if customizations:
        settings["workbench.colorCustomizations"] = customizations
    return settings
