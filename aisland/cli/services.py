"""The services' commands: `aisland services …`, and the groups of aisland's own services — `docker`, `jenkins`,
`swiftbar` — and of the supervisor behind them.
"""

import json
from pathlib import Path
from typing import Optional, Tuple

import click

from aisland import ci_scaffold, ci_templates
from aisland import supervisor as supervisor_mod
from aisland.cli._base import _Group, _service_entries
from aisland.services import jenkins_jobs
from aisland.services import registry as service_registry
from aisland.services.docker import docker_service
from aisland.services.jenkins import JenkinsService
from aisland.services.swiftbar import SwiftBarService

# --- Docker Commands ---


@click.group(cls=_Group)
def docker() -> None:
    """Manage the Docker engine: in Colima's VM on macOS, the system's own engine on Linux."""
    pass


_MOUNT_HELP = (
    "macOS: share a host directory with the Colima VM so containers can bind-mount it: DIR (read-only) "
    "or DIR:w (writable); repeatable. $HOME is always kept. Colima shares ONLY $HOME by default, "
    'so an external volume needs this — e.g. --mount "$DATA_ROOT:r". Applies at VM boot.'
)


@docker.command(name="start")
@click.option("--mount", "mounts", multiple=True, metavar="DIR[:w]", help=_MOUNT_HELP)
def docker_start(mounts: Tuple[str, ...]) -> None:
    """Start Docker (optionally sharing extra host directories with the VM)."""
    docker_service().launch(mounts=mounts)


@docker.command(name="stop")
def docker_stop() -> None:
    """Stop Docker."""
    docker_service().stop()


@docker.command(name="status")
def docker_status() -> None:
    """Show Docker status."""
    docker_service().status()


@docker.command(name="info")
def docker_info() -> None:
    """Show detailed Docker configuration (socket paths, colima status)."""
    docker_service().info()


@docker.command(name="restart")
@click.option("--mount", "mounts", multiple=True, metavar="DIR[:w]", help=_MOUNT_HELP)
def docker_restart(mounts: Tuple[str, ...]) -> None:
    """Restart Docker (the way to change VM mounts on a running engine)."""
    docker_service().restart(mounts=mounts)


@docker.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe all Docker data?")
def docker_reset() -> None:
    """Stop Docker and wipe all its data."""
    docker_service().reset()


# --- Jenkins Commands ---


@click.group(cls=_Group)
def jenkins() -> None:
    """Manage local Jenkins CI/CD server."""
    pass


@jenkins.command(name="start")
@click.option("--port", default=JenkinsService.DEFAULT_PORT, help="HTTP port for Jenkins.")
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
@click.option("--port", default=JenkinsService.DEFAULT_PORT, help="HTTP port for Jenkins.")
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
        jenkins_jobs.remove_pipelines(JenkinsService().service_data_dir, project)
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
    jenkins_jobs.init_pipelines(JenkinsService(), project, local)


@jenkins.command(name="scaffold")
@click.argument("project", required=False)
@click.option("--force", "-f", is_flag=True, help="Overwrite existing files.")
def jenkins_scaffold(project: Optional[str], force: bool) -> None:
    """Generate standard Jenkinsfile, Jenkinsfile.local, and GitHub Actions for a project or all projects."""
    ci_scaffold.scaffold(project, force, JenkinsService().url())


@jenkins.group(name="templates", cls=_Group)
def jenkins_templates() -> None:
    """The template set `aisland jenkins scaffold` writes a project's CI files from.

    aisland's own set by default; a workspace names another in workspace.toml — a folder of the workspace, or the name
    an installed package registers:

    \b
    [ci]
    templates = "ci-templates"
    [ci.variables]
    agent_label = "linux-pod"
    """


@jenkins_templates.command(name="export")
@click.argument("folder", type=click.Path(file_okay=False, path_type=Path))
def jenkins_templates_export(folder: Path) -> None:
    """Copy aisland's own template set into FOLDER (new or empty), to start a set of your own from it.

    \b
    aisland jenkins templates export ci-templates
    """
    copied = ci_templates.export(folder)
    click.echo(f"==> Copied aisland's template set into {folder} ({len(copied)} files)")
    click.echo(f'  - Point the workspace at it: [ci] templates = "{folder.name}" in workspace.toml')


@jenkins.command(name="remove")
@click.argument("project", required=False)
@click.confirmation_option(prompt="Are you sure you want to remove the pipeline(s)?")
def jenkins_remove(project: Optional[str]) -> None:
    """Remove a specific project pipeline or all pipelines from Jenkins."""
    jenkins_jobs.remove_pipelines(JenkinsService().service_data_dir, project)


# --- SwiftBar Commands ---


@click.group(cls=_Group)
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


@click.group(name="services", cls=_Group, invoke_without_command=True)
@click.pass_context
def services(ctx: click.Context) -> None:
    """Every aisland service at once, or the ones named. Alone: the supervisor and each service's state.

    With no name, start/stop/restart/reset act on the services started by default (docker, jenkins, swiftbar);
    status and info cover every service, and a service a project brings starts when named. Each service also has
    its own group (`aisland jenkins start --port 9081`).

    \b
    aisland services start -b
    aisland services start jenkins docker
    aisland services stop
    """
    if ctx.invoked_subcommand is None:
        every = _service_entries()  # a declaration aisland cannot use stops here, before anything is printed
        click.echo("Aisland Infrastructure Status:")
        supervisor_mod.print_status()
        service_registry.status(every, ())


_SERVICE_NAMES = click.argument("names", nargs=-1)


_BACKGROUND = click.option("--background", "-b", is_flag=True, help="Run in the background.")


@services.command(name="start")
@_SERVICE_NAMES
@_BACKGROUND
def services_start(names: Tuple[str, ...], background: bool) -> None:
    """Start the services named (default: the services started by default)."""
    service_registry.start(_service_entries(), names, background)


@services.command(name="stop")
@_SERVICE_NAMES
def services_stop(names: Tuple[str, ...]) -> None:
    """Stop the services named (default: the services started by default, the container engine last)."""
    service_registry.stop(_service_entries(), names)


@services.command(name="restart")
@_SERVICE_NAMES
@_BACKGROUND
def services_restart(names: Tuple[str, ...], background: bool) -> None:
    """Restart the services named (default: the services started by default)."""
    service_registry.restart(_service_entries(), names, background)


@services.command(name="status")
@_SERVICE_NAMES
@click.option("--json", "as_json", is_flag=True, help="Print the states as one JSON object.")
def services_status(names: Tuple[str, ...], as_json: bool) -> None:
    """Each named service's state (default: every service)."""
    if as_json:
        click.echo(json.dumps({"services": service_registry.states(_service_entries(), names)}, indent=2))
        return
    service_registry.status(_service_entries(), names)


@services.command(name="info")
@_SERVICE_NAMES
def services_info(names: Tuple[str, ...]) -> None:
    """URLs, credentials and paths of the services named (default: every service)."""
    service_registry.info(_service_entries(), names)


@services.command(name="reset")
@_SERVICE_NAMES
@click.confirmation_option(prompt="Stop these services and wipe their data?")
def services_reset(names: Tuple[str, ...]) -> None:
    """Stop the services named and wipe their data (default: the services started by default)."""
    service_registry.reset(_service_entries(), names)


# --- Supervisor Commands ---


@click.group(cls=_Group)
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
