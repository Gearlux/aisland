import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional

import click

# Path Configuration (relative to the file itself)
SERVICES_DIR = Path(__file__).parent.absolute()
# The package is at aisland/aisland/manager.py, so project root is SERVICES_DIR.parent
BASE_DIR = SERVICES_DIR.parent.absolute()
BIN_DIR = BASE_DIR / "bin"
DATA_DIR = BASE_DIR / "data"


class BaseService:
    """Standardized management logic for aisland services."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.service_data_dir = DATA_DIR / name
        self.pid_file = DATA_DIR / f"{name}.pid"
        self.log_file = DATA_DIR / f"{name}.log"
        self.service_data_dir.mkdir(parents=True, exist_ok=True)

    def is_running(self) -> bool:
        """Check if the service is currently running."""
        # 1. Try PID file first
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                os.kill(pid, 0)
                return True
            except (OSError, ValueError):
                pass

        # 2. Fallback: Search for process by name/pattern
        return self._find_process_by_pattern()

    def _find_process_by_pattern(self) -> bool:
        """Search for a running process that looks like this service using pgrep."""
        try:
            patterns = {"jenkins": "jenkins.war", "mlflow": "mlflow server"}
            pattern = patterns.get(self.name, self.name)

            # Use pgrep -f to search the full command line
            cmd = ["pgrep", "-f", pattern]
            output = subprocess.check_output(cmd, text=True).strip()

            if output:
                # Take the first PID returned
                pids = output.splitlines()
                if pids:
                    pid = pids[0]
                    self.pid_file.write_text(pid)
                    return True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            pass
        return False

    def get_pid(self) -> Optional[int]:
        """Return the current PID if running."""
        if self.is_running():
            return int(self.pid_file.read_text().strip())
        return None

    def start(self, cmd: List[str], env: Optional[dict] = None, background: bool = False) -> None:
        """Execute the service command."""
        if self.is_running():
            click.echo(f"{self.name.capitalize()} is already running (PID: {self.get_pid()}).")
            return

        click.echo(f"Starting {self.name.capitalize()}...")

        # Prepare environment
        process_env = os.environ.copy()

        # Ensure the root workspace virtual environment is prioritized
        # This guarantees that 'python3' and other tools point to the project-standard environment.
        workspace_root = Path(__file__).resolve().parents[3]
        venv_bin = workspace_root / ".venv" / "bin"

        current_path = process_env.get("PATH", "")
        if str(venv_bin) not in current_path:
            current_path = f"{venv_bin}:{current_path}"
        process_env["PATH"] = current_path

        if env:
            process_env.update(env)
        if background:
            with open(self.log_file, "w") as log:
                process = subprocess.Popen(
                    cmd,
                    env=process_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self.pid_file.write_text(str(process.pid))
            click.echo(f"{self.name.capitalize()} started in background (PID: {process.pid})")
            click.echo(f"Logs: {self.log_file}")
        else:
            try:
                process = subprocess.Popen(cmd, env=process_env)
                self.pid_file.write_text(str(process.pid))
                process.wait()
            except KeyboardInterrupt:
                click.echo(f"\nStopping {self.name.capitalize()}...")
                process.terminate()
                process.wait()
            finally:
                if self.pid_file.exists():
                    self.pid_file.unlink()

    def stop(self) -> None:
        """Stop the running service."""
        pid = self.get_pid()
        if not pid:
            click.echo(f"{self.name.capitalize()} is not running.")
            return

        click.echo(f"Stopping {self.name.capitalize()} (PID: {pid})...")
        try:
            if os.name == "nt":  # Windows
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)

            # Wait for shutdown
            for _ in range(10):
                if not self.is_running():
                    break
                time.sleep(0.5)

            if self.pid_file.exists():
                self.pid_file.unlink()
            click.echo(f"{self.name.capitalize()} stopped.")
        except OSError as e:
            click.echo(f"Error stopping {self.name.capitalize()}: {e}")

    def status(self) -> None:
        """Display service status."""
        running = self.is_running()
        status_str = click.style("Running", fg="green") if running else click.style("Stopped", fg="red")
        click.echo(f"  {self.name.capitalize():<15} : {status_str} " + (f"(PID: {self.get_pid()})" if running else ""))

    def restart(self, **kwargs: Any) -> None:
        """Restart the service."""
        click.echo(f"Restarting {self.name}...")
        self.stop()
        # We assume launch is the method to start with default/stored config
        # Individual services might need to override this if launch signatures differ significantly
        if hasattr(self, "launch"):
            getattr(self, "launch")(**kwargs)
        else:
            click.echo(f"Error: {self.name} service does not have a launch method.")

    def reset(self) -> None:
        """Stop the service and wipe its data directory, logs, and PID files."""
        if self.is_running():
            self.stop()

        if self.service_data_dir.exists():
            import shutil

            click.echo(f"Wiping data for {self.name} at {self.service_data_dir}...")
            shutil.rmtree(self.service_data_dir)
            self.service_data_dir.mkdir(parents=True, exist_ok=True)

        if self.log_file.exists():
            self.log_file.unlink()

        if self.pid_file.exists():
            self.pid_file.unlink()

        click.echo(f"{self.name.capitalize()} data reset.")


def get_workspace_root() -> Path:
    """Resolve the absolute path to the workspace root by searching upwards for a marker."""
    curr = Path(__file__).resolve()
    # Search up to 6 levels up for the workspace root marker TASKS.md
    for _ in range(6):
        if (curr / "TASKS.md").exists():
            return curr
        curr = curr.parent

    # Fallback to .git if TASKS.md not found
    curr = Path(__file__).resolve()
    for _ in range(6):
        if (curr / ".git").exists():
            return curr
        curr = curr.parent

    # Absolute fallback
    return Path(__file__).resolve().parents[3]


def get_workspace_name() -> str:
    """Return the name of the workspace (repo name or root directory name)."""
    root = get_workspace_root()
    try:
        # Try to get the repo name from git remote
        import subprocess

        output = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
        if output:
            # Extract name from URL (handles both https and ssh)
            # e.g., https://github.com/Gearlux/gearlux.git -> gearlux
            name = output.split("/")[-1].replace(".git", "")
            if name:
                return name
    except Exception:
        pass
    return root.name


def discover_projects(root: Path) -> List[Path]:
    """Recursively discover pyproject.toml files, but only in directories that are identified as projects."""
    projects = []
    # Skip standard ignored directories
    if any(p in str(root) for p in [".venv", "aisland/data"]):
        return []

    for item in root.iterdir():
        if item.is_dir():
            pyproject = item / "pyproject.toml"
            if pyproject.exists():
                projects.append(pyproject)
                # If it's a project, also check for sub-projects (submodules)
                projects.extend(discover_projects(item))
    return projects


def provision_system() -> None:
    """Scan all projects for [tool.aisland.sys-dependencies] and install via Homebrew."""
    workspace_root = get_workspace_root()
    click.echo(f"==> Scanning for system dependencies in {workspace_root}...")

    import tomllib

    brew_packages = set()

    for pyproject in discover_projects(workspace_root):
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
                aisland_config = data.get("tool", {}).get("aisland", {})
                sys_deps = aisland_config.get("sys-dependencies", {})
                brew_deps = sys_deps.get("brew", [])
                if isinstance(brew_deps, list):
                    for dep in brew_deps:
                        brew_packages.add(dep)
        except Exception as e:
            click.echo(f"  - Warning: Failed to parse {pyproject}: {e}")

    if not brew_packages:
        click.echo("  - No system dependencies found.")
        return

    click.echo(f"  - Found required brew packages: {', '.join(sorted(brew_packages))}")

    # Check for brew command
    try:
        subprocess.check_call(["brew", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        click.echo("  - Error: 'brew' command not found. Homebrew is required for system provisioning.")
        return

    # Non-interactive installation
    env = os.environ.copy()
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

    for pkg in sorted(brew_packages):
        click.echo(f"==> Installing {pkg} via Homebrew...")
        try:
            # --quiet flag for non-interactive output reduction
            subprocess.check_call(["brew", "install", "--quiet", pkg], env=env)
            click.echo(f"  - {pkg} installed successfully.")
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error: Failed to install {pkg}: {e}")


def get_project_metadata(pyproject_path: Path) -> dict:
    """Extract name and dependencies from pyproject.toml."""
    import tomllib

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
        project_info = data.get("project", {})
        name = project_info.get("name")
        deps = project_info.get("dependencies", [])
        # Also include optional-dependencies if they are internal
        optional_deps = project_info.get("optional-dependencies", {})
        for extra_deps in optional_deps.values():
            deps.extend(extra_deps)

        return {"name": name, "path": pyproject_path.parent, "dependencies": deps, "has_dev": "dev" in optional_deps}


def get_install_order(workspace_root: Path) -> List[dict]:
    """Determine the correct installation order using topological sort based on workspace dependencies."""
    pyprojects = discover_projects(workspace_root)
    projects = [get_project_metadata(p) for p in pyprojects]
    project_names = {p["name"] for p in projects}

    # Build adjacency list
    adj: dict[str, list[str]] = {p["name"]: [] for p in projects}
    for p in projects:
        for dep in p["dependencies"]:
            # Extract package name from dependency string (e.g., 'logflow>=0.1.0' -> 'logflow')
            dep_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
            if dep_name in project_names and dep_name != p["name"]:
                adj[p["name"]].append(dep_name)

    # Topological sort (Kahn's algorithm or DFS)
    visited = set()
    temp_visited = set()
    order = []

    def visit(name: str) -> None:
        if name in temp_visited:
            # Circular dependency detected - just break and continue for now
            return
        if name not in visited:
            temp_visited.add(name)
            for neighbor in adj.get(name, []):
                visit(neighbor)
            temp_visited.remove(name)
            visited.add(name)
            order.append(name)

    for p in projects:
        visit(p["name"])

    # Map names back to metadata
    name_to_meta = {p["name"]: p for p in projects}
    return [name_to_meta[name] for name in order]


def setup_workspace() -> None:
    """Perform a full workspace setup: system provisioning followed by editable pip installs."""
    workspace_root = get_workspace_root()
    venv_bin = workspace_root / ".venv" / "bin"
    pip_cmd = str(venv_bin / "pip")

    if not venv_bin.exists():
        click.echo(f"Error: Virtual environment not found at {workspace_root / '.venv'}. Run aisland/setup.sh first.")
        return

    # 1. System Provisioning
    provision_system()

    # 2. Dependency Discovery
    click.echo(f"==> Resolving workspace dependency graph in {workspace_root}...")
    install_order = get_install_order(workspace_root)

    # 3. Provisioning (Git & Python)
    for project in install_order:
        project_dir = project["path"]

        # Git Configuration (pull.rebase false)
        try:
            subprocess.check_call(["git", "config", "pull.rebase", "false"], cwd=project_dir)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        # Python Provisioning (Editable Installs)
        target = f"{project_dir}[dev]" if project["has_dev"] else str(project_dir)
        click.echo(f"==> Installing {project['name']} ({target})...")
        try:
            subprocess.check_call([pip_cmd, "install", "-e", target])
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error: Failed to install {project['name']}: {e}")

    click.echo("==> Workspace setup complete!")


def run_quality_gates(arg1: Optional[str] = None, arg2: Optional[str] = None) -> None:
    """
    Execute quality gates.
    aisland check                 -> All projects, all tools
    aisland check confluid         -> All tools for confluid
    aisland check pytest          -> pytest for all projects
    aisland check pytest confluid -> pytest for confluid
    """
    workspace_root = get_workspace_root()
    venv_bin = workspace_root / ".venv" / "bin"

    if not venv_bin.exists():
        click.echo(f"Error: Virtual environment not found at {workspace_root / '.venv'}. Run aisland/setup.sh first.")
        return

    env = os.environ.copy()
    env["PATH"] = f"{venv_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(workspace_root)

    # Find projects to check
    pyprojects = discover_projects(workspace_root)
    all_projects = [get_project_metadata(p) for p in pyprojects]
    project_names = {p["name"] for p in all_projects}

    # Tool mapping
    supported_tools = ["isort", "black", "flake8", "mypy", "pytest"]

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
        click.echo(
            f"Error: Argument '{arg1}' is neither a project nor a supported tool ({', '.join(supported_tools)})."
        )
        return

    # Filter projects
    if target_project:
        projects = [p for p in all_projects if p["name"] == target_project]
        if not projects:
            click.echo(f"Error: Project '{target_project}' not found.")
            return
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

        if target_tool:
            active_tools = [t for t in all_tools if t[0] == target_tool]
        else:
            active_tools = all_tools

        project_failed = False
        for current_tool_name, cmd, project_relative in active_tools:
            cwd = project_dir if project_relative else workspace_root
            try:
                subprocess.check_call(cmd, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                click.echo(f"  [{click.style('PASS', fg='green')}] {current_tool_name}")
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

    if any(not r[1] for r in results):
        exit(1)
