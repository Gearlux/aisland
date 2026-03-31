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


def provision_system() -> None:
    """Scan all projects for [tool.aisland.sys-dependencies] and install via Homebrew."""
    workspace_root = get_workspace_root()
    click.echo(f"==> Scanning for system dependencies in {workspace_root}...")

    import json
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore

    brew_packages = set()

    for pyproject in workspace_root.glob("**/pyproject.toml"):
        if ".venv" in str(pyproject) or "aisland/data" in str(pyproject):
            continue

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

    # 2. Python Provisioning (Editable Installs)
    click.echo(f"==> Performing editable installs for all projects in {workspace_root}...")
    
    # We prioritize projects that others depend on (order doesn't strictly matter for editable, but it's cleaner)
    # confluid is a core dependency, so we ensure it's handled early.
    project_dirs = [
        p.parent for p in workspace_root.glob("**/pyproject.toml") 
        if ".venv" not in str(p) and "aisland/data" not in str(p)
    ]
    
    # Sort: confluid first, then logflow, then others.
    def project_priority(d: Path) -> int:
        name = d.name
        if name == "confluid": return 0
        if name == "logflow": return 1
        return 10

    project_dirs.sort(key=project_priority)

    for project_dir in project_dirs:
        # Check if project has [dev] extra
        has_dev = False
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
            
        try:
            with open(project_dir / "pyproject.toml", "rb") as f:
                data = tomllib.load(f)
                if "dev" in data.get("project", {}).get("optional-dependencies", {}):
                    has_dev = True
        except Exception:
            pass

        target = f"{project_dir}[dev]" if has_dev else str(project_dir)
        click.echo(f"==> Installing {project_dir.name} ({target})...")
        try:
            subprocess.check_call([pip_cmd, "install", "-e", target])
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error: Failed to install {project_dir.name}: {e}")

    click.echo("==> Workspace setup complete!")
