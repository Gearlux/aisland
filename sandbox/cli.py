import click
from typing import Optional

from sandbox.services.jenkins import JenkinsService
from sandbox.services.mlflow import MLflowService


@click.group()
def main() -> None:
    """Sandbox Manager: Multi-service development infrastructure."""
    pass


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
def jenkins_init(project: Optional[str]) -> None:
    """Initialize Jenkins with Multi-branch pipelines for a project or all projects."""
    JenkinsService().init_pipelines(project_name=project)


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
@click.option("--port", default=5000, help="HTTP port for MLflow.")
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


@mlflow.command(name="reset")
@click.confirmation_option(prompt="Are you sure you want to wipe all MLflow data?")
def mlflow_reset() -> None:
    """Stop MLflow and wipe all its data."""
    MLflowService().reset()


# --- Global Commands ---

@main.command()
def status() -> None:
    """Show status of all sandbox services."""
    click.echo("Sandbox Infrastructure Status:")
    JenkinsService().status()
    MLflowService().status()


@main.command(name="start-all")
@click.option("--background", "-b", is_flag=True, help="Run all in background.")
def start_all(background: bool) -> None:
    """Start all sandbox services."""
    click.echo("Starting all services...")
    JenkinsService().launch(background=background)
    MLflowService().launch(background=background)


@main.command(name="stop-all")
def stop_all() -> None:
    """Stop all sandbox services."""
    click.echo("Stopping all services...")
    JenkinsService().stop()
    MLflowService().stop()


@main.command(name="reset-all")
@click.confirmation_option(prompt="Are you sure you want to wipe ALL sandbox data?")
def reset_all() -> None:
    """Stop all services and wipe all data."""
    click.echo("Resetting all services...")
    JenkinsService().reset()
    MLflowService().reset()


if __name__ == "__main__":
    main()
