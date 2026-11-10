# The editor's theme and colours are a per-machine choice laid over the generated workspace file (2026-10-21)

## Context

A window of one workspace should be told apart at a glance, or shown in a theme of its own. VS Code reads
`workbench.colorTheme` and `workbench.colorCustomizations` from a `.code-workspace` file, but aisland rewrites
that file on every `aisland setup` and `aisland workspace configure source …`, so a choice made in VS Code at *Workspace* scope (or
by the Peacock extension, which writes there) is lost at the next setup. A colour is a personal choice: the user
ruled it must stay on the machine and never reach a committed file.

## Decision

`aisland/editor.py` keeps the choice in `git config --local aisland.editor.{theme,background,foreground,area}`
— the per-machine layer `aisland workspace configure … --machine-only` already uses — and `editor_files.write_code_workspace` adds
`editor.workspace_settings(editor.read_choice(root))` over the template's settings on every write.

- The theme is what VS Code stores in `workbench.colorTheme`: a contributed theme's `id`, else its label
  (`settingsId = theme.id || label` in VS Code's `colorThemeData.ts`), a `%key%` label read from the extension's
  `package.nls.json`. The list comes from VS Code's own extension folder (found from the `code` command, else the
  standard install places) and `~/.vscode/extensions` (only what its `extensions.json` lists). A machine with no
  theme keeps the name as typed, unchecked (2026-10-26, below).
- The colours are written unscoped (no `"[Theme]"` key), so VS Code lays them over whichever theme is active.
  A colour is one of CSS Color 4's 148 names (`css_colors.py`, copied from the W3C table) or a hex form VS Code
  reads. `frame` colours what Peacock colours — the title bar, activity bar and status bar; `sidebar` adds the side
  bar with its title and section headers (left out, each keeps the theme's colour, a stripe across the side bar);
  `both` adds the editor, gutter, side bar, panel and terminal (key lists from VS Code's theme colour reference).
- A value hand-edited into `.git/config` that cannot be read is skipped with a warning: setup still writes the
  file.

## Consequences

- The choice survives every rewrite and never dirties a tracked file; another checkout keeps its own.
- A workspace that is no git repository cannot keep one (`theme|color set` say so).
- Nothing chosen, the file is byte-identical to before.

## Example

```text
$ git config --local --get-regexp '^aisland\.editor\.'
aisland.editor.theme Solarized Dark
aisland.editor.background navy
```

```jsonc
"workbench.colorTheme": "Solarized Dark",
"workbench.colorCustomizations": {
    "titleBar.activeBackground": "#000080",
    "titleBar.inactiveBackground": "#000080",
    "activityBar.background": "#000080",
    "statusBar.background": "#000080"
}
```

## What you may change

- The keys each area colours (`editor._FRAME`, `editor._SIDEBAR`, `editor._CODE`), and new areas beside
  `frame`/`sidebar`/`both`.
- The places VS Code is looked for (`editor.INSTALL_FOLDERS`).
- What must not change: the choice stays per machine and out of every committed file, and the colours stay
  unscoped so they hold whatever the theme.

## A machine without themes is a remote one: the name is kept unchecked (2026-10-26)

**Context.** `aisland workspace theme set "Monokai Pro"` typed in a remote window's terminal was refused with "VS
Code is not installed on this machine". The window's theme is applied by the local VS Code: Monokai Pro's
`package.json` says `"extensionKind": ["ui", "workspace"]` and was installed on the Mac only, while the remote (a
tunnel's box) had no `~/.vscode/extensions`, only Python and Jupyter extensions in `~/.vscode-server/extensions`, and
no `theme-*` folder in the tunnel server's `server/extensions`. A desktop VS Code ships its built-in themes (12
`theme-*` folders in VS Code 1.142.0 on macOS). The terminal's socket back to VS Code (`$VSCODE_IPC_HOOK_CLI`)
cannot ask for the local themes: the extension host's CLI server answers `open`, `openExternal`, `status` and
`extensionManagement` only, and the last lists the REMOTE extensions (`_remoteCLI.manageExtensions` uses
`remoteExtensionManagementServer`).

**Decision.** No theme found means VS Code is not on this machine, so a window showing the workspace comes from
another one: `set_theme` keeps the name as typed (`ChosenTheme(checked=False)`), the command says it is unchecked
(`editor.UNCHECKED_THEME`; the MCP tool's `note`), and `theme list` prints `editor.NO_THEMES_HERE`. A machine with
any theme still refuses an unknown name. A VS Code installed where aisland does not look (Insiders, VSCodium, an
unpacked tarball) is read the same way: unchecked, never wrongly refused.

**Consequences.** A typo on a remote machine is kept, and VS Code falls back to its default theme silently. The
detection needs no environment variable, so it holds in an `aisland tunnel connect` shell, which has no `code`.

**Example.**

```text
$ aisland workspace theme set "Monokai Pro"     # on the remote machine
==> Theme: Monokai Pro (this machine only; not checked: VS Code is not on this machine, so the theme comes from the VS Code that opens this workspace remotely)
```

**What you may change.** The wording; checking against a list the local VS Code hands over, should such a channel
appear (it would need an extension of aisland's own on the local machine).

---

[The architecture and every decision](../architecture.md#the-decisions)
