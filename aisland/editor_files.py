"""The files an editor reads, generated per machine and never committed: the VS Code workspace file
(``<workspace>.code-workspace``), each project's ``pyrightconfig.json``, and the root ``mypy.ini``'s ``mypy_path``."""

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from aisland import editor, git_exclude, locations, supervisor
from aisland.paths import SERVICES_DIR
from aisland.projects import dependency_order, discover_projects, get_project_metadata, get_workspace_name

# Shared pyright exclude globs for the root and per-project configs below.
_PYRIGHT_EXCLUDE = [
    "**/__pycache__",
    "**/.venv",
    "**/.*_cache",
    "**/node_modules",
]


def write_pyrightconfig(workspace_root: Path, projects: List[dict]) -> None:
    """Generate ``pyrightconfig.json`` at the workspace root AND inside every project.

    The mono-repo lays each project out as ``<proj>/<pkg>/`` and installs them
    editable into the shared ``.venv``. Pyright / Pylance need each project root
    on ``extraPaths`` to resolve cross-project imports the same way the venv does
    (e.g. ``import confluid`` finds ``confluid/confluid``) — the editable-install
    ``__editable__`` finder hooks are not statically resolvable.

    Two audiences, one generator:
    - **Workspace root** — for editors opened on the whole mono-repo:
      ``venvPath: "."`` + each project dir on ``extraPaths``, and each project
      inside the root on ``exclude``: the generated workspace file opens every
      project as a folder of its own, so the root's analysis reading them too
      read every file twice and ran Pylance out of memory. A window opened on
      the bare root folder therefore indexes no project.
    - **Each project dir** — for editors opened on a single project as its own
      workspace folder (pyright only reads the config at the opened root):
      ``venvPath`` walks back to the shared workspace ``.venv`` and
      ``extraPaths`` lists the sibling projects. This replaces the previously
      COMMITTED per-project ``.vscode/settings.json`` ``extraPaths``, which
      drifted (they still named pre-rename projects) and leaked
      workspace-specific sibling paths to external consumers of a standalone
      project. Note pyright IGNORES ``python.analysis.extraPaths`` from
      ``settings.json`` whenever a ``pyrightconfig.json`` is present, so a
      stale local ``settings.json`` is neutralized, not merged.

    Portability: every path is RELATIVE to the file that carries it — no
    hardcoded absolute path, satisfying the workspace No-Hardcoded-Paths
    mandate. All copies are generated artifacts (gitignored — the root via the
    root ``.gitignore``, each project via its ``.git/info/exclude``) and are
    rewritten on every ``aisland setup`` so they always track the live project
    set.

    Plain JSON (no comments): ``pyrightconfig.json`` is consumed by an external
    tool whose comment tolerance we don't control, so provenance is carried by
    the exclude entry + this function rather than an in-file header.
    """
    extra_paths = sorted(os.path.relpath(p["path"], workspace_root) for p in projects)
    # The root's analysis leaves out every project inside it: the workspace file lists each project as a folder
    # of its own, whose analysis covers it, and the root's reading them again ran Pylance out of memory. Their
    # imports still resolve through `extraPaths`. A project outside the workspace holds no file of the root's.
    inside = [path for path in extra_paths if path.split(os.sep, 1)[0] != os.pardir]
    config = {
        "venvPath": ".",
        "venv": ".venv",
        "extraPaths": extra_paths,
        "exclude": [*_PYRIGHT_EXCLUDE, *inside],
    }
    out = workspace_root / "pyrightconfig.json"
    out.write_text(json.dumps(config, indent=4) + "\n")
    click.echo(f"==> Wrote {out} ({len(extra_paths)} project paths)")

    for project in projects:
        project_dir = Path(project["path"])
        siblings = sorted(os.path.relpath(p["path"], project_dir) for p in projects if Path(p["path"]) != project_dir)
        project_config = {
            "venvPath": os.path.relpath(workspace_root, project_dir),
            "venv": ".venv",
            "extraPaths": siblings,
            "exclude": list(_PYRIGHT_EXCLUDE),
        }
        (project_dir / "pyrightconfig.json").write_text(json.dumps(project_config, indent=4) + "\n")
        # Kept out of git with the editable install's metadata, in the project's own `.git/info/exclude`: a
        # `.gitignore` line would be a file to commit in every project (aisland.git_exclude).
        git_exclude.exclude(project_dir, ["pyrightconfig.json", "**/*.egg-info/"])
    click.echo(f"==> Wrote per-project pyrightconfig.json into {len(projects)} project dir(s)")


def sync_mypy_path(workspace_root: Path, projects: List[dict]) -> None:
    """Append missing project dirs to the ``mypy_path`` line of the root mypy.ini.

    Unlike ``pyrightconfig.json``, the root ``mypy.ini`` is COMMITTED,
    hand-maintained policy (dozens of per-module ``[mypy-*]`` sections) — not a
    generated artifact. The only part that must track the live project set is
    the ``mypy_path`` search path (the run-mypy-from-workspace-root mandate),
    so setup keeps exactly that line complete. Append-only by design:

    * entries are never removed — inactive submodules (a private project on a
      machine without its access) stay listed, and mypy silently ignores path
      entries that don't exist on this machine, so the committed file stays
      identical across machines instead of churning per activation set;
    * hand-added nested entries (``confluid/confluid``, ``aisland/aisland``)
      are preserved untouched.
    """
    ini = workspace_root / "mypy.ini"
    if not ini.exists():
        click.echo("  - Warning: no mypy.ini at the workspace root; skipping mypy_path sync.")
        return

    lines = ini.read_text().splitlines()
    line_no = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("mypy_path") and "=" in line),
        None,
    )
    if line_no is None:
        click.echo("  - Warning: mypy.ini has no 'mypy_path =' line; skipping mypy_path sync.")
        return

    key, _, value = lines[line_no].partition("=")
    # mypy accepts ':' or ',' as the separator; preserve whichever the file uses.
    separator = "," if "," in value else ":"
    entries = [entry.strip() for entry in value.split(separator) if entry.strip()]

    known = set(entries)
    missing = sorted({rel for p in projects if (rel := os.path.relpath(p["path"], workspace_root)) not in known})
    if not missing:
        return

    lines[line_no] = f"{key.rstrip()} = {separator.join(entries + missing)}"
    ini.write_text("\n".join(lines) + "\n")
    click.echo(f"==> mypy.ini: added {', '.join(missing)} to mypy_path")


#: The settings and extension recommendations every machine's workspace file carries. The folder list is the only
#: part written per machine, so the template holds none.
# Inside the package, so a wheel carries it (pyproject `package-data`).
CODE_WORKSPACE_TEMPLATE = SERVICES_DIR / "code-workspace.json"


# VS Code reads the file as JSONC, so the provenance header can live in the file itself.
_CODE_WORKSPACE_HEADER = """\
// =========================================================================
// AUTO-GENERATED FILE — DO NOT EDIT BY HAND
// Generated by: aisland setup
// Source template: aisland/aisland/code-workspace.json (aisland.editor_files.write_code_workspace)
// To regenerate:  aisland workspace code-workspace
// =========================================================================
//
// IF PYLANCE CRASHES — no completion, no hover, "Go to Symbol in Workspace"
// (Cmd+T / Ctrl+T) finds nothing:
//
// 1. Check that it is the crash. Cmd+Shift+P (Ctrl+Shift+P) -> "Output: Show
//    Output Channels..." -> "Python Language Server". A crash reads
//        FATAL ERROR: ... JavaScript heap out of memory
//    followed by "Pylance language server ... starting" again, every half
//    minute or so. The same log is on disk:
//        macOS: ~/Library/Application Support/Code/logs/<session>/window<N>/
//               exthost/ms-python.python/Python Language Server.log
//        Linux: ~/.config/Code/logs/<session>/window<N>/exthost/...
// 2. Give Pylance its own Node.js, with an 8 GB heap instead of VS Code's
//    1 GB: Cmd+Shift+P -> "Preferences: Open User Settings (JSON)" — in the
//    profile this workspace opens in — and add
//        "python.analysis.nodeExecutable": "auto"
//    It cannot go in this file: Pylance declares the setting machine-scoped,
//    and VS Code ignores a machine-scoped setting in a workspace file.
// 3. Cmd+Shift+P -> "Developer: Reload Window". Pylance downloads Node.js
//    from nodejs.org once; the "Python Language Server" output then starts with
//        Client: Running with node: .../ms-python.vscode-pylance-<version>/dist/.cache/node/node
// =========================================================================
"""


def code_workspace_path(workspace_root: Path) -> Path:
    """``<workspace>/<name>.code-workspace``, the name the template's ``${workspaceFolder:<name>}`` references use."""
    return workspace_root / f"{get_workspace_name(workspace_root)}.code-workspace"


def _loggair_log_dir(workspace_root: Path) -> Optional[Path]:
    """The folder loggair writes log files into for a run in this workspace, or None when loggair cannot say.

    Asked the way a run asks: from the workspace root, with the workspace ``.env`` exported over this process's
    environment (``source project.bashrc`` does the same), through loggair's own report
    (``python -m loggair --json``), which configures nothing and creates no folder. Out of process, so aisland
    takes no dependency on loggair; ``-P`` keeps the root's ``loggair/`` checkout from shadowing the package.
    """
    python = workspace_root / ".venv" / "bin" / "python"
    if not python.exists():
        return None
    env = {**os.environ, **supervisor.read_workspace_env(workspace_root)}
    try:
        result = subprocess.run(
            [str(python), "-P", "-m", "loggair", "--json"],
            cwd=workspace_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        click.echo("  - Note: loggair did not report its log folder within 60 s; no log folder listed.")
        return None
    if result.returncode != 0:
        # A workspace without loggair has no log folder to list, and nothing to be told about a tool it never used.
        if "No module named loggair" not in result.stderr:
            click.echo("  - Note: `python -m loggair --json` failed; no log folder listed.")
        return None
    try:
        log_dir = json.loads(result.stdout).get("resolved", {}).get("log_dir")
    except json.JSONDecodeError:
        log_dir = None
    if not log_dir:
        click.echo("  - Note: loggair's report names no log folder; no log folder listed.")
        return None
    return Path(log_dir)


#: The workspace the template is written for: its `${workspaceFolder:<name>}` references and terminal profile name.
_TEMPLATE_WORKSPACE = "WORKSPACE"


#: The key of this platform's terminal profiles in the template.
_PROFILE_PLATFORMS = {"Darwin": "osx", "Linux": "linux"}


def _terminal_shell(path: str) -> str:
    """The template's shell when this machine has it, else the bash on PATH (else the template's, unchanged)."""
    if Path(path).exists():
        return path
    return shutil.which("bash") or path


def _code_workspace_settings(name: str) -> Dict[str, Any]:  # Any: VS Code's settings JSON
    """The template's settings and extensions for the workspace ``name`` on this machine.

    The template names the workspace `WORKSPACE`: every `${workspaceFolder:WORKSPACE}` and the `WORKSPACE-bash`
    profile name become the workspace's own name, so they resolve in a workspace of any name. This platform's terminal profiles keep their shell when the machine has it and take the bash on
    PATH when it does not; the other platform's profiles are left as written.
    """
    text = CODE_WORKSPACE_TEMPLATE.read_text()
    text = text.replace(f"${{workspaceFolder:{_TEMPLATE_WORKSPACE}}}", f"${{workspaceFolder:{name}}}")
    text = text.replace(f'"{_TEMPLATE_WORKSPACE}-bash"', json.dumps(f"{name}-bash"))
    settings: Dict[str, Any] = json.loads(text)  # Any: as the return's
    key = _PROFILE_PLATFORMS.get(platform.system())
    if key is not None:
        for profile in settings["settings"].get(f"terminal.integrated.profiles.{key}", {}).values():
            profile["path"] = _terminal_shell(profile["path"])
    return settings


def write_code_workspace(workspace_root: Path) -> Optional[Path]:
    """Write this machine's VS Code workspace file. Returns its path when the content changed, else None.

    Generated and gitignored like ``pyrightconfig.json``: the folder list follows what is checked out HERE, so a
    per-machine source override shows in the editor without dirtying a tracked file. Folders: the workspace, then
    every project with a checkout in dependency order (sorted by folder name first, so the disk's listing order
    never reorders it), then loggair's log folder when it exists. A project a link in the workspace's root names
    (``ml -> machine_learning``) shows under that short name (:func:`aisland.locations.alias_names`), its path still the
    real folder. Settings and extensions come from
    :data:`CODE_WORKSPACE_TEMPLATE`, with this machine's editor theme and colours over them (:mod:`aisland.editor`).
    An unchanged file is not rewritten, so VS Code does not reload it.
    """
    name = get_workspace_name(workspace_root)
    projects = sorted(
        (get_project_metadata(p) for p in discover_projects(workspace_root)), key=lambda p: p["path"].name
    )
    short_names = locations.alias_names(workspace_root)
    folders = [{"name": name, "path": "."}]
    folders += [
        {
            "name": short_names.get(p["path"].resolve(), p["path"].name),
            "path": os.path.relpath(p["path"], workspace_root),
        }
        for p in dependency_order(projects)
    ]
    log_dir = _loggair_log_dir(workspace_root)
    if log_dir is not None and log_dir.is_dir():
        folders.append({"name": log_dir.name, "path": os.path.relpath(log_dir, workspace_root)})

    document = {"folders": folders, **_code_workspace_settings(name)}
    # This machine's theme and colours (`aisland workspace theme|color`), kept in .git/config, never the template.
    document["settings"].update(editor.workspace_settings(editor.read_choice(workspace_root)))
    text = _CODE_WORKSPACE_HEADER + json.dumps(document, indent=4) + "\n"
    out = code_workspace_path(workspace_root)
    if out.exists() and out.read_text() == text:
        return None
    out.write_text(text)
    click.echo(f"==> Wrote {out.name} ({len(folders)} folders)")
    return out
