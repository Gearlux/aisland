"""`aisland workspace …`: init, show, projects, track, test-stats, provision, harness, mirror, the editor and shell files and the
environment files; `configure` (:mod:`aisland.cli.configure`) and `theme`/`color` (:mod:`aisland.cli.editor`) are
added here.
"""

import json
import os
from pathlib import Path
from typing import Callable, Optional, Tuple

import click

from aisland import failures, locations
from aisland.cli import configure as configure_cli
from aisland.cli import editor as editor_cli
from aisland.cli._base import (
    _complete_submodules,
    _complete_workspace_projects,
    _configure_root,
    _editor_root,
    _Group,
    main,
)
from aisland.errors import AislandError
from aisland.services.jenkins import JenkinsService
from aisland.setup import bashrc, configure, editor_files, env_example, harness
from aisland.setup import init as workspace_mod
from aisland.setup import provision
from aisland.workspace import check as workspace_check
from aisland.workspace import discovery, recording, tracking
from aisland.workspace.mirrors import run_sync
from aisland.workspace.model import Workspace
from aisland.workspace.projects import get_workspace_root, workspace_folders

# --- Workspace Commands ---


@click.group("workspace", cls=_Group)
def workspace_group() -> None:
    """Create a workspace aisland runs in, or write this one down for another machine."""


@workspace_group.command("init")
@click.argument("folder", type=click.Path(path_type=Path))
@click.option(
    "--add",
    "add",
    multiple=True,
    metavar="URL",
    help="A repository to add as a submodule (a project of the workspace). Repeat for several.",
)
@click.option(
    "--from",
    "from_file",
    type=click.Path(path_type=Path),
    metavar="FILE",
    help="A workspace.toml (`aisland setup` keeps one in every workspace): each of its projects is added as a "
    "submodule in its path, on its branch, before the --add ones; the file becomes the new workspace's own.",
)
@click.option(
    "--rewrite",
    "rewrite",
    multiple=True,
    type=click.Choice(workspace_mod.STARTING_FILES),
    metavar="FILE",
    help="A starting file to write again from aisland's template, the old copy kept as FILE.bak. Repeat for several. "
    "In an existing repository only these files are written.",
)
@click.option(
    "--local",
    "local",
    is_flag=True,
    help="A new root repository no one shares: it tracks only .gitmodules and the projects, and ignores everything "
    "else.",
)
@click.option(
    "--untracked",
    "untracked",
    is_flag=True,
    help="The root records none of its projects' commits: `git status` never shows them, `git pull-all` never "
    "records them (`aisland workspace track set` undoes it).",
)
def workspace_init(
    folder: Path,
    add: Tuple[str, ...],
    from_file: Optional[Path],
    rewrite: Tuple[workspace_mod.StartingFile, ...],
    local: bool,
    untracked: bool,
) -> None:
    """Make FOLDER a workspace: git init, the starting files, the submodules and the git aliases.

    Writes .gitignore, .env, mypy.ini and vscode-init-bash.sh, never over a file that exists. A folder that already
    is a git repository is set up in place: no git init, the missing files written, the --add projects added (--from
    and --local are refused there). --from names a workspace.toml: each of its projects is cloned into its path on
    its branch and the file is copied in as the new workspace's own; a file that cannot be read is refused, naming
    its line, before anything is written.

    --rewrite FILE writes FILE again over the one there, keeping it as FILE.bak (refused when FILE.bak exists);
    in an existing repository it is all init does: `aisland workspace init . --rewrite vscode-init-bash.sh`.

    --local makes a new root repository that tracks only the projects (its .gitignore ignores everything; add a
    project later with `git submodule add -f <url>`, then `aisland setup`). --untracked makes the root record none of
    the projects' commits: `aisland workspace track unset`, for every project.
    """
    workspace_mod.init_workspace(folder, add, from_file, rewrite, local, untracked)


def _track(projects: Tuple[str, ...], track: bool, machine_only: bool) -> None:
    for line in tracking.track_command(_configure_root(), projects, track, machine_only):
        click.echo(line)


_TRACK_ARGUMENTS = [
    click.argument("projects", nargs=-1, metavar="[PROJECT]...", shell_complete=_complete_submodules),
    click.option("--machine-only", is_flag=True, help="Change this machine only (.git/config); commit nothing."),
]


def _with_track_arguments(command: Callable[..., None]) -> Callable[..., None]:
    """``track set`` and ``track unset`` take the same arguments."""
    for decorator in reversed(_TRACK_ARGUMENTS):
        command = decorator(command)
    return command


@workspace_group.group("track", cls=_Group)
def workspace_track() -> None:
    """Whether the root records the projects' commits: set it, unset it, or see what is set.

    \b
    aisland workspace track unset                     every project: the root records none of their commits
    aisland workspace track unset models              one project
    aisland workspace track set models                record its commits again
    aisland workspace track unset --machine-only      this machine only (.git/config); nothing to commit
    aisland workspace track status                    which projects, whose choice, whether git agrees
    """


@workspace_track.command("unset")
@_with_track_arguments
def workspace_track_unset(projects: Tuple[str, ...], machine_only: bool) -> None:
    """Stop recording PROJECT's commits in the root (no PROJECT: every submodule).

    `git status` in the root no longer shows the project's new commits, `git add <project>` skips it and `git
    pull-all` never records them; the root keeps the commit it last recorded. Written for everyone as `track = false`
    in workspace.toml and `ignore = all` in .gitmodules (commit them), or with --machine-only in .git/config alone.
    """
    _track(projects, False, machine_only)


@workspace_track.command("set")
@_with_track_arguments
def workspace_track_set(projects: Tuple[str, ...], machine_only: bool) -> None:
    """Record PROJECT's commits in the root again (no PROJECT: every submodule).

    A project whose commits moved meanwhile shows again in `git status` (` M <project>`), and the next `git pull-all`
    records it once origin has them. Without --machine-only it also drops this machine's own choice.
    """
    _track(projects, True, machine_only)


@workspace_track.command("status")
@click.argument("projects", nargs=-1, metavar="[PROJECT]...", shell_complete=_complete_submodules)
@click.option("--json", "as_json", is_flag=True, help="Print the rows as one JSON object.")
def workspace_track_status(projects: Tuple[str, ...], as_json: bool) -> None:
    """Whether the root records PROJECT's commits, whose choice that is and whether git agrees (no PROJECT: every
    submodule). Changes nothing.

    With no PROJECT it says the workspace's choice and lists the projects that differ from it, or that git does not
    yet agree with (`aisland setup` writes git's setting); the others are counted. With PROJECTs it gives a line each.
    """
    root = _configure_root()
    if as_json:
        click.echo(json.dumps(tracking.status_json(root, projects), indent=2))
        return
    for line in tracking.status_lines(root, projects):
        click.echo(line)


@workspace_group.group("test-stats", cls=_Group)
def workspace_test_stats() -> None:
    """Whether the pytest plugin records the projects' test runs (for `aisland tests report`).

    \b
    aisland workspace test-stats off                       every project, and every one added later
    aisland workspace test-stats off models                one project
    aisland workspace test-stats off --machine-only        this machine only (.git/config)
    aisland workspace test-stats on models                 recorded again
    """


_TEST_STATS_ARGUMENTS = [
    click.argument("projects", nargs=-1, metavar="[PROJECT]...", shell_complete=_complete_workspace_projects),
    click.option("--machine-only", is_flag=True, help="Change this machine only (.git/config); commit nothing."),
]


def _with_test_stats_arguments(command: Callable[..., None]) -> Callable[..., None]:
    """``on`` and ``off`` take the same arguments."""
    for decorator in reversed(_TEST_STATS_ARGUMENTS):
        command = decorator(command)
    return command


@workspace_test_stats.command("off")
@_with_test_stats_arguments
def workspace_test_stats_off(projects: Tuple[str, ...], machine_only: bool) -> None:
    """Stop recording PROJECT's test runs (no PROJECT: every project, and every one added later).

    Written for everyone as `test-stats = false` in workspace.toml (commit it), or with --machine-only in .git/config
    alone; this machine's choice wins over the committed one.
    """
    for line in recording.command(_configure_root(), projects, False, machine_only):
        click.echo(line)


@workspace_test_stats.command("on")
@_with_test_stats_arguments
def workspace_test_stats_on(projects: Tuple[str, ...], machine_only: bool) -> None:
    """Record PROJECT's test runs again (no PROJECT: every project). Without --machine-only it also drops this
    machine's own choice for them."""
    for line in recording.command(_configure_root(), projects, True, machine_only):
        click.echo(line)


@workspace_group.command("show")
@click.option("--json", "as_json", is_flag=True, help="Print it as one JSON object.")
def workspace_show(as_json: bool) -> None:
    """What the workspace's projects declare: selections, services and code plugins, and from which project.

    Read from each project's pyproject.toml ([tool.aisland.selection.*], [tool.aisland.service.*],
    [project.entry-points."aisland.plugins"]) — nothing needs to be installed; a code plugin shows whether its
    project is. A declaration aisland cannot use is refused, naming its file and line.
    """
    if not locations.workspace_found():
        raise AislandError(locations.missing_workspace_message())
    root = get_workspace_root()
    found = discovery.discover(root)
    loaded = discovery.load(found)
    for line in workspace_check.unknown_keys(root):
        click.echo(line, err=True)
    for name in sorted((set(found.services) | set(found.plugins)) & set(main.commands)):
        failures.warn(
            f"Warning: service {name!r} shares its name with the command `aisland {name}`, which wins: "
            f"reach it as `aisland services start {name}` (and stop, restart, status, info, reset)"
        )
    if as_json:
        click.echo(json.dumps(discovery.as_json(found, loaded), indent=2))
        return
    for line in discovery.report_lines(found, loaded):
        click.echo(line)


@workspace_group.command("projects")
@click.option("--json", "as_json", is_flag=True, help="Print them as one JSON object.")
def workspace_projects(as_json: bool) -> None:
    """The workspace's projects with a checkout, every dependency before the projects that need it: one folder a
    line, or with --json each project's folder and distribution name.

    \b
    for project in $(aisland workspace projects); do git -C "$project" status --short; done
    """
    if not locations.workspace_found():
        raise AislandError(locations.missing_workspace_message())
    projects = [{"folder": p["path"].name, "name": p["name"]} for p in Workspace(get_workspace_root()).install_order]
    if as_json:
        click.echo(json.dumps({"projects": projects}, indent=2))
        return
    for project in projects:
        click.echo(project["folder"])


@workspace_group.group("provision", invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Print the rows as one JSON object.")
@click.pass_context
def workspace_provision(ctx: click.Context, as_json: bool) -> None:
    """The system tools this machine needs (Homebrew on macOS, aisland's downloads on Linux): who needs each, and
    whether it is installed. `install` installs them, as `aisland setup` does.

    \b
    aisland workspace provision
    aisland workspace provision install
    """

    if ctx.invoked_subcommand is not None:
        return
    rows = provision.rows(_configure_root())
    if as_json:
        click.echo(json.dumps({"rows": rows}, indent=2))
        return
    for line in configure.table(rows) if rows else [provision.nothing_here()]:
        click.echo(line)


@workspace_provision.command("install")
def workspace_provision_install() -> None:
    """Install every system tool this machine needs: aisland's own and the projects' sys-dependencies."""

    provision.install(_configure_root())
    if provision.this_platform() == "brew":
        # Jenkins' Java is no declared formula: the WAR names the Java it runs on (services/jenkins_java.py).
        JenkinsService().provision_java()


@workspace_group.group("harness", cls=_Group, invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Print the sets as one JSON object.")
@click.pass_context
def workspace_harness(ctx: click.Context, as_json: bool) -> None:
    """The agent harness sets the workspace combines — each one's commands, scripts, rules and other files, in the
    order workspace.toml lists them (aisland's own when it lists none). `install` installs them, as `aisland setup`
    does; `export` copies aisland's set to start one of your own.

    \b
    [harness]
    sets = ["aisland", "models", { git = "https://github.com/acme/agent-rules", ref = "v2" }]
    """

    if ctx.invoked_subcommand is not None:
        return
    rows = harness.rows(_configure_root())
    if as_json:
        click.echo(json.dumps({"sets": rows}, indent=2))
        return
    if not rows:
        click.echo("No harness sets: workspace.toml lists none ([harness] sets = []).")
        return
    for row in rows:
        click.echo(f"{row['set']}  ({row['kind']}, {row['folder']})")
        click.echo(f"    rules  {row['rules'] or '-'}")
        for place in row["files"]:
            click.echo(f"    {place}")


@workspace_harness.command("install")
def workspace_harness_install() -> None:
    """Install the harness sets into the workspace, as `aisland setup` does."""

    root = _configure_root()
    notes = harness.install_harness(root)
    click.echo(f"==> Harness: {harness.summary(harness.sets(root))}")
    for line in notes:
        click.echo(f"  - {line}")


@workspace_harness.command("export")
@click.argument("folder", type=click.Path(file_okay=False, path_type=Path))
def workspace_harness_export(folder: Path) -> None:
    """Copy aisland's own harness set into FOLDER (new or empty), to start a set of your own from it.

    \b
    aisland workspace harness export agents
    """

    copied = harness.export(folder)
    click.echo(f"==> Copied aisland's harness set into {folder} ({len(copied)} files)")
    click.echo(
        f'  - List it in workspace.toml: [harness] sets = ["{folder.name}"] (keep "aisland" to keep aisland\'s too)'
    )


@workspace_group.group("mirror")
def workspace_mirror() -> None:
    """The mirror plugin's action: copy each project to its mirror (set it with `aisland workspace configure mirror`)."""


@workspace_mirror.command("sync")
@click.argument("project", required=False)
def workspace_mirror_sync(project: Optional[str]) -> None:
    """Push the primary's dev/main, main, every other branch checked out here and tags to PROJECT's mirror.

    Default: every mirror. Each branch is copied as the primary has it, so an unpushed commit never reaches the
    mirror, and a local branch the primary lacks is reported, not pushed. Fast-forward only: a branch the mirror moved is refused, a tag that differs is never overwritten, and nothing on
    the mirror is ever deleted. Exit 1 when anything was refused or failed.
    """

    if not locations.workspace_found():
        raise AislandError(locations.missing_workspace_message())
    raise SystemExit(run_sync(project))


@workspace_group.command("bashrc")
def workspace_bashrc() -> None:
    """Link the workspace's project.bashrc to aisland's copy — the step `aisland setup` takes, alone.

    A real file in its place is refused (it may hold your own lines: put them in user.bashrc); a link pointing
    elsewhere is replaced.
    """
    root = _editor_root()
    link = root / "project.bashrc"
    if link.exists() and not link.is_symlink():
        raise AislandError(
            f"{link} is a regular file, not a link — move your lines to {root / 'user.bashrc'}, delete it, "
            "and run `aisland workspace bashrc` again"
        )
    before = os.readlink(link) if link.is_symlink() else None
    bashrc.link_project_bashrc(root)
    if before is not None and os.readlink(link) == before:
        click.echo(f"==> {link.name} already links to {before}")


@workspace_group.command("code-workspace")
def workspace_code_workspace() -> None:
    """Write the workspace's VS Code file, <workspace>.code-workspace — the step `aisland setup` takes, alone.

    Its folders follow what is checked out here, its settings the template, its theme and colours this machine's
    choice (`aisland workspace theme|color`). An unchanged file is left as it is, so VS Code does not reload.
    """
    root = _editor_root()
    if editor_files.write_code_workspace(root) is None:
        click.echo(f"==> {editor_files.code_workspace_path(root).name} is up to date")


@workspace_group.command("env-example")
@click.option(
    "--missing", is_flag=True, help="Also print the variables .env does not mention yet (.env is never edited)."
)
def workspace_env_example(missing: bool) -> None:
    """Write env.example and env.complete from each project's own: the variables a person normally sets, and all.

    Each line is commented out, so a new machine copies env.example (`cp env.example .env`) and uncomments what it
    needs. A variable several projects read is listed once, under the project that defines it, naming the others.
    An unchanged file is left as it is; `aisland setup` writes them too. --missing prints, ready to paste, the
    variables of env.complete an existing .env does not mention.
    """
    root = _editor_root()
    written = env_example.write(root)
    for kind in env_example.KINDS:
        name = env_example.FILE_NAMES[kind]
        result = written[kind]
        said = "Wrote" if result.changed else "Up to date:"
        click.echo(f"==> {said} {name} ({env_example.counts(result)})")
        for why in (f"no {name}", "no checkout"):
            names = [project for project, reason in result.without if reason == why]
            if names and (kind == "complete" or why != "no checkout"):
                click.echo(f"    {why}: {', '.join(names)}")
    if not missing:
        return
    dotenv = root / ".env"
    if not dotenv.is_file():
        click.echo(f"==> No .env yet: cp {env_example.EXAMPLE} .env")
        return
    absent = env_example.missing(root, dotenv)
    if not absent:
        click.echo("==> .env mentions every variable")
        return
    click.echo(f"==> Not in .env ({len(absent)}) — copy the lines you need:")
    project = None
    for owner, entry in absent:
        if owner != project:
            click.echo(f"# -- {owner}")
            project = owner
        click.echo(entry.line)


@workspace_group.command("env-test")
@click.argument("projects", nargs=-1, shell_complete=_complete_workspace_projects)
def workspace_env_test(projects: Tuple[str, ...]) -> None:
    """Write PROJECT's tests/test_env_example.py: it fails when the project reads a variable its env.complete lacks.

    No PROJECT: every project with a checkout. A project without an env.complete gets no test; an unchanged test is
    left as it is.
    """
    root = _editor_root()
    folders = {name: path for name, path, _active in workspace_folders(root)}
    for name in projects:
        if name not in folders:
            raise AislandError(f"{name} is no project of this workspace")
    for name in projects or tuple(folders):
        folder = root / folders[name]
        if not env_example.checked_out(folder):
            continue
        if not (folder / env_example.COMPLETE).is_file():
            click.echo(f"==> {name} has no {env_example.COMPLETE}: no test written")
            continue
        path, changed = env_example.write_test(root, name, folders[name])
        click.echo(f"==> Wrote {path}" if changed else f"==> {path} is up to date")


workspace_group.add_command(configure_cli.workspace_configure)
workspace_group.add_command(editor_cli.theme_group)
workspace_group.add_command(editor_cli.color_group)
