from pathlib import Path
from typing import Optional

import click

from aisland import supervisor as supervisor_mod
from aisland.manager import (
    generate_gemini_settings,
    get_workspace_root,
    provision_system,
    reconcile_submodules,
    run_quality_gates,
    setup_workspace,
)
from aisland.services.aim import DEFAULT_PORT as AIM_PORT
from aisland.services.aim import AimService
from aisland.services.clearml import DEFAULT_WEB_PORT as CLEARML_WEB_PORT
from aisland.services.clearml import ClearMLService
from aisland.services.docker import DockerService
from aisland.services.fiftyone import FiftyOneService
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
    "Recorded in frameworks.toml; omit to reuse the recorded selection.",
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
    """Check the shared .venv for known recurring dependency skew (exit 1 if any check fails)."""
    from aisland.doctor import run_doctor

    raise SystemExit(run_doctor())


# --- Project Source Commands ---


@main.group()
def source() -> None:
    """Choose where each workspace project's package comes from.

    \b
    local   checked out on --ref, installed editable  (you develop it here)
    github  no checkout, installed from the remote at --ref
    pypi    no checkout, installed from the published wheel

    `github` and `pypi` both deinit the submodule (empty directory) and drop it from the
    *.code-workspace folder list. Both layers of configuration — the committed
    `sources.toml` and the per-machine `.git/config` override — are honoured.
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
    help="Record only the per-machine .git/config override; leave sources.toml (and the "
    "committed build files it drives) untouched.",
)
@click.option("--no-scaffold", is_flag=True, help="Skip regenerating dependent projects' CI files.")
def source_set(
    project: str,
    kind: str,
    ref: Optional[str],
    version: Optional[str],
    machine_only: bool,
    no_scaffold: bool,
) -> None:
    """Set PROJECT's source to KIND and apply it (checkout, venv, editor, CI).

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
            scaffold=not no_scaffold,
        )
    )


@source.command(name="unset")
@click.argument("project")
@click.option("--machine-only", is_flag=True, help="Clear only this machine's override; keep the committed source.")
@click.option("--no-scaffold", is_flag=True, help="Skip regenerating dependent projects' CI files.")
def source_unset(project: str, machine_only: bool, no_scaffold: bool) -> None:
    """Return PROJECT to unmanaged (historical behaviour: checkout + editable, `@main` in CI).

    The checkout and the venv are left as they are — unmanaged means aisland holds no opinion,
    not that the current state is wrong. Use `aisland source set <project> local` to actively
    restore a checkout.
    """
    from aisland.sources import unset_project_source

    raise SystemExit(unset_project_source(project=project, machine_only=machine_only, scaffold=not no_scaffold))


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

    Both layers are honoured: the committed `frameworks.toml` (shared, and the set
    generated CI installs) and a per-machine `.git/config aisland.frameworks` override.
    """
    pass


@framework.command(name="list")
def framework_list() -> None:
    """Show each declared framework, which projects offer it, and whether it is selected."""
    from aisland.frameworks import FRAMEWORKS_FILE, describe

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
    click.echo(f"\n  '-' = that layer records no selection at all ({FRAMEWORKS_FILE} absent, or no override).")

    # Print the commands that CHANGE this, using real framework names so they are copy-pasteable.
    # `--machine-only` is a FLAG on set/unset, never a command of its own — showing it detached
    # from its verb sent an operator hunting for a subcommand that does not exist.
    example = " ".join(row["framework"] for row in rows[:2])
    commands = [
        (f"aisland framework set {example}", f"shared: writes {FRAMEWORKS_FILE}; CI installs it"),
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
    help="Record only the per-machine .git/config override; leave frameworks.toml "
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

    Tracking backends live behind extras (`marainer[mlflow]`/`[aim]`/`[tensorboard]`/`[wandb]`)
    and each imports its library lazily, so code runs with none of them installed — the
    local-file backend always works. A project declares which of its extras are trackers in
    `[tool.aisland] trackers`; this command selects among them, and `aisland setup` installs
    the intersection.

    Both layers are honoured: the committed `trackers.toml` (shared, and the set generated
    CI installs) and a per-machine `.git/config aisland.trackers` override.
    """
    pass


@tracker.command(name="list")
def tracker_list() -> None:
    """Show each declared tracker, which projects offer it, and whether it is selected."""
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
    click.echo(f"\n  '-' = that layer records no selection at all ({TRACKERS.file} absent, or no override).")

    example = " ".join(row["tracker"] for row in rows[:2])
    commands = [
        (f"aisland tracker set {example}", f"shared: writes {TRACKERS.file}; CI installs it"),
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
    help="Record only the per-machine .git/config override; leave trackers.toml "
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


@docker.command(name="start")
def docker_start() -> None:
    """Start Docker."""
    DockerService().launch()


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
def docker_restart() -> None:
    """Restart Docker."""
    DockerService().restart()


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
