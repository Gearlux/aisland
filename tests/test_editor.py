"""The editor's theme and colours, chosen per machine (`aisland workspace theme|color`, `aisland.editor`).

Covers the themes read from VS Code's own folder and the installed extensions (nothing when VS Code is not
there), the CSS colour names, the choice kept in `.git/config` and nowhere committed, and what each choice
writes into the generated `<workspace>.code-workspace` — which keeps it across every later rewrite.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import pytest
from click.testing import CliRunner

from aisland import cli, editor, manager
from aisland.css_colors import NAMED_COLORS

# --- fixtures -----------------------------------------------------------------------------


def _extension(
    root: Path,
    folder: str,
    themes: List[Any],  # a malformed manifest holds anything
    *,
    publisher: str = "",
    name: str = "",
    nls: Optional[Dict[str, str]] = None,
) -> Path:
    path = root / folder
    path.mkdir(parents=True)
    manifest: Dict[str, Any] = {"name": name or folder, "contributes": {"themes": themes}}
    if publisher:
        manifest["publisher"] = publisher
    (path / "package.json").write_text(json.dumps(manifest))
    if nls is not None:
        (path / "package.nls.json").write_text(json.dumps(nls))
    return path


@pytest.fixture
def vscode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    """A VS Code with two built-in theme extensions and one installed from the marketplace."""
    built_in = tmp_path / "Code.app" / "Contents" / "Resources" / "app" / "extensions"
    user = tmp_path / "home" / ".vscode" / "extensions"
    _extension(
        built_in,
        "theme-defaults",
        [
            {"id": "Dark Modern", "label": "%darkModern%", "uiTheme": "vs-dark"},
            {"id": "Light Modern", "label": "%lightModern%", "uiTheme": "vs"},
            {"id": "Default High Contrast", "label": "%hc%", "uiTheme": "hc-black"},
        ],
        nls={"darkModern": "Dark Modern", "lightModern": "Light Modern", "hc": "Dark High Contrast"},
    )
    _extension(
        built_in,
        "theme-solarized-dark",
        [{"label": "%themeLabel%", "uiTheme": "vs-dark"}],
        nls={"themeLabel": "Solarized Dark"},
    )
    _extension(built_in, "git", [])  # an extension contributing no theme
    _extension(
        user,
        "monokai.theme-monokai-pro-vscode-2.0.16",
        [
            {"label": "Monokai Pro (Filter Machine)", "uiTheme": "vs-dark"},
            {"label": "Monokai Pro Light", "uiTheme": "vs"},
        ],
        publisher="monokai",
        name="theme-monokai-pro-vscode",
    )
    monkeypatch.setattr(editor, "extension_folders", lambda: [built_in, user])
    return {"built_in": built_in, "user": user}


@pytest.fixture
def no_vscode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editor, "extension_folders", lambda: [])


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace `acme/` that is a git repository, as every workspace with submodules is."""
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(manager, "_loggair_log_dir", lambda workspace_root: None)
    monkeypatch.setattr(cli, "get_workspace_root", lambda: root)
    return root


def _settings(root: Path) -> Dict[str, Any]:
    text = manager.code_workspace_path(root).read_text()
    document: Dict[str, Any] = json.loads("\n".join(line for line in text.splitlines() if not line.startswith("//")))
    settings: Dict[str, Any] = document["settings"]
    return settings


def _run(*args: str) -> click.testing.Result:
    return CliRunner().invoke(cli.main, ["workspace", *args])


def _local_config(root: Path) -> str:
    return subprocess.run(
        ["git", "config", "--local", "--get-regexp", r"^aisland\.editor\."], cwd=root, capture_output=True, text=True
    ).stdout


# --- the themes on this machine -----------------------------------------------------------


def test_the_themes_are_vs_codes_own_and_the_installed_extensions(vscode: Dict[str, Path]) -> None:
    assert [(t.name, t.kind, t.source) for t in editor.list_themes()] == [
        ("Dark Modern", "dark", "built-in"),
        ("Default High Contrast", "high contrast", "built-in"),
        ("Light Modern", "light", "built-in"),
        ("Monokai Pro (Filter Machine)", "dark", "monokai.theme-monokai-pro-vscode"),
        ("Monokai Pro Light", "light", "monokai.theme-monokai-pro-vscode"),
        ("Solarized Dark", "dark", "built-in"),
    ]


def test_a_theme_is_named_by_its_id_and_without_one_by_its_label_read_from_the_nls_file(
    vscode: Dict[str, Path],
) -> None:
    """What VS Code stores in `workbench.colorTheme`: the id ("Default High Contrast", shown as "Dark High
    Contrast"), else the label, whose `%key%` placeholder is looked up in the extension's `package.nls.json`."""
    names = {t.name for t in editor.list_themes()}
    assert "Default High Contrast" in names and "Dark High Contrast" not in names
    assert "Solarized Dark" in names and "%themeLabel%" not in names


def test_a_label_whose_placeholder_has_no_translation_is_skipped(vscode: Dict[str, Path]) -> None:
    _extension(vscode["user"], "acme.broken-1.0.0", [{"label": "%missing%", "uiTheme": "vs"}], publisher="acme")
    assert all("%" not in t.name for t in editor.list_themes())


def test_an_extension_vs_code_no_longer_lists_is_left_out(vscode: Dict[str, Path]) -> None:
    """An uninstalled or replaced version stays on disk until VS Code cleans it up; `extensions.json` is the list
    of what is installed."""
    _extension(vscode["user"], "acme.old-theme-1.0.0", [{"label": "Acme Old", "uiTheme": "vs"}], publisher="acme")
    (vscode["user"] / "extensions.json").write_text(
        json.dumps([{"relativeLocation": "monokai.theme-monokai-pro-vscode-2.0.16"}])
    )
    names = {t.name for t in editor.list_themes()}
    assert "Acme Old" not in names
    assert "Monokai Pro Light" in names


def test_two_versions_of_one_extension_list_its_themes_once(vscode: Dict[str, Path]) -> None:
    _extension(
        vscode["user"],
        "monokai.theme-monokai-pro-vscode-2.0.15",
        [{"label": "Monokai Pro Light", "uiTheme": "vs"}],
        publisher="monokai",
        name="theme-monokai-pro-vscode",
    )
    assert [t.name for t in editor.list_themes()].count("Monokai Pro Light") == 1


def test_an_unreadable_manifest_is_skipped(vscode: Dict[str, Path]) -> None:
    (vscode["user"] / "acme.bad-1.0.0").mkdir()
    (vscode["user"] / "acme.bad-1.0.0" / "package.json").write_text("{not json")
    assert len(editor.list_themes()) == 6


def test_an_unreadable_extensions_json_lists_every_folder(vscode: Dict[str, Path]) -> None:
    (vscode["user"] / "extensions.json").write_text("{not json")
    assert "Monokai Pro Light" in {t.name for t in editor.list_themes()}


def test_a_theme_entry_that_is_no_object_is_skipped(vscode: Dict[str, Path]) -> None:
    _extension(vscode["user"], "acme.odd-1.0.0", ["Acme", {"label": "Acme Odd", "uiTheme": "vs"}], publisher="acme")
    assert "Acme Odd" in {t.name for t in editor.list_themes()}


def test_without_vs_code_no_theme_is_listed(no_vscode: None) -> None:
    assert editor.list_themes() == []


@pytest.mark.parametrize(
    "layout",
    [
        "Visual Studio Code.app/Contents/Resources/app",  # macOS: <app>/bin/code, <app>/extensions
        "usr/share/code",  # Linux: <code>/bin/code, <code>/resources/app/extensions
    ],
)
def test_vs_codes_own_folder_is_found_from_the_code_command(tmp_path: Path, layout: str) -> None:
    install = tmp_path / layout
    extensions = install / "extensions" if layout.endswith("/app") else install / "resources" / "app" / "extensions"
    extensions.mkdir(parents=True)
    (install / "bin").mkdir()
    (install / "bin" / "code").write_text("#!/bin/sh\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "code").symlink_to(install / "bin" / "code")
    folders = editor.extension_folders(code_command=str(tmp_path / "bin" / "code"), install_folders=[], home=tmp_path)
    assert folders == [extensions]


def test_the_folders_are_vs_codes_own_then_the_users_and_only_those_that_exist(tmp_path: Path) -> None:
    built_in = tmp_path / "Code.app" / "extensions"
    built_in.mkdir(parents=True)
    (tmp_path / ".vscode" / "extensions").mkdir(parents=True)
    no_code = str(tmp_path / "no-code-command")
    assert editor.extension_folders(
        code_command=no_code, install_folders=[tmp_path / "missing", built_in], home=tmp_path
    ) == [built_in, tmp_path / ".vscode" / "extensions"]
    assert editor.extension_folders(code_command=no_code, install_folders=[], home=tmp_path / "nobody") == []


def test_theme_list_prints_name_kind_and_source_and_marks_this_workspaces(vscode: Dict[str, Path], repo: Path) -> None:
    assert _run("theme", "set", "Solarized Dark").exit_code == 0
    result = _run("theme", "list")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 6
    assert re.fullmatch(r"\* Solarized Dark +dark +built-in", lines[-1])
    assert re.fullmatch(r"  Monokai Pro Light +light +monokai\.theme-monokai-pro-vscode", lines[-2])


def test_theme_list_prints_nothing_without_vs_code(no_vscode: None, repo: Path) -> None:
    result = _run("theme", "list")
    assert result.exit_code == 0
    assert result.output == ""


# --- choosing a theme ---------------------------------------------------------------------


def test_theme_set_writes_it_into_the_workspace_file(vscode: Dict[str, Path], repo: Path) -> None:
    result = _run("theme", "set", "Solarized Dark")
    assert result.exit_code == 0, result.output
    assert _settings(repo)["workbench.colorTheme"] == "Solarized Dark"
    assert "Theme: Solarized Dark (this machine only)" in result.output


def test_the_choice_lives_in_git_config_local_and_in_no_committed_file(vscode: Dict[str, Path], repo: Path) -> None:
    assert _run("theme", "set", "Solarized Dark").exit_code == 0
    assert _run("color", "set", "background", "navy").exit_code == 0
    assert _local_config(repo).splitlines() == ["aisland.editor.theme Solarized Dark", "aisland.editor.background navy"]
    assert not (repo / "config.toml").exists()
    tracked = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert "config.toml" not in tracked


def test_every_later_rewrite_keeps_the_choice(vscode: Dict[str, Path], repo: Path) -> None:
    """What `aisland setup` and `aisland workspace configure source …` do: write the file again from the template."""
    assert _run("theme", "set", "Solarized Dark").exit_code == 0
    assert _run("color", "set", "background", "navy").exit_code == 0
    manager.code_workspace_path(repo).unlink()
    manager.write_code_workspace(repo)
    settings = _settings(repo)
    assert settings["workbench.colorTheme"] == "Solarized Dark"
    assert settings["workbench.colorCustomizations"]["titleBar.activeBackground"] == "#000080"


def test_a_theme_name_is_matched_whatever_its_case_and_kept_as_vs_code_spells_it(
    vscode: Dict[str, Path], repo: Path
) -> None:
    assert _run("theme", "set", "solarized dark").exit_code == 0
    assert _settings(repo)["workbench.colorTheme"] == "Solarized Dark"


def test_an_unknown_theme_is_refused_naming_the_closest_and_nothing_is_kept(
    vscode: Dict[str, Path], repo: Path
) -> None:
    result = _run("theme", "set", "Solarzed Dark")
    assert result.exit_code == 1
    assert result.output == (
        "Error: no installed VS Code theme is named 'Solarzed Dark' (did you mean 'Solarized Dark'?). "
        "List them: aisland workspace theme list\n"
    )
    assert _local_config(repo) == ""
    assert not manager.code_workspace_path(repo).exists()


def test_a_theme_cannot_be_set_without_vs_code(no_vscode: None, repo: Path) -> None:
    result = _run("theme", "set", "Solarized Dark")
    assert result.exit_code == 1
    assert result.output == "Error: VS Code is not installed on this machine: there is no theme to choose\n"
    assert _local_config(repo) == ""


def test_theme_unset_gives_the_window_back_to_the_users_own_theme(vscode: Dict[str, Path], repo: Path) -> None:
    assert _run("theme", "set", "Solarized Dark").exit_code == 0
    result = _run("theme", "unset")
    assert result.exit_code == 0, result.output
    assert "workbench.colorTheme" not in _settings(repo)
    assert _local_config(repo) == ""
    assert "Theme: your VS Code user setting" in result.output


def test_nothing_chosen_writes_the_file_exactly_as_before(repo: Path) -> None:
    manager.write_code_workspace(repo)
    settings = _settings(repo)
    assert "workbench.colorTheme" not in settings
    assert "workbench.colorCustomizations" not in settings


def test_a_workspace_that_is_no_git_repository_cannot_keep_a_choice(
    vscode: Dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    monkeypatch.setattr(cli, "get_workspace_root", lambda: root)
    monkeypatch.setattr(manager, "_loggair_log_dir", lambda workspace_root: None)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    result = _run("theme", "set", "Solarized Dark")
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {root} is not a git repository: this machine's editor choice is kept in its .git/config\n"
    )
    assert editor.read_choice(root) == editor.EditorChoice()


# --- colours ------------------------------------------------------------------------------

FRAME_BACKGROUND = [
    "titleBar.activeBackground",
    "titleBar.inactiveBackground",
    "activityBar.background",
    "statusBar.background",
]
FRAME_FOREGROUND = [
    "titleBar.activeForeground",
    "titleBar.inactiveForeground",
    "activityBar.foreground",
    "statusBar.foreground",
]
CODE_BACKGROUND = [
    "editor.background",
    "editorGutter.background",
    "sideBar.background",
    "panel.background",
    "terminal.background",
]
CODE_FOREGROUND = ["foreground", "editor.foreground", "sideBar.foreground", "terminal.foreground"]
SIDEBAR_BACKGROUND = ["sideBar.background", "sideBarSectionHeader.background"]
SIDEBAR_FOREGROUND = ["sideBar.foreground", "sideBarTitle.foreground"]


def test_the_named_colours_are_the_148_of_css_color_4() -> None:
    assert len(NAMED_COLORS) == 148
    assert NAMED_COLORS["rebeccapurple"] == "#663399"
    assert NAMED_COLORS["grey"] == NAMED_COLORS["gray"] == "#808080"
    assert all(re.fullmatch(r"#[0-9a-f]{6}", value) for value in NAMED_COLORS.values())


def test_a_background_by_name_colours_the_window_frame_only(repo: Path) -> None:
    result = _run("color", "set", "background", "navy")
    assert result.exit_code == 0, result.output
    assert _settings(repo)["workbench.colorCustomizations"] == {key: "#000080" for key in FRAME_BACKGROUND}
    assert "Background: navy (#000080), on the window frame (this machine only)" in result.output


def test_a_foreground_is_set_on_its_own(repo: Path) -> None:
    assert _run("color", "set", "foreground", "white").exit_code == 0
    assert _settings(repo)["workbench.colorCustomizations"] == {key: "#ffffff" for key in FRAME_FOREGROUND}


def test_the_colours_hold_whatever_the_theme(vscode: Dict[str, Path], repo: Path) -> None:
    """Not scoped to a theme (`"[Solarized Dark]": {…}`): VS Code lays them over the active one."""
    assert _run("color", "set", "background", "navy").exit_code == 0
    assert _run("theme", "set", "Solarized Dark").exit_code == 0
    customizations = _settings(repo)["workbench.colorCustomizations"]
    assert not any(key.startswith("[") for key in customizations)
    assert customizations["statusBar.background"] == "#000080"


@pytest.mark.parametrize("typed", ["Dark Slate Blue", "DarkSlateBlue", "dark-slate-blue", "darkslateblue"])
def test_a_colour_name_is_matched_whatever_its_case_spaces_or_hyphens(repo: Path, typed: str) -> None:
    assert _run("color", "set", "background", typed).exit_code == 0
    assert _local_config(repo) == "aisland.editor.background darkslateblue\n"
    assert _settings(repo)["workbench.colorCustomizations"]["statusBar.background"] == "#483d8b"


@pytest.mark.parametrize(
    ("typed", "written"),
    [("#1E2A38", "#1e2a38"), ("#abc", "#abc"), ("#abcd", "#abcd"), ("#1e2a38cc", "#1e2a38cc")],
)
def test_a_hex_colour_is_taken_as_written_in_every_form_vs_code_reads(repo: Path, typed: str, written: str) -> None:
    assert _run("color", "set", "background", typed).exit_code == 0
    assert _settings(repo)["workbench.colorCustomizations"]["statusBar.background"] == written


@pytest.mark.parametrize("typed", ["bluish", "#12", "#ggg", "1e2a38", "#1e2a38c"])
def test_a_colour_that_is_neither_a_name_nor_hex_is_refused_and_nothing_is_kept(repo: Path, typed: str) -> None:
    result = _run("color", "set", "background", typed)
    assert result.exit_code == 1
    assert result.output.startswith(f"Error: '{typed}' is not a colour: write a CSS name")
    assert "aisland workspace color list" in result.output
    assert _local_config(repo) == ""


def test_area_both_colours_the_code_too_and_is_kept_for_later_colours(repo: Path) -> None:
    assert _run("color", "set", "background", "navy", "--area", "both").exit_code == 0
    assert _run("color", "set", "foreground", "white").exit_code == 0  # no --area: the kept one
    customizations = _settings(repo)["workbench.colorCustomizations"]
    assert customizations == {
        **{key: "#000080" for key in FRAME_BACKGROUND + CODE_BACKGROUND},
        **{key: "#ffffff" for key in FRAME_FOREGROUND + CODE_FOREGROUND},
    }
    assert _run("color", "set", "background", "navy", "--area", "frame").exit_code == 0
    assert set(_settings(repo)["workbench.colorCustomizations"]) == set(FRAME_BACKGROUND + FRAME_FOREGROUND)


def test_area_sidebar_colours_the_frame_and_the_side_bar_and_leaves_the_code_to_the_theme(repo: Path) -> None:
    """The side bar's title and section headers too: left out, each keeps the theme's colour as a stripe."""
    result = _run("color", "set", "background", "navy", "--area", "sidebar")
    assert result.exit_code == 0, result.output
    assert "Background: navy (#000080), on the window frame and the side bar (this machine only)" in result.output
    assert _run("color", "set", "foreground", "white").exit_code == 0  # no --area: the kept one
    assert _settings(repo)["workbench.colorCustomizations"] == {
        **{key: "#000080" for key in FRAME_BACKGROUND + SIDEBAR_BACKGROUND},
        **{key: "#ffffff" for key in FRAME_FOREGROUND + SIDEBAR_FOREGROUND},
    }
    assert _local_config(repo).splitlines() == [
        "aisland.editor.background navy",
        "aisland.editor.area sidebar",
        "aisland.editor.foreground white",
    ]


def test_area_sidebar_is_listed_in_the_colour_tables_header(repo: Path) -> None:
    assert _run("color", "set", "background", "navy", "--area", "sidebar").exit_code == 0
    assert _run("color", "list", "navy").output.splitlines()[0].endswith("area: sidebar")


def test_color_unset_one_role_keeps_the_other(repo: Path) -> None:
    assert _run("color", "set", "background", "navy").exit_code == 0
    assert _run("color", "set", "foreground", "white").exit_code == 0
    result = _run("color", "unset", "background")
    assert result.exit_code == 0, result.output
    assert set(_settings(repo)["workbench.colorCustomizations"]) == set(FRAME_FOREGROUND)


def test_color_unset_without_a_role_gives_every_colour_back_to_the_theme(repo: Path) -> None:
    assert _run("color", "set", "background", "navy", "--area", "both").exit_code == 0
    assert _run("color", "set", "foreground", "white").exit_code == 0
    assert _run("color", "unset").exit_code == 0
    assert "workbench.colorCustomizations" not in _settings(repo)
    assert _local_config(repo) == ""


@pytest.mark.parametrize(
    ("key", "value", "warning"),
    [
        ("background", "bluish", "  - Warning: ignoring aisland.editor.background: 'bluish' is not a colour"),
        (
            "area",
            "everywhere",
            "  - Warning: ignoring aisland.editor.area: 'everywhere' is not one of frame, sidebar, both",
        ),
    ],
)
def test_a_hand_edited_value_that_cannot_be_read_is_ignored_with_a_warning(
    repo: Path, capsys: pytest.CaptureFixture[str], key: str, value: str, warning: str
) -> None:
    """`aisland setup` must still write the file: the value is skipped, never the run."""
    subprocess.run(["git", "config", "--local", f"aisland.editor.{key}", value], cwd=repo, check=True)
    manager.write_code_workspace(repo)
    assert warning in capsys.readouterr().out
    assert "workbench.colorCustomizations" not in _settings(repo)


def test_a_key_aisland_does_not_know_and_an_empty_theme_are_ignored(repo: Path) -> None:
    subprocess.run(["git", "config", "--local", "aisland.editor.font", "Menlo"], cwd=repo, check=True)
    subprocess.run(["git", "config", "--local", "aisland.editor.theme", ""], cwd=repo, check=True)
    assert editor.read_choice(repo) == editor.EditorChoice()


def test_color_list_is_a_table_of_every_name_with_its_value(repo: Path) -> None:
    result = _run("color", "list")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "This workspace: background: theme's, foreground: theme's, area: frame"
    rows = lines[1:]
    assert len(rows) == 148
    assert re.fullmatch(r" {7}aliceblue +#f0f8ff", rows[0])
    assert re.fullmatch(r" {7}yellowgreen +#9acd32", rows[-1])


def test_color_list_filters_by_a_part_of_the_name_and_marks_the_chosen(repo: Path) -> None:
    assert _run("color", "set", "background", "darkslateblue", "--area", "both").exit_code == 0
    assert _run("color", "set", "foreground", "#e0e0e0").exit_code == 0
    result = _run("color", "list", "SLATE")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "This workspace: background: darkslateblue (#483d8b), foreground: #e0e0e0, area: both"
    names = [line.split()[0] for line in lines[1:]]
    assert names == [
        "darkslateblue",
        "darkslategray",
        "darkslategrey",
        "lightslategray",
        "lightslategrey",
        "mediumslateblue",
        "slateblue",
        "slategray",
        "slategrey",
    ]
    assert lines[1].endswith("  ← background")


def test_color_list_shows_a_swatch_on_a_terminal(repo: Path) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "color", "list", "navy"], color=True)
    assert "\x1b[48;2;0;0;128m" in result.output


# --- shell completion ---------------------------------------------------------------------


def _complete(args: List[str], incomplete: str) -> List[str]:
    from click.shell_completion import ShellComplete

    completion = ShellComplete(cli.main, {}, "aisland", "_AISLAND_COMPLETE")
    return [item.value for item in completion.get_completions(args, incomplete)]


def test_tab_after_theme_set_offers_the_installed_themes(vscode: Dict[str, Path]) -> None:
    assert _complete(["workspace", "theme", "set"], "Mono") == ["Monokai Pro (Filter Machine)", "Monokai Pro Light"]


def test_tab_after_color_set_offers_the_colour_names(repo: Path) -> None:
    assert _complete(["workspace", "color", "set", "background"], "darkslate") == [
        "darkslateblue",
        "darkslategray",
        "darkslategrey",
    ]


def test_a_completion_that_cannot_read_vs_code_offers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> List[Path]:
        raise OSError("unreadable")

    monkeypatch.setattr(editor, "extension_folders", broken)
    assert _complete(["workspace", "theme", "set"], "") == []


# --- the real machine ---------------------------------------------------------------------


def test_this_machines_vs_code_lists_its_built_in_themes() -> None:
    """Read for real: skipped where VS Code is not installed."""
    if not editor.extension_folders():
        pytest.skip("VS Code is not installed here")
    names = {t.name for t in editor.list_themes()}
    if not names:
        pytest.skip("this VS Code ships no theme extension where it was found")
    assert all(name and "%" not in name for name in names), names
    assert os.sep not in "".join(names)
