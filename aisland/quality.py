"""``aisland check``: the quality gates (isort, black, flake8, mypy, pytest, act) of every project or one."""

import os
import subprocess
from typing import Optional

import click

from aisland import provision
from aisland.errors import AislandError
from aisland.mcp_config import no_venv
from aisland.projects import discover_projects, get_project_metadata, get_workspace_root


def run_quality_gates(arg1: Optional[str] = None, arg2: Optional[str] = None) -> None:
    """
    Execute quality gates.
    aisland check                 -> All projects, all tools
    aisland check models          -> All tools for models
    aisland check pytest          -> pytest for all projects
    aisland check pytest models   -> pytest for models
    """
    workspace_root = get_workspace_root()
    venv_bin = workspace_root / ".venv" / "bin"

    if not venv_bin.exists():
        raise no_venv(workspace_root)

    env = os.environ.copy()
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(workspace_root)

    # Find projects to check
    pyprojects = discover_projects(workspace_root)
    all_projects = [get_project_metadata(p) for p in pyprojects]
    project_names = {p["name"] for p in all_projects}

    # Tool mapping
    supported_tools = ["isort", "black", "flake8", "mypy", "pytest", "act"]

    target_project: Optional[str] = None
    target_tool: Optional[str] = None

    # Logic to resolve arg1 and arg2
    if arg1 in supported_tools:
        target_tool = arg1
        target_project = arg2  # Can be None (all projects)
    elif arg1 in project_names:
        target_project = arg1
        target_tool = arg2  # Can be None (all tools)
    elif arg1:
        raise AislandError(
            f"Argument '{arg1}' is neither a project nor a supported tool ({', '.join(supported_tools)})."
        )

    # Filter projects
    if target_project:
        projects = [p for p in all_projects if p["name"] == target_project]
        if not projects:
            raise AislandError(f"Project '{target_project}' not found.")
    else:
        projects = all_projects

    results: list[tuple[str, bool]] = []

    for project in projects:
        project_dir = project["path"]
        name = project["name"]
        click.echo(f"==> Checking {name} at {project_dir}...")

        # Quality Gate Tools configuration
        all_tools = [
            ("isort", ["isort", "."], True),
            ("black", ["black", "."], True),
            ("flake8", ["flake8", "."], True),
            ("mypy", ["mypy", str(project_dir.relative_to(workspace_root))], False),
            ("pytest", ["pytest"], True),
        ]

        if (project_dir / ".github" / "workflows").exists():
            all_tools.append(("act", ["act", "--list"], True))

        if target_tool:
            active_tools = [t for t in all_tools if t[0] == target_tool]
        else:
            active_tools = all_tools

        project_failed = False
        for current_tool_name, cmd, project_relative in active_tools:
            cwd = project_dir if project_relative else workspace_root
            try:
                subprocess.check_call(
                    cmd,
                    cwd=cwd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                )
                click.echo(f"  [{click.style('PASS', fg='green')}] {current_tool_name}")
            except FileNotFoundError:
                if cmd[0] == "act":  # a system tool, not the project's dev extra
                    missing = f"act is not installed — {provision.install_command('act')}"
                else:
                    missing = f"{cmd[0]} is not installed in {venv_bin.parent} (install the project's dev extra: aisland setup)"
                click.echo(f"  [{click.style('FAIL', fg='red')}] {current_tool_name} — {missing}")
                project_failed = True
            except subprocess.CalledProcessError:
                click.echo(f"  [{click.style('FAIL', fg='red')}] {current_tool_name}")
                # Re-run to show output if it failed
                click.echo(f"--- {current_tool_name} Output ---")
                subprocess.run(cmd, cwd=cwd, env=env)
                click.echo("-----------------------")
                project_failed = True

        results.append((name, not project_failed))

    # Summary table
    if len(results) > 1:
        click.echo("\nQuality Gate Summary:")
        for name, success in results:
            status = click.style("SUCCESS", fg="green") if success else click.style("FAILURE", fg="red")
            click.echo(f"  {name:<25} : {status}")

    failed = [name for name, success in results if not success]
    if failed:
        raise AislandError(f"the quality gates failed: {', '.join(failed)}")
