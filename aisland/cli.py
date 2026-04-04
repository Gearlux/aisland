from typing import Optional

import click

from aisland.manager import provision_system, setup_workspace
from aisland.services.jenkins import JenkinsService
from aisland.services.mlflow import MLflowService
from aisland.services.swiftbar import SwiftBarService


@click.group()
def main() -> None:
    """Aisland Manager: Multi-service development infrastructure."""
    pass


@main.command()
def provision() -> None:
    """Scan projects and install required system dependencies (Homebrew)."""
    provision_system()


@main.command()
def setup() -> None:
    """Full workspace setup: provision system tools and install all projects in editable mode."""
    setup_workspace()


# --- Jenkins Commands ---


@main.group()
def jenkins() -> None:
    """Manage local Jenkins CI/CD server."""
    pass


@jenkins.command(name="start")
@click.option("--port", default=8080, help="HTTP port for Jenkins.")
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


@jenkins.command(name="restart")
@click.option("--port", default=8080, help="HTTP port for Jenkins.")
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


@jenkins.command(name="remove")
@click.argument("project", required=False)
@click.confirmation_option(prompt="Are you sure you want to remove the pipeline(s)?")
def jenkins_remove(project: Optional[str]) -> None:
    """Remove a specific project pipeline or all pipelines from Jenkins."""
    JenkinsService().remove_pipelines(project_name=project)


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


@swiftbar.command(name="reset")
def swiftbar_reset() -> None:
    """Alias for remove."""
    SwiftBarService().reset()


# --- Global Commands ---


@main.command()
def status() -> None:
    """Show status of all aisland services."""
    click.echo("Aisland Infrastructure Status:")
    JenkinsService().status()
    MLflowService().status()
    SwiftBarService().status()


@main.command(name="start")
@click.option("--background", "-b", is_flag=True, help="Run all in background.")
def start(background: bool) -> None:
    """Start all aisland services."""
    click.echo("Starting all services...")
    JenkinsService().launch(background=background)
    MLflowService().launch(background=background)
    SwiftBarService().launch()


@main.command(name="stop")
def stop() -> None:
    """Stop all aisland services."""
    click.echo("Stopping all services...")
    JenkinsService().stop()
    MLflowService().stop()
    SwiftBarService().stop()


@main.command(name="restart")
@click.option("--background", "-b", is_flag=True, help="Run all in background.")
def restart(background: bool) -> None:
    """Restart all aisland services."""
    click.echo("Restarting all services...")
    JenkinsService().restart(background=background)
    MLflowService().restart(background=background)
    SwiftBarService().restart()


@main.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe ALL aisland data?")
def reset() -> None:
    """Stop all services and wipe all data."""
    click.echo("Resetting all services...")
    JenkinsService().reset()
    MLflowService().reset()
    SwiftBarService().reset()


if __name__ == "__main__":
    main()
