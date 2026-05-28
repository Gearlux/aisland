from typing import Optional

import click

from aisland.manager import (
    generate_gemini_settings,
    get_workspace_root,
    provision_system,
    reconcile_submodules,
    run_quality_gates,
    setup_workspace,
)
from aisland.services.docker import DockerService
from aisland.services.fiftyone import FiftyOneService
from aisland.services.jenkins import JenkinsService
from aisland.services.labelstudio import LabelStudioService
from aisland.services.mlflow import MLflowService
from aisland.services.swiftbar import SwiftBarService


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
def setup() -> None:
    """Full workspace setup: provision system tools and install all projects in editable mode."""
    setup_workspace()


@main.command()
def reconcile() -> None:
    """Align submodule checkouts with .gitmodules `active` flags (init active, deinit inactive)."""
    reconcile_submodules(get_workspace_root())


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
    DockerService().status()
    JenkinsService().status()
    MLflowService().status()
    LabelStudioService().status()
    FiftyOneService().status()
    SwiftBarService().status()


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
