"""``aisland check``: the quality gates (isort, black, flake8, mypy, pytest, act) of every project or one."""

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import click

from aisland import provision
from aisland.errors import AislandError
from aisland.mcp_config import no_venv
from aisland.projects import discover_projects, get_project_metadata, get_workspace_root

#: The gates, in the order they run. ``isort`` and ``black`` are the FORMATTERS: they rewrite the files they find (the
#: CI jobs only ``--check``); the others read.
SUPPORTED_TOOLS = ["isort", "black", "flake8", "mypy", "pytest", "act"]

#: One gate: its tool, its command, and whether it runs in the project folder (else in the workspace root).
_Gate = Tuple[str, List[str], bool]


def _targets(arg1: Optional[str], arg2: Optional[str], project_names: Set[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(project, tool)`` the two optional arguments name — each is a tool or a project, in either order."""
    if arg1 in SUPPORTED_TOOLS:
        return arg2, arg1
    if arg1 in project_names:
        return arg1, arg2
    if arg1:
        raise AislandError(
            f"Argument '{arg1}' is neither a project nor a supported tool ({', '.join(SUPPORTED_TOOLS)})."
        )
    return None, None


def _gates(project_dir: Path, workspace_root: Path) -> List[_Gate]:
    gates: List[_Gate] = [
        ("isort", ["isort", "."], True),
        ("black", ["black", "."], True),
        ("flake8", ["flake8", "."], True),
        ("mypy", ["mypy", str(project_dir.relative_to(workspace_root))], False),
        ("pytest", ["pytest"], True),
    ]
    if (project_dir / ".github" / "workflows").exists():
        gates.append(("act", ["act", "--list"], True))
    return gates


def _run_gate(tool: str, cmd: List[str], cwd: Path, env: Dict[str, str], venv_bin: Path) -> bool:
    """Run one gate ONCE and say PASS or FAIL; a failed gate's output follows its line, as it was produced."""
    try:
        run = subprocess.run(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
        )
    except FileNotFoundError:
        if cmd[0] == "act":  # a system tool, not the project's dev extra
            missing = f"act is not installed — {provision.install_command('act')}"
        else:
            missing = f"{cmd[0]} is not installed in {venv_bin.parent} (install the project's dev extra: aisland setup)"
        click.echo(f"  [{click.style('FAIL', fg='red')}] {tool} — {missing}")
        return False
    if run.returncode == 0:
        click.echo(f"  [{click.style('PASS', fg='green')}] {tool}")
        return True
    click.echo(f"  [{click.style('FAIL', fg='red')}] {tool}")
    click.echo(f"--- {tool} Output ---")
    click.echo(run.stdout.rstrip("\n"))
    click.echo("-----------------------")
    return False


def run_quality_gates(arg1: Optional[str] = None, arg2: Optional[str] = None) -> None:
    """
    Execute quality gates.
    aisland check                 -> All projects, all tools
    aisland check models          -> All tools for models
    aisland check pytest          -> pytest for all projects
    aisland check pytest models   -> pytest for models

    ``isort`` and ``black`` rewrite the files they find. Each gate runs once; a failed one shows what it printed.
    """
    workspace_root = get_workspace_root()
    venv_bin = workspace_root / ".venv" / "bin"

    if not venv_bin.exists():
        raise no_venv(workspace_root)

    env = os.environ.copy()
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(workspace_root)

    all_projects = [get_project_metadata(p) for p in discover_projects(workspace_root)]
    target_project, target_tool = _targets(arg1, arg2, {p["name"] for p in all_projects})

    if target_project:
        projects = [p for p in all_projects if p["name"] == target_project]
        if not projects:
            raise AislandError(f"Project '{target_project}' not found.")
    else:
        projects = all_projects

    results: List[Tuple[str, bool]] = []
    for project in projects:
        project_dir = project["path"]
        name = project["name"]
        click.echo(f"==> Checking {name} at {project_dir}...")
        passed = True
        for tool, cmd, in_project in _gates(project_dir, workspace_root):
            if target_tool and tool != target_tool:
                continue
            cwd = project_dir if in_project else workspace_root
            passed = _run_gate(tool, cmd, cwd, env, venv_bin) and passed
        results.append((name, passed))

    # Summary table
    if len(results) > 1:
        click.echo("\nQuality Gate Summary:")
        for name, success in results:
            status = click.style("SUCCESS", fg="green") if success else click.style("FAILURE", fg="red")
            click.echo(f"  {name:<25} : {status}")

    failed = [name for name, success in results if not success]
    if failed:
        raise AislandError(f"the quality gates failed: {', '.join(failed)}")
