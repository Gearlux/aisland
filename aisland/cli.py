import json
from pathlib import Path
from typing import Optional, Tuple

import click

from aisland import nightly, pytest_stats
from aisland import supervisor as supervisor_mod
from aisland.clean import DEFAULT_KEEP_DAYS
from aisland.manager import (
    generate_gemini_settings,
    generate_mcp_json,
    get_workspace_root,
    provision_system,
    reconcile_submodules,
    run_quality_gates,
    setup_workspace,
)
from aisland.pytest_stats import DEFAULT_WINDOW_DAYS
from aisland.services.aim import DEFAULT_PORT as AIM_PORT
from aisland.services.aim import AimService
from aisland.services.clearml import DEFAULT_WEB_PORT as CLEARML_WEB_PORT
from aisland.services.clearml import ClearMLService
from aisland.services.docker import DockerService
from aisland.services.fiftyone import FiftyOneService
from aisland.services.iqengine import DEFAULT_PORT as IQENGINE_PORT
from aisland.services.iqengine import RECORDINGS_ENV_KEY as IQENGINE_RECORDINGS_KEY
from aisland.services.iqengine import IQEngineService
from aisland.services.jenkins import JenkinsService
from aisland.services.labelstudio import LabelStudioService
from aisland.services.mlflow import MLflowService
from aisland.services.swiftbar import SwiftBarService
from aisland.sources import SOURCE_KINDS


@click.group()
def main() -> None:
    """Aisland Manager: Multi-service development infrastructure."""
    pass


# --- Notebook Commands ---


@main.group()
def notebooks() -> None:
    """Manage Jupyter notebooks in the workspace."""
    pass


@notebooks.command(name="clean")
def notebooks_clean() -> None:
    """
    Clear outputs of code cells from standard input.
    Can be used as a git filter to avoid committing notebook outputs.
    """
    from aisland.notebook_utils import clean_notebook_stdin

    clean_notebook_stdin()


@main.command()
def provision() -> None:
    """Scan projects and install required system dependencies (Homebrew)."""
    provision_system()


@main.command()
@click.option(
    "--non-interactive",
    "-y",
    is_flag=True,
    help="Reuse saved activation decisions; still prompt for new/undecided optional submodules.",
)
@click.option(
    "--activate", multiple=True, help="Activate the named optional submodule(s) without prompting (repeatable)."
)
@click.option(
    "--deactivate", multiple=True, help="Deactivate the named optional submodule(s) without prompting (repeatable)."
)
@click.option(
    "--framework",
    multiple=True,
    help="ML framework(s) to install, e.g. --framework torch --framework keras (repeatable). "
    "Recorded in config.toml; omit to reuse the recorded selection.",
)
def setup(
    non_interactive: bool,
    activate: tuple[str, ...],
    deactivate: tuple[str, ...],
    framework: tuple[str, ...],
) -> None:
    """Full workspace setup: provision tools, resolve optional-submodule activation, install activated projects."""
    setup_workspace(non_interactive=non_interactive, activate=activate, deactivate=deactivate, framework=framework)


@main.command()
def reconcile() -> None:
    """Align submodule checkouts with .gitmodules `active` flags (init active, deinit inactive)."""
    reconcile_submodules(get_workspace_root())


@main.command()
def doctor() -> None:
    """Check the shared .venv and the Docker/Colima setup for known recurring skew (exit 1 if any check fails)."""
    from aisland.doctor import run_doctor

    raise SystemExit(run_doctor())


@main.command(name="clean")
@click.argument("project")
@click.option(
    "--keep-days",
    type=click.IntRange(min=0),
    default=DEFAULT_KEEP_DAYS,
    show_default=True,
    help="Delete backups older than this; the sweep runs on EVERY invocation.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be moved and swept; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def clean(project: str, keep_days: int, dry_run: bool, yes: bool) -> None:
    """Move PROJECT's git-ignored content into a dated backup under aisland/data/backups.

    Reclaims the disk a checkout accumulates (`.venv/`, `build/`, caches, run dirs) without
    the irreversibility of `git clean -Xdf`: everything is renamed into the backup store and
    only really disappears when the retention sweep expires it. ONE project per invocation —
    there is no `--all`, and the workspace root is refused.

    \b
    aisland clean sonair --dry-run     # what would move, and which backups expire
    aisland clean sonair               # move it, after confirming
    aisland clean sonair --keep-days 1 # sweep harder on a full disk
    """
    from aisland.clean import clean_project

    raise SystemExit(clean_project(project, keep_days=keep_days, dry_run=dry_run, assume_yes=yes))


@main.group()
def backups() -> None:
    """Inspect and restore the backups `aisland clean` takes.

    A separate group from `clean` on purpose: `aisland clean <project>` acts on a project's
    working tree, while these act on the STORE under `aisland/data/backups`. Keeping the
    destructive verb one word long also keeps it from ever being typed by accident.
    """
    pass


@backups.command(name="list")
@click.argument("project", required=False)
@click.option(
    "--keep-days",
    type=click.IntRange(min=0),
    default=DEFAULT_KEEP_DAYS,
    show_default=True,
    help="Retention window the EXPIRES column is computed against.",
)
def backups_list(project: Optional[str], keep_days: int) -> None:
    """Show every backup in the store (newest first), optionally just PROJECT's."""
    from aisland.clean import list_store

    raise SystemExit(list_store(project, keep_days=keep_days))


@backups.command(name="prune")
@click.option(
    "--keep-days",
    type=click.IntRange(min=0),
    default=DEFAULT_KEEP_DAYS,
    show_default=True,
    help="Delete backups older than this, for every project.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be deleted; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def backups_prune(keep_days: int, dry_run: bool, yes: bool) -> None:
    """Apply the retention window to the whole store, without cleaning anything.

    `aisland clean` already sweeps on every run; this is the same sweep on its own, for a
    workspace nobody is currently cleaning. It covers every project — the retention window is
    a property of the store, not of one project.

    \b
    aisland backups prune --dry-run    # what has expired
    aisland backups prune              # delete it (asks first)
    aisland backups prune --keep-days 1
    """
    from aisland.clean import prune_store

    raise SystemExit(prune_store(keep_days=keep_days, dry_run=dry_run, assume_yes=yes))


@backups.command(name="restore")
@click.argument("project")
@click.option("--stamp", default=None, help="Backup to restore from  [default: the most recent]")
@click.option("--entry", "entries", multiple=True, help="Restore only this path (repeatable).")
@click.option("--force", is_flag=True, help="Replace a path the project already has, instead of skipping it.")
@click.option("--dry-run", is_flag=True, help="Show what would be restored; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation --force asks for.")
def backups_restore(
    project: str,
    stamp: Optional[str],
    entries: tuple[str, ...],
    force: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Move a backup's content back into PROJECT.

    Moves rather than copies — the disk was full enough to run a clean, so a second copy is
    the one thing that may not fit. The backup is consumed as it is restored: its manifest is
    rewritten and an emptied backup is removed. A path the project already has is SKIPPED
    (exit 1) unless `--force` replaces it.

    \b
    aisland backups restore sonair                       # the most recent backup, whole
    aisland backups restore sonair --stamp 20260804-164945
    aisland backups restore sonair --entry checkpoints   # just this one, repeatable
    aisland backups restore sonair --force               # overwrite what came back on its own
    """
    from aisland.clean import restore_backup

    raise SystemExit(restore_backup(project, stamp=stamp, only=entries, force=force, dry_run=dry_run, assume_yes=yes))


# --- Test-Run Statistics ---
#
# Root AGENTS.md "Two Test Runs": the fast/slow split is decided from these records, never by
# guess. The pytest plugin in `aisland/pytest_stats.py` writes them; this group reads them.


@main.group()
def tests() -> None:
    """Every pytest run in the workspace, recorded — read back per project."""


@tests.command(name="report")
@click.argument("project")
@click.option(
    "--days",
    type=click.IntRange(min=1),
    default=DEFAULT_WINDOW_DAYS,
    show_default=True,
    help="How far back to read the local runs and the Jenkins builds.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON (what an MCP tool would return).")
def tests_report(project: str, days: int, as_json: bool) -> None:
    """PROJECT's fast run against its 5-minute budget, its slowest tests, the tests that failed, and what to move.

    Reads the local runs (`aisland/data/test-stats/PROJECT/`) and the local Jenkins builds'
    JUnit results. A fast run over budget lists the never-failed tests to mark `slow`; a slow
    test that failed is listed for the fast run.

    \b
    aisland tests report streamstudio
    aisland tests report signals-wlan --days 90
    aisland tests report raidar --json
    """
    if not (pytest_stats.WORKSPACE_ROOT / project / "pyproject.toml").is_file():
        raise click.BadParameter(
            f"no project {project!r} in the workspace {pytest_stats.WORKSPACE_ROOT}", param_hint="PROJECT"
        )
    report = pytest_stats.build_report(project, days=days)
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else pytest_stats.render_report(report))


@tests.command(name="nightly")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON (what an MCP tool would return).")
@click.option("--no-notify", "no_notify", is_flag=True, help="Write the report but show no desktop notification.")
def tests_nightly(as_json: bool, no_notify: bool) -> None:
    """Report the local Jenkins' nightly runs since the previous report; exit 1 when one failed or none ran.

    Reads every 01:00 run of the orchestrator (`<workspace>.local`) started since the previous
    report, writes `aisland/data/nightly/<date>.md` — each project's result, a failed one's stage,
    tests and error lines — and shows a desktop notification when a night failed, had no run, or
    is still building. The `<workspace>.nightly-check` Jenkins job runs it each weekday at 09:30.

    \b
    aisland tests nightly
    aisland tests nightly --json --no-notify
    """
    report, status = nightly.run_nightly(notify=not no_notify)
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else nightly.render_markdown(report))
    raise SystemExit(status)


# --- Workspace Configuration Commands ---
#
# `config.toml` is maintained by `aisland source/framework/tracker set` — this group only
# INSPECTS it and completes the one-time migration from the per-concern files it replaced.


@main.group()
def config() -> None:
    """Inspect the workspace's single committed configuration file (`config.toml`).

    One tracked file at the workspace root holds every shared decision: the `frameworks` and
    `trackers` selections and the `[sources.*]` registry. It is written by `aisland framework
    set` / `aisland tracker set` / `aisland source set`; per-machine deviations stay in
    `.git/config` and never reach it.

    The workspace-root `uv.toml` is NOT part of it — uv only reads a file of its own settings,
    so that one stays generated by `aisland setup` and gitignored.
    """
    pass


@config.command(name="show")
def config_show() -> None:
    """Print the workspace configuration file."""
    from aisland.config import CONFIG_FILE, config_path

    path = config_path(get_workspace_root())
    if not path.exists():
        click.echo(f"No {CONFIG_FILE} at {path.parent} — nothing is configured yet.")
        click.echo("Create it with: aisland framework set <name>... | aisland source set <project> <kind>")
        return
    click.echo(click.style(str(path), bold=True))
    click.echo(path.read_text())


@config.command(name="migrate")
def config_migrate() -> None:
    """Fold any legacy sources/frameworks/trackers.toml into `config.toml` (idempotent)."""
    from aisland.config import CONFIG_FILE, migrate

    folded = migrate(get_workspace_root())
    if not folded:
        click.echo(f"Nothing to migrate — no legacy file beside {CONFIG_FILE}.")
        return
    click.echo(f"==> Folded {', '.join(folded)} into {CONFIG_FILE}. Commit the change.")


# --- Project Source Commands ---


@main.group()
def source() -> None:
    """Choose where each workspace project's package comes from.

    \b
    local   checked out on --ref, installed editable  (you develop it here)
    github  no checkout, installed from the remote at --ref
    pypi    no checkout, installed from the published wheel

    `github` and `pypi` both deinit the submodule (empty directory) and drop it from the
    *.code-workspace folder list. Both layers of configuration — the committed `[sources.*]`
    tables of `config.toml` and the per-machine `.git/config` override — are honoured.
    """
    pass


@source.command(name="list")
def source_list() -> None:
    """Show the committed source, the per-machine override, and the on-disk state per project."""
    from aisland.sources import describe

    rows = describe()
    if not rows:
        click.echo("No project sources configured — every project is unmanaged (checkout + editable install).")
        click.echo("Set one with: aisland source set <project> pypi")
        return
    header = f"  {'PROJECT':<22} {'COMMITTED':<16} {'OVERRIDE':<16} {'EFFECTIVE':<16} STATE"
    click.echo(click.style(header, bold=True))
    for row in rows:
        click.echo(
            f"  {row['project']:<22} {row['committed']:<16} {row['override']:<16} "
            f"{row['effective']:<16} {row['state']}"
        )


@source.command(name="set")
@click.argument("project")
@click.argument("kind", type=click.Choice(SOURCE_KINDS))
@click.option("--ref", default=None, help="Branch or tag for a `local` / `github` source (default: main).")
@click.option("--version", default=None, help="PEP 440 specifier for a `pypi` source (default: latest).")
@click.option(
    "--machine-only",
    is_flag=True,
    help="Record only the per-machine .git/config override; leave config.toml (and the "
    "editor folder list it drives) untouched.",
)
def source_set(
    project: str,
    kind: str,
    ref: Optional[str],
    version: Optional[str],
    machine_only: bool,
) -> None:
    """Set PROJECT's source to KIND and apply it (checkout, venv, editor folder list).

    No CI file changes: a runner installs a RELEASED dependency from PyPI and an unreleased one
    from GitHub `main`, whatever the source here (`aisland jenkins scaffold` asks PyPI).

    \b
    aisland source set loggair local  --ref dev/main  # develop it here, on dev/main
    aisland source set loggair github --ref dev/main  # pin to that branch, no checkout
    aisland source set loggair pypi                   # consume the published wheel
    aisland source set loggair pypi --machine-only    # this machine only; nothing committed
    """
    from aisland.sources import set_project_source

    raise SystemExit(
        set_project_source(
            project=project,
            kind=kind,
            ref=ref,
            version=version,
            machine_only=machine_only,
        )
    )


@source.command(name="unset")
@click.argument("project")
@click.option("--machine-only", is_flag=True, help="Clear only this machine's override; keep the committed source.")
def source_unset(project: str, machine_only: bool) -> None:
    """Return PROJECT to unmanaged (historical behaviour: checkout + editable install).

    The checkout and the venv are left as they are — unmanaged means aisland holds no opinion,
    not that the current state is wrong. Use `aisland source set <project> local` to actively
    restore a checkout.
    """
    from aisland.sources import unset_project_source

    raise SystemExit(unset_project_source(project=project, machine_only=machine_only))


@source.command(name="apply")
@click.argument("project", required=False)
def source_apply(project: Optional[str]) -> None:
    """Re-apply the configured source(s) without changing the configuration (idempotent)."""
    from aisland.sources import apply_configured

    raise SystemExit(apply_configured(project))


# --- Framework Selection Commands ---


@main.group()
def framework() -> None:
    """Choose which ML frameworks this workspace installs.

    Workspace packages keep their core framework-free and put each framework behind an EXTRA
    (`recordstream[torch]`, `sonair[lightning]`/`[keras]`). A project declares which of its
    extras are frameworks in `[tool.aisland] frameworks`; this command selects among them, and
    `aisland setup` installs the intersection.

    Both layers are honoured: the committed `frameworks` key of `config.toml` (shared, and
    the set generated CI installs) and a per-machine `.git/config aisland.frameworks` override.
    """
    pass


@framework.command(name="list")
def framework_list() -> None:
    """Show each declared framework, which projects offer it, and whether it is selected."""
    from aisland.frameworks import CONFIG_FILE, FRAMEWORKS_KEY, describe

    rows = describe()
    if not rows:
        click.echo("No project declares framework extras ([tool.aisland] frameworks).")
        return
    header = f"  {'FRAMEWORK':<14} {'COMMITTED':<11} {'OVERRIDE':<10} {'EFFECTIVE':<11} PROJECTS"
    click.echo(click.style(header, bold=True))
    for row in rows:
        click.echo(
            f"  {row['framework']:<14} {row['committed']:<11} {row['override']:<10} "
            f"{row['effective']:<11} {row['projects']}"
        )
    click.echo(
        f"\n  '-' = no selection recorded in that layer ({CONFIG_FILE} has no `{FRAMEWORKS_KEY}` key, or no override)."
    )

    # Print the commands that CHANGE this, using real framework names so they are copy-pasteable.
    # `--machine-only` is a FLAG on set/unset, never a command of its own — showing it detached
    # from its verb sent an operator hunting for a subcommand that does not exist.
    example = " ".join(row["framework"] for row in rows[:2])
    commands = [
        (f"aisland framework set {example}", f"shared: writes {CONFIG_FILE}; CI installs it"),
        (f"aisland framework set {example} --machine-only", "this machine only (.git/config)"),
        ("aisland framework unset --machine-only", "drop this machine's override"),
        (f"aisland framework set {example} --no-apply", "record it without reinstalling"),
    ]
    width = max(len(cmd) for cmd, _ in commands)
    click.echo("\n  Change it (--machine-only / --no-apply are FLAGS on set/unset, not commands):")
    for cmd, note in commands:
        click.echo(f"    {cmd:<{width}}  # {note}")


@framework.command(name="set")
@click.argument("names", nargs=-1, required=True)
@click.option(
    "--machine-only",
    is_flag=True,
    help="Record only the per-machine .git/config override; leave config.toml "
    "(and the set generated CI installs) untouched.",
)
@click.option("--no-apply", is_flag=True, help="Record the selection without reinstalling anything.")
def framework_set(names: tuple[str, ...], machine_only: bool, no_apply: bool) -> None:
    """Select FRAMEWORKS and reinstall the projects that declare them.

    \b
    aisland framework set torch lightning        # a torch-only machine
    aisland framework set lightning keras        # both backends
    aisland framework set keras --machine-only   # this machine only; nothing committed
    """
    from aisland.frameworks import set_frameworks

    raise SystemExit(set_frameworks(names=names, machine_only=machine_only, apply=not no_apply))


@framework.command(name="unset")
@click.option("--machine-only", is_flag=True, help="Clear only this machine's override; keep the committed selection.")
@click.option("--no-apply", is_flag=True, help="Clear the selection without reinstalling anything.")
def framework_unset(machine_only: bool, no_apply: bool) -> None:
    """Clear the framework selection; the next `aisland setup` prompts again."""
    from aisland.frameworks import unset_frameworks

    raise SystemExit(unset_frameworks(machine_only=machine_only, apply=not no_apply))


# --- Tracker Selection Commands ---
#
# The sibling of `framework`, over the same machinery (aisland/selection.py) with the
# TRACKERS axis. A separate command rather than more names under `framework` because the
# axes are independent: what a machine TRAINS WITH and where its results GO are unrelated,
# and merging them would install a tracking client because someone picked a training engine.


@main.group()
def tracker() -> None:
    """Choose which experiment-tracking backends this workspace installs.

    Tracking backends live behind extras (`matrainer[mlflow]`/`[aim]`/`[tensorboard]`/`[wandb]`)
    and each imports its library lazily, so code runs with none of them installed — the
    local-file backend always works. A project declares which of its extras are trackers in
    `[tool.aisland] trackers`; this command selects among them, and `aisland setup` installs
    the intersection.

    Both layers are honoured: the committed `trackers` key of `config.toml` (shared, and the
    set generated CI installs) and a per-machine `.git/config aisland.trackers` override.
    """
    pass


@tracker.command(name="list")
def tracker_list() -> None:
    """Show each declared tracker, which projects offer it, and whether it is selected."""
    from aisland.config import CONFIG_FILE
    from aisland.selection import TRACKERS, describe

    rows = describe(axis=TRACKERS)
    if not rows:
        click.echo("No project declares tracker extras ([tool.aisland] trackers).")
        return
    header = f"  {'TRACKER':<14} {'COMMITTED':<11} {'OVERRIDE':<10} {'EFFECTIVE':<11} PROJECTS"
    click.echo(click.style(header, bold=True))
    for row in rows:
        click.echo(
            f"  {row['tracker']:<14} {row['committed']:<11} {row['override']:<10} "
            f"{row['effective']:<11} {row['projects']}"
        )
    click.echo(
        f"\n  '-' = no selection recorded in that layer ({CONFIG_FILE} has no `{TRACKERS.plural}` key, or no override)."
    )

    example = " ".join(row["tracker"] for row in rows[:2])
    commands = [
        (f"aisland tracker set {example}", f"shared: writes {CONFIG_FILE}; CI installs it"),
        (f"aisland tracker set {example} --machine-only", "this machine only (.git/config)"),
        ("aisland tracker unset --machine-only", "drop this machine's override"),
        (f"aisland tracker set {example} --no-apply", "record it without reinstalling"),
    ]
    width = max(len(cmd) for cmd, _ in commands)
    click.echo("\n  Change it (--machine-only / --no-apply are FLAGS on set/unset, not commands):")
    for cmd, note in commands:
        click.echo(f"    {cmd:<{width}}  # {note}")


@tracker.command(name="set")
@click.argument("names", nargs=-1, required=True)
@click.option(
    "--machine-only",
    is_flag=True,
    help="Record only the per-machine .git/config override; leave config.toml "
    "(and the set generated CI installs) untouched.",
)
@click.option("--no-apply", is_flag=True, help="Record the selection without reinstalling anything.")
def tracker_set(names: tuple[str, ...], machine_only: bool, no_apply: bool) -> None:
    """Select TRACKERS and reinstall the projects that declare them.

    \b
    aisland tracker set mlflow                  # a machine that only uses the MLflow server
    aisland tracker set aim tensorboard         # local-only, no server to operate
    aisland tracker set mlflow --machine-only   # this machine only; nothing committed
    """
    from aisland.selection import TRACKERS, set_selection

    raise SystemExit(set_selection(names=names, axis=TRACKERS, machine_only=machine_only, apply=not no_apply))


@tracker.command(name="unset")
@click.option("--machine-only", is_flag=True, help="Clear only this machine's override; keep the committed selection.")
@click.option("--no-apply", is_flag=True, help="Clear the selection without reinstalling anything.")
def tracker_unset(machine_only: bool, no_apply: bool) -> None:
    """Clear the tracker selection; the next `aisland setup` prompts again."""
    from aisland.selection import TRACKERS, unset_selection

    raise SystemExit(unset_selection(axis=TRACKERS, machine_only=machine_only, apply=not no_apply))


# --- Docker Commands ---


@main.group()
def docker() -> None:
    """Manage local Docker container engine (via Colima)."""
    pass


_MOUNT_HELP = (
    "Share a host directory with the Colima VM so containers can bind-mount it: DIR (read-only) "
    "or DIR:w (writable); repeatable. $HOME is always kept. Colima shares ONLY $HOME by default, "
    'so an external volume needs this — e.g. --mount "$DATA_ROOT:r". Applies at VM boot.'
)


@docker.command(name="start")
@click.option("--mount", "mounts", multiple=True, metavar="DIR[:w]", help=_MOUNT_HELP)
def docker_start(mounts: Tuple[str, ...]) -> None:
    """Start Docker (optionally sharing extra host directories with the VM)."""
    DockerService().launch(mounts=mounts)


@docker.command(name="stop")
def docker_stop() -> None:
    """Stop Docker."""
    DockerService().stop()


@docker.command(name="status")
def docker_status() -> None:
    """Show Docker status."""
    DockerService().status()


@docker.command(name="info")
def docker_info() -> None:
    """Show detailed Docker configuration (socket paths, colima status)."""
    DockerService().info()


@docker.command(name="restart")
@click.option("--mount", "mounts", multiple=True, metavar="DIR[:w]", help=_MOUNT_HELP)
def docker_restart(mounts: Tuple[str, ...]) -> None:
    """Restart Docker (the way to change VM mounts on a running engine)."""
    DockerService().restart(mounts=mounts)


@docker.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe all Docker data?")
def docker_reset() -> None:
    """Stop Docker and wipe all its data."""
    DockerService().reset()


# --- Jenkins Commands ---


@main.group()
def jenkins() -> None:
    """Manage local Jenkins CI/CD server."""
    pass


@jenkins.command(name="start")
@click.option("--port", default=9080, help="HTTP port for Jenkins.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
def jenkins_start(port: int, background: bool) -> None:
    """Start Jenkins."""
    JenkinsService().launch(port=port, background=background)


@jenkins.command(name="stop")
def jenkins_stop() -> None:
    """Stop Jenkins."""
    JenkinsService().stop()


@jenkins.command(name="status")
def jenkins_status() -> None:
    """Show Jenkins status."""
    JenkinsService().status()


@jenkins.command(name="info")
def jenkins_info() -> None:
    """Show detailed Jenkins configuration (URL, JENKINS_HOME, admin password)."""
    JenkinsService().info()


@jenkins.command(name="restart")
@click.option("--port", default=9080, help="HTTP port for Jenkins.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
def jenkins_restart(port: int, background: bool) -> None:
    """Restart Jenkins."""
    JenkinsService().restart(port=port, background=background)


@jenkins.command(name="reset")
@click.option("--project", help="Only reset data for a specific project pipeline.")
@click.confirmation_option(prompt="Are you sure you want to wipe data?")
def jenkins_reset(project: Optional[str]) -> None:
    """Stop Jenkins and wipe its data (or just a specific project's data)."""
    if project:
        JenkinsService().remove_pipelines(project_name=project)
    else:
        JenkinsService().reset()


@jenkins.command(name="init")
@click.argument("project", required=False)
@click.option(
    "--local",
    is_flag=True,
    help="Initialize pipeline using local filesystem path and Jenkinsfile.local.",
)
def jenkins_init(project: Optional[str], local: bool) -> None:
    """Initialize Jenkins with Multi-branch pipelines for a project or all projects."""
    JenkinsService().init_pipelines(project_name=project, local=local)


@jenkins.command(name="scaffold")
@click.argument("project", required=False)
@click.option("--force", "-f", is_flag=True, help="Overwrite existing files.")
def jenkins_scaffold(project: Optional[str], force: bool) -> None:
    """Generate standard Jenkinsfile, Jenkinsfile.local, and GitHub Actions for a project or all projects."""
    JenkinsService().scaffold_pipelines(project_name=project, force=force)


@jenkins.command(name="remove")
@click.argument("project", required=False)
@click.confirmation_option(prompt="Are you sure you want to remove the pipeline(s)?")
def jenkins_remove(project: Optional[str]) -> None:
    """Remove a specific project pipeline or all pipelines from Jenkins."""
    JenkinsService().remove_pipelines(project_name=project)


# --- MCP Commands ---


@main.group()
def mcp() -> None:
    """Manage MCP client configuration for workspace MCP servers."""
    pass


@mcp.command(name="gemini")
def mcp_gemini() -> None:
    """Generate .gemini/settings.json for the workspace MCP servers (gitignored; regenerated)."""
    generate_gemini_settings()


@mcp.command(name="claude")
def mcp_claude() -> None:
    """Generate .mcp.json (Claude Code's project servers) for the workspace MCP servers (gitignored; regenerated)."""
    generate_mcp_json()


# --- MLflow Commands ---


@main.group()
def mlflow() -> None:
    """Manage local MLflow tracking server."""
    pass


@mlflow.command(name="start")
@click.option("--port", default=5005, help="HTTP port for MLflow.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
def mlflow_start(port: int, background: bool) -> None:
    """Start MLflow."""
    MLflowService().launch(port=port, background=background)


@mlflow.command(name="stop")
def mlflow_stop() -> None:
    """Stop MLflow."""
    MLflowService().stop()


@mlflow.command(name="status")
def mlflow_status() -> None:
    """Show MLflow status."""
    MLflowService().status()


@mlflow.command(name="info")
def mlflow_info() -> None:
    """Show detailed MLflow configuration (URL, backend store, artifacts)."""
    MLflowService().info()


@mlflow.command(name="restart")
@click.option("--port", default=5005, help="HTTP port for MLflow.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
def mlflow_restart(port: int, background: bool) -> None:
    """Restart MLflow."""
    MLflowService().restart(port=port, background=background)


@mlflow.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe all MLflow data?")
def mlflow_reset() -> None:
    """Stop MLflow and wipe all its data."""
    MLflowService().reset()


@mlflow.command(name="ui")
@click.argument("db_file")
@click.option("--port", default=5000, help="HTTP port for MLflow UI.")
def mlflow_ui(db_file: str, port: int) -> None:
    """Run local MLflow UI with the given database file."""
    MLflowService().ui(db_path=db_file, port=port)


# --- Aim Commands ---


@main.group()
def aim() -> None:
    """Serve the Aim experiment-tracking UI from a local repository.

    Aim needs no server infrastructure — runs live in an `.aim` repository directory and
    `aim up` serves the comparison UI from it. The repository path is published to the
    workspace `.env` as `AIM_REPO`, so a run config points its tracker at `$AIM_REPO` and
    what a run writes is what this UI shows.

    Requires the `aim` backend: `aisland tracker set aim`.
    """
    pass


@aim.command(name="start")
@click.option("--port", default=AIM_PORT, help="HTTP port for the Aim UI.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
@click.option("--repo", default=None, help="Aim repository to serve (default: the workspace one).")
def aim_start(port: int, background: bool, repo: Optional[str]) -> None:
    """Start the Aim UI."""
    AimService().launch(port=port, background=background, repo=repo)


@aim.command(name="stop")
def aim_stop() -> None:
    """Stop the Aim UI."""
    AimService().stop()


@aim.command(name="status")
def aim_status() -> None:
    """Show Aim UI status."""
    AimService().status()


@aim.command(name="info")
def aim_info() -> None:
    """Show detailed Aim configuration (URL, repository path)."""
    AimService().info()


@aim.command(name="restart")
@click.option("--port", default=AIM_PORT, help="HTTP port for the Aim UI.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
def aim_restart(port: int, background: bool) -> None:
    """Restart the Aim UI."""
    AimService().restart(port=port, background=background)


@aim.command(name="reset")
def aim_reset() -> None:
    """Delete the Aim repository and its logs."""
    AimService().reset()


# --- ClearML Commands ---


@main.group()
def clearml() -> None:
    """Run the ClearML server (upstream's docker-compose stack, workspace-local state).

    Seven containers, wired by upstream's compose file at a pinned release and verified by
    checksum; aisland layers a generated override that moves every host bind mount out of
    `/opt/clearml` into `aisland/data/clearml/state`, so a service's data stays inside the
    workspace.

    Requires the `clearml` backend (`aisland tracker set clearml`) and a running Docker
    daemon (`aisland docker start`). For tracking WITHOUT a server, `ClearMLTracker(offline=True)`
    records to local disk and needs neither.
    """
    pass


@clearml.command(name="start")
@click.option("--port", default=CLEARML_WEB_PORT, help="HTTP port for the ClearML web UI.")
def clearml_start(port: int) -> None:
    """Start the ClearML server stack (detached)."""
    ClearMLService().launch(port=port)


@clearml.command(name="stop")
def clearml_stop() -> None:
    """Stop the ClearML server stack."""
    ClearMLService().stop()


@clearml.command(name="status")
def clearml_status() -> None:
    """Show the ClearML containers."""
    ClearMLService().status()


@clearml.command(name="info")
def clearml_info() -> None:
    """Show detailed ClearML configuration (pinned version, compose files, state dir)."""
    ClearMLService().info()


@clearml.command(name="reset")
def clearml_reset() -> None:
    """Tear the stack down and delete its state, volumes and compose files."""
    ClearMLService().reset()


# --- IQEngine Commands ---


@main.group()
def iqengine() -> None:
    """Run the IQEngine SigMF viewer over a local directory of recordings.

    One pinned container (image digest checked by Docker), the directory bind-mounted
    READ-ONLY at the path upstream documents, no database (the index is rebuilt from the
    directory at every start). Open http://localhost:3000 -> "Local to Backend" to browse the
    recordings, their annotation counts, and the spectrogram with the annotation boxes drawn.

    Every `.sigmf-meta` needs its `<name>.sigmf-data` beside it — IQEngine skips the rest —
    and the Docker VM must share the directory (`aisland docker start --mount DIR`); `start`
    diagnoses both instead of showing an empty table. Requires a running Docker daemon
    (`aisland docker start`).
    """
    pass


@iqengine.command(name="start")
@click.option(
    "--recordings",
    default=None,
    metavar="DIR",
    help=f"Directory of SigMF recordings to serve (default: ${IQENGINE_RECORDINGS_KEY} from .env, "
    "else aisland/data/iqengine/recordings). Remembered in .env.",
)
@click.option("--port", default=IQENGINE_PORT, show_default=True, help="Host port for the IQEngine web UI.")
def iqengine_start(recordings: Optional[str], port: int) -> None:
    """Start the viewer (detached) over a recordings directory."""
    IQEngineService().launch(recordings=recordings, port=port)


@iqengine.command(name="stop")
def iqengine_stop() -> None:
    """Stop and remove the viewer container (the recordings are untouched)."""
    IQEngineService().stop()


@iqengine.command(name="status")
def iqengine_status() -> None:
    """Show whether the viewer container is running."""
    IQEngineService().status()


@iqengine.command(name="info")
def iqengine_info() -> None:
    """Show the pinned image, the served directory and the URL."""
    IQEngineService().info()


@iqengine.command(name="reset")
def iqengine_reset() -> None:
    """Remove the container and the pinned image (re-pulled on next start); keeps the recordings."""
    IQEngineService().reset()


# --- Label Studio Commands ---


@main.group()
def labelstudio() -> None:
    """Manage local Label Studio annotation server."""
    pass


@labelstudio.command(name="start")
@click.option("--port", default=9081, help="HTTP port for Label Studio.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
@click.option(
    "--username",
    default=None,
    help="Username for default user (defaults to git email).",
)
@click.option(
    "--password",
    default=None,
    help="Password for default user (defaults to workspace name).",
)
@click.option(
    "--token",
    default=None,
    help="User token for API access (defaults to <workspace>_token_<hex>).",
)
def labelstudio_start(
    port: int,
    background: bool,
    username: Optional[str],
    password: Optional[str],
    token: Optional[str],
) -> None:
    """Start Label Studio."""
    LabelStudioService().launch(
        port=port,
        background=background,
        username=username,
        password=password,
        user_token=token,
    )


@labelstudio.command(name="stop")
def labelstudio_stop() -> None:
    """Stop Label Studio."""
    LabelStudioService().stop()


@labelstudio.command(name="status")
def labelstudio_status() -> None:
    """Show Label Studio status."""
    LabelStudioService().status()


@labelstudio.command(name="info")
def labelstudio_info() -> None:
    """Show detailed Label Studio configuration (URL, credentials, API token)."""
    LabelStudioService().info()


@labelstudio.command(name="restart")
@click.option("--port", default=9081, help="HTTP port for Label Studio.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
@click.option(
    "--username",
    default=None,
    help="Username for default user (defaults to git email).",
)
@click.option(
    "--password",
    default=None,
    help="Password for default user (defaults to workspace name).",
)
@click.option(
    "--token",
    default=None,
    help="User token for API access (defaults to <workspace>_token_<hex>).",
)
def labelstudio_restart(
    port: int,
    background: bool,
    username: Optional[str],
    password: Optional[str],
    token: Optional[str],
) -> None:
    """Restart Label Studio."""
    LabelStudioService().restart(
        port=port,
        background=background,
        username=username,
        password=password,
        user_token=token,
    )


@labelstudio.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe all Label Studio data?")
def labelstudio_reset() -> None:
    """Stop Label Studio and wipe all its data."""
    LabelStudioService().reset()


# --- FiftyOne Commands ---


@main.group()
def fiftyone() -> None:
    """Manage local FiftyOne (voxel51) app for visual dataset/annotation review."""
    pass


@fiftyone.command(name="start")
@click.option("--port", default=5151, help="HTTP port for FiftyOne.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
def fiftyone_start(port: int, background: bool) -> None:
    """Start FiftyOne."""
    FiftyOneService().launch(port=port, background=background)


@fiftyone.command(name="stop")
def fiftyone_stop() -> None:
    """Stop FiftyOne."""
    FiftyOneService().stop()


@fiftyone.command(name="status")
def fiftyone_status() -> None:
    """Show FiftyOne status."""
    FiftyOneService().status()


@fiftyone.command(name="info")
def fiftyone_info() -> None:
    """Show detailed FiftyOne configuration (URL, database dir, registered datasets)."""
    FiftyOneService().info()


@fiftyone.command(name="restart")
@click.option("--port", default=5151, help="HTTP port for FiftyOne.")
@click.option("--background", "-b", is_flag=True, help="Run in background.")
def fiftyone_restart(port: int, background: bool) -> None:
    """Restart FiftyOne."""
    FiftyOneService().restart(port=port, background=background)


@fiftyone.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe all FiftyOne data?")
def fiftyone_reset() -> None:
    """Stop FiftyOne and wipe all its data."""
    FiftyOneService().reset()


# --- SwiftBar Commands ---


@main.group()
def swiftbar() -> None:
    """Manage SwiftBar menu bar plugins."""
    pass


@swiftbar.command(name="init")
def swiftbar_init() -> None:
    """Initialize SwiftBar scripts for monitoring workspace projects."""
    SwiftBarService().init_scripts()


@swiftbar.command(name="start")
def swiftbar_start() -> None:
    """Start SwiftBar application."""
    SwiftBarService().launch()


@swiftbar.command(name="stop")
def swiftbar_stop() -> None:
    """Stop SwiftBar application."""
    SwiftBarService().stop()


@swiftbar.command(name="restart")
def swiftbar_restart() -> None:
    """Restart SwiftBar application."""
    SwiftBarService().restart()


@swiftbar.command(name="remove")
def swiftbar_remove() -> None:
    """Unlink workspace scripts from SwiftBar."""
    SwiftBarService().remove()


@swiftbar.command(name="info")
def swiftbar_info() -> None:
    """Show detailed SwiftBar configuration (plugins dir, registered scripts)."""
    SwiftBarService().info()


@swiftbar.command(name="reset")
def swiftbar_reset() -> None:
    """Alias for remove."""
    SwiftBarService().reset()


# --- Global Commands ---


@main.command()
def status() -> None:
    """Show status of all aisland services."""
    click.echo("Aisland Infrastructure Status:")
    supervisor_mod.print_status()
    DockerService().status()
    JenkinsService().status()
    MLflowService().status()
    LabelStudioService().status()
    FiftyOneService().status()
    SwiftBarService().status()


# --- Supervisor Commands ---


@main.group()
def supervisor() -> None:
    """Manage the process-compose supervisor backing aisland services."""
    pass


@supervisor.command(name="status")
def supervisor_status() -> None:
    """Show the supervisor state and its process table."""
    supervisor_mod.print_status()


@supervisor.command(name="attach")
def supervisor_attach() -> None:
    """Attach the process-compose TUI to the running supervisor."""
    supervisor_mod.attach()


@supervisor.command(name="down")
@click.confirmation_option(prompt="Stop ALL supervised services and the supervisor itself?")
def supervisor_down() -> None:
    """Stop every supervised service and the supervisor process."""
    supervisor_mod.shutdown_server()


@main.command(name="info")
def info() -> None:
    """Show detailed info (URLs, credentials, paths) for all aisland services."""
    click.echo("Aisland Infrastructure Info:")
    DockerService().info()
    JenkinsService().info()
    MLflowService().info()
    LabelStudioService().info()
    FiftyOneService().info()
    SwiftBarService().info()


@main.command(name="start")
@click.option("--background", "-b", is_flag=True, help="Run all in background.")
def start(background: bool) -> None:
    """Start all aisland services."""
    click.echo("Starting all services...")
    DockerService().launch()
    JenkinsService().launch(background=background)
    MLflowService().launch(background=background)
    LabelStudioService().launch(background=background)
    FiftyOneService().launch(background=background)
    SwiftBarService().launch()


@main.command(name="stop")
def stop() -> None:
    """Stop all aisland services."""
    click.echo("Stopping all services...")
    JenkinsService().stop()
    MLflowService().stop()
    LabelStudioService().stop()
    FiftyOneService().stop()
    SwiftBarService().stop()
    DockerService().stop()


@main.command(name="restart")
@click.option("--background", "-b", is_flag=True, help="Run all in background.")
def restart(background: bool) -> None:
    """Restart all aisland services."""
    click.echo("Restarting all services...")
    DockerService().restart()
    JenkinsService().restart(background=background)
    MLflowService().restart(background=background)
    LabelStudioService().restart(background=background)
    FiftyOneService().restart(background=background)
    SwiftBarService().restart()


@main.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe ALL aisland data?")
def reset() -> None:
    """Stop all services and wipe all data."""
    click.echo("Resetting all services...")
    JenkinsService().reset()
    MLflowService().reset()
    LabelStudioService().reset()
    FiftyOneService().reset()
    SwiftBarService().reset()
    DockerService().reset()


# --- Completion Commands ---


@main.group()
def completion() -> None:
    """Install shell completion for every CLI in the workspace."""
    pass


@completion.command(name="install")
@click.option(
    "--target-rc",
    type=click.Path(path_type=Path),
    default=None,
    help="Rc file to write into. Default: the workspace `.project.bashrc.completion`, which "
    "project.bashrc sources. Pass ~/.bashrc (or project.bashrc) to install elsewhere.",
)
@click.option("--shell", type=click.Choice(["bash", "zsh", "fish"]), default=None, help="Default: detect from $SHELL.")
@click.option("--no-prune", is_flag=True, help="Keep blocks for programs that are no longer installed.")
def completion_install(target_rc: Optional[Path], shell: Optional[str], no_prune: bool) -> None:
    """Install completion for every workspace CLI — liquifai apps, click apps, argparse apps.

    Idempotent: every block is marker-delimited and rewritten in place, so re-run this after
    installing a new project rather than re-running the whole of setup.sh.
    """
    from aisland.completion import install_completions, is_sourced_by, shell_snippet, verify_click_completion

    report = install_completions(target_rc=target_rc, shell=shell, prune=not no_prune)

    click.echo(f"==> {report.rc} ({report.shell})")
    click.echo(f"    liquifai apps: {', '.join(report.liquifai_apps) or '(none discovered)'}")
    click.echo(f"    other CLIs:    {', '.join(report.non_liquifai) or '(none found)'}")
    if report.pruned:
        click.echo(f"    pruned stale:  {', '.join(report.pruned)}")
    if not verify_click_completion("aisland"):
        click.echo("    WARNING: `aisland` emitted no completion script — its block will be inert.")

    # The fragment only does anything once something sources it. Say so, but only when it
    # is actually unsourced — this is a hint, not a nag on every re-run.
    root = report.rc.parent
    if not any(is_sourced_by(report.rc, root / name) for name in ("project.bashrc", ".bashrc", ".zshrc")):
        if not is_sourced_by(report.rc, Path.home() / f".{report.shell}rc"):
            click.echo(f"\n    Not sourced yet — add to your shell rc:\n      {shell_snippet(report.rc)}")
    click.echo(f"\n    Open a new shell (or `exec {report.shell}`) to pick it up.")


@main.command(name="check")
@click.argument("arg1", required=False)
@click.argument("arg2", required=False)
def check(arg1: Optional[str], arg2: Optional[str]) -> None:
    """
    Run quality gates.
    aisland check                 -> All projects, all tools
    aisland check confluid         -> All tools for confluid
    aisland check pytest          -> pytest for all projects
    aisland check pytest confluid -> pytest for confluid
    """
    run_quality_gates(arg1=arg1, arg2=arg2)


if __name__ == "__main__":
    main()
