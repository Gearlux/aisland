import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional

import click

# Path Configuration (relative to the file itself)
SERVICES_DIR = Path(__file__).parent.absolute()
# The package is at sandbox/sandbox/manager.py, so project root is SERVICES_DIR.parent
BASE_DIR = SERVICES_DIR.parent.absolute()
BIN_DIR = BASE_DIR / "bin"
DATA_DIR = BASE_DIR / "data"


class BaseService:
    """Standardized management logic for sandbox services."""

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
