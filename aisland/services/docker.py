import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import click

from aisland.manager import BaseService


class DockerService(BaseService):
    """Docker Container Engine (via Colima)."""

    def __init__(self) -> None:
        super().__init__("docker")

    def is_running(self) -> bool:
        """Check if Colima is currently running."""
        try:
            # colima status returns non-zero if not running
            result = subprocess.run(
                ["colima", "status"],
                capture_output=True,
                text=True,
                check=False,
            )
            return "status: running" in result.stdout.lower()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _find_process_by_pattern(self) -> bool:
        """Not used for Colima as it has its own status command."""
        return self.is_running()

    def launch(self, background: bool = True, **kwargs: Any) -> None:
        """Start the Docker engine via Colima."""
        if self.is_running():
            click.echo("Docker (Colima) is already running.")
            self.configure_socket()
            return

        click.echo("Starting Docker (Colima)...")
        try:
            # We always run colima start in a way that it can return or background
            # Colima start itself backgrounds by default but we can check if it succeeds.
            cmd = ["colima", "start"]
            
            # For Apple Silicon, we might need specific architecture if the user encountered issues
            # but we'll stick to defaults for now as per brew installation.
            
            subprocess.check_call(cmd)
            
            # Ensure Docker socket is configured
            self.configure_socket()
            
            click.echo("Docker (Colima) started successfully.")
        except subprocess.CalledProcessError as e:
            click.echo(f"Error starting Docker (Colima): {e}", err=True)

    def stop(self) -> None:
        """Stop the Docker engine via Colima."""
        if not self.is_running():
            click.echo("Docker (Colima) is not running.")
            return

        click.echo("Stopping Docker (Colima)...")
        try:
            subprocess.check_call(["colima", "stop"])
            click.echo("Docker (Colima) stopped.")
        except subprocess.CalledProcessError as e:
            click.echo(f"Error stopping Docker (Colima): {e}", err=True)

    def status(self) -> None:
        """Display Docker engine status."""
        try:
            result = subprocess.run(["colima", "status"], capture_output=True, text=True)
            if result.returncode == 0:
                click.echo(f"  Docker (Colima)  : {click.style('Running', fg='green')}")
                # Show socket info if available
                if "socket:" in result.stdout.lower():
                    for line in result.stdout.splitlines():
                        if "socket:" in line.lower():
                            click.echo(f"    {line.strip()}")
            else:
                click.echo(f"  Docker (Colima)  : {click.style('Stopped', fg='red')}")
        except FileNotFoundError:
            click.echo(f"  Docker (Colima)  : {click.style('Not Installed', fg='yellow')}")

    def configure_socket(self) -> None:
        """Ensure the Docker client is pointing to the standard /var/run/docker.sock."""
        # Colima socket path
        colima_socket = Path.home() / ".colima" / "default" / "docker.sock"
        standard_socket = Path("/var/run/docker.sock")

        if not colima_socket.exists():
            # Fallback for older colima versions/custom profiles
            colima_socket = Path.home() / ".colima" / "docker.sock"

        if not colima_socket.exists():
            click.echo(f"  - Warning: Colima socket not found at {colima_socket}")
            return

        click.echo(f"Configuring Docker socket symlink...")
        try:
            # Check if /var/run/docker.sock already exists and points to the right place
            if standard_socket.exists() or standard_socket.is_symlink():
                if standard_socket.is_symlink() and os.readlink(standard_socket) == str(colima_socket):
                    click.echo("  - Standard socket symlink already configured.")
                    return
                
                # If it exists but is wrong/different, we need to remove it
                click.echo(f"  - Removing existing {standard_socket}...")
                subprocess.run(["sudo", "rm", "-f", str(standard_socket)], check=True)

            # Create symlink (requires sudo for /var/run)
            click.echo(f"  - Creating symlink: {standard_socket} -> {colima_socket}")
            subprocess.run(["sudo", "ln", "-s", str(colima_socket), str(standard_socket)], check=True)
            click.echo("  - Socket configuration complete.")
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error configuring socket symlink: {e}")
            click.echo("    (You may need to manually run: sudo ln -s " + str(colima_socket) + " /var/run/docker.sock)")

    def reset(self) -> None:
        """Factory reset Colima (wipes all containers/images)."""
        if click.confirm("Are you sure you want to delete ALL Docker data (containers, images, volumes)?"):
            if self.is_running():
                self.stop()
            click.echo("Deleting Colima instance...")
            try:
                subprocess.check_call(["colima", "delete"])
                click.echo("Docker (Colima) reset complete.")
            except subprocess.CalledProcessError as e:
                click.echo(f"Error resetting Docker (Colima): {e}", err=True)
