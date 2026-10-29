# The editor's files

The files `aisland setup` writes for the editor — Pyright configuration and the VS Code workspace file — the workspace window's own theme and colours, and the `mypy.ini` at the workspace root.

`aisland setup` also writes **`pyrightconfig.json`** files so Pyright /
Pylance resolve cross-project imports against the same project set
installed editable into the `.venv` — one at the workspace root
(`venvPath: "."`, plus each project dir on `extraPaths`) and one inside
**every project dir** (`venvPath: ".."` back to the shared `.venv`,
sibling projects on `extraPaths`) for when you open a single project as
its own editor window. All copies use relative paths only, so they are
portable across machines — and they are regenerated on
every setup, so they always track the live project list. Setup keeps them
out of git through the project's `.git/info/exclude` (never its `.gitignore`),
so a project's `git status` stays as it was. Don't hand-edit them; add a
project's `pyproject.toml` and re-run `aisland setup`. Don't commit
editor `extraPaths` in `.vscode/settings.json` either — pyright ignores
those whenever a `pyrightconfig.json` is present.

`aisland setup` also writes the **VS Code workspace file**,
`acme.code-workspace` (named after the workspace), from scratch on every
run. Its folder list is what this machine has: the workspace, every project
with a checkout in dependency order (a project before the projects that depend on it), and the
folder the logging library loggair writes its log files into, when loggair is installed in the
`.venv` and that folder exists. That folder
is asked from loggair itself (`python -m loggair --json`, from the workspace
root with `.env` loaded), so a `log_dir` in `~/.config/loggair/config.yaml` or
a `LOGGAIR_DIR` in `.env` decides it. A project set to `pypi` or `github`, or
an inactive optional submodule, has no checkout and is not listed.
A short name you give a project folder with a link in the workspace root
(`ln -s machine_learning ml`) is the name the folder shows under, its path still
the real folder; with two links to one folder the first by name wins. Such a
link is never a project of its own: setup, its extras prompt, `aisland workspace configure source`
list the folder once, under its real name, and `workspace.toml` writes the link as
the project's `path`. A link to a folder outside the workspace is a project like
any other.
`aisland workspace configure source set|unset` rewrite the file too, `--machine-only`
included. The settings and extension recommendations come from
`code-workspace.json` (inside the aisland package, so a wheel carries it): change a shared setting there and re-run
`aisland setup`. The file is gitignored and rewritten, so a setting you change
in VS Code at *Workspace* scope is gone at the next setup — put a personal one
in your *User* settings.

### One file alone (`aisland workspace code-workspace|bashrc`)

Each of setup's two editor-and-shell steps runs alone, without the rest of setup:

```bash
aisland workspace code-workspace   # writes acme.code-workspace
aisland workspace bashrc           # links project.bashrc to aisland's copy
```

```text
$ aisland workspace code-workspace
==> Wrote acme.code-workspace (4 folders)
$ aisland workspace code-workspace
==> acme.code-workspace is up to date
$ aisland workspace bashrc
==> project.bashrc already links to aisland/project.bashrc
```

An unchanged workspace file is left as it is, so VS Code does not reload it. `project.bashrc` is a link to the copy
aisland owns (`aisland/project.bashrc` in a checkout, `.aisland/project.bashrc` from a wheel); a link pointing
elsewhere is replaced, and a regular file in its place is refused, since it may hold your own lines — those belong in
`user.bashrc`, which `project.bashrc` sources last.

### This machine's theme and colours (`aisland workspace theme|color`)

The workspace window can have its own theme and its own background and
foreground colour, chosen on this machine only: the choice is kept in
`.git/config` (`aisland.editor.*`), never in a committed file, and every
rewrite of the workspace file carries it.

```bash
aisland workspace theme list                  # the themes VS Code offers here; * marks the workspace's
aisland workspace theme set "Solarized Dark"  # this window only — the others keep your user setting
aisland workspace theme unset
aisland workspace color list slate            # CSS's 148 colour names with a swatch; a part of the name filters
aisland workspace color set background navy   # title bar, activity bar and status bar
aisland workspace color set foreground '#e0e0e0' --area both   # the frame and the editor, side bar, panel, terminal
aisland workspace color set background navy --area sidebar     # the frame and the side bar; the code keeps the theme's
aisland workspace color unset background      # without a role: both colours go back to the theme
```

```text
$ aisland workspace color list slate
This workspace: background: navy (#000080), foreground: #e0e0e0, area: both
       darkslateblue         #483d8b
       darkslategray         #2f4f4f
       ...
```

A theme is named as `theme list` prints it (case ignored); the list is read
from VS Code's own folder and `~/.vscode/extensions`. On a remote machine —
one you open in a remote window, over SSH or a tunnel — VS Code is not
installed and the themes are your local VS Code's: `theme list` says so, and
`theme set` keeps the name as you typed it, unchecked:

```text
$ aisland workspace theme set "Monokai Pro"
==> Theme: Monokai Pro (this machine only; not checked: VS Code is not on this machine, so the theme comes from the VS Code that opens this workspace remotely)
```

Run `aisland workspace theme list` on your local machine to see the names. A colour is a CSS name
(`navy`, `dark slate blue`) or `#RGB`, `#RGBA`, `#RRGGBB`, `#RRGGBBAA`. The
colours hold whatever the theme: they are written unscoped into
`workbench.colorCustomizations`, which VS Code lays over the active theme.
`--area` (`frame`, the default; `sidebar`, the frame and the side bar; or `both`) is kept for later colours.

The **`mypy.ini`** at the workspace root is the opposite: committed, hand-maintained policy
(the per-module `[mypy-*]` sections). Setup only keeps its `mypy_path`
line complete — any discovered project missing from it is **appended**
(never removed: inactive submodules stay listed and mypy ignores path
entries that don't exist on a given machine, so the committed file stays
identical across machines).
