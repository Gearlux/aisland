"""Aim — the local experiment-tracking UI, served from a repository on disk.

Aim needs no server infrastructure: runs are written into an ``.aim`` repository directory
and ``aim up`` serves the comparison UI straight from it. The only thing this service has to
get right is that the repository it serves is the one the *tracker* writes to — so the path
is published to the workspace ``.env`` as ``AIM_REPO``, which a run config reads with
``$AIM_REPO`` the same way it reads ``DATA_ROOT``.
"""

import shutil
import sys
from pathlib import Path
from typing import List, Optional

import click

from aisland.manager import BaseService

DEFAULT_PORT = 43800

#: Workspace ``.env`` key holding the repository path, so a tracker and this UI agree.
REPO_ENV_KEY = "AIM_REPO"


class AimService(BaseService):
    """Aim UI (`aim up`) over the workspace's Aim repository."""

    def __init__(self) -> None:
        super().__init__("aim")
        # The repository is the service's own data dir, so runs stay inside aisland/data/
        # rather than polluting the workspace root with a stray `.aim`.
        self.repo_dir = self.service_data_dir

    def setup(self) -> None:
        """Fail with the selection command when aim is absent — never install it here.

        Which tracking backends a machine has is decided by ``aisland tracker`` and recorded
        in ``config.toml`` / ``.git/config``; a silent install leaves the venv holding
        something no selection asked for, and gone again after the next ``aisland setup``.
        """
        import importlib.util

        if importlib.util.find_spec("aim") is None:
            raise click.ClickException(
                "Aim is not installed in this environment.\n"
                "  Select it, so the choice is recorded and survives the next setup:\n"
                "      aisland tracker set aim\n"
                "  Or, for this machine only:\n"
                "      aisland tracker set aim --machine-only"
            )

    def info(self) -> None:
        """Show detailed Aim configuration."""
        super().info()
        ports = self.get_ports()
        url_port = ports[0] if ports else DEFAULT_PORT
        click.echo(f"  URL           : http://localhost:{url_port}")
        click.echo(f"  Repository    : {self.repo_dir}")
        click.echo(f"  {REPO_ENV_KEY:<14}: published to the workspace .env for run configs")

    def launch(self, port: int = DEFAULT_PORT, background: bool = False, repo: Optional[str] = None) -> None:
        """Start the Aim UI over ``repo`` (default: the workspace Aim repository)."""
        self.setup()
        target = self.repo_dir if repo is None else Path(repo).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        self._publish_repo(target)

        click.echo(f"Aim: http://localhost:{port}")
        click.echo(f"  Repository: {target}")

        cmd: List[str] = [
            self._aim_binary(),
            "up",
            "--host",
            "localhost",
            "--port",
            str(port),
            "--repo",
            str(target),
            # Aim prompts to initialise a repository it has not seen; a supervised process has
            # no one to answer, so it would hang instead of starting.
            "--yes",
        ]

        # Aim has no dedicated health endpoint; the app root serving HTTP is the signal.
        self.start(cmd, background=background, readiness={"port": port, "path": "/"})

    def _aim_binary(self) -> str:
        """The venv's ``aim`` console script, falling back to the name on PATH.

        Venv-first for the same reason the completion installer is: running from an
        unactivated shell must not silently pick up a different Aim (or none).
        """
        candidate = Path(sys.prefix) / "bin" / "aim"
        if candidate.exists():
            return str(candidate)
        return shutil.which("aim") or "aim"

    def _publish_repo(self, target: Path) -> None:
        """Record the repository path in the workspace ``.env`` as ``AIM_REPO``.

        This is the whole point of the service knowing where the repository lives: a run
        config points its tracker at ``$AIM_REPO``, so what a run writes and what this UI
        serves cannot drift apart.
        """
        from aisland.manager import get_workspace_root

        env_file = get_workspace_root() / ".env"
        line = f"{REPO_ENV_KEY}={target}"
        if not env_file.exists():
            env_file.write_text(line + "\n")
            click.echo(f"Created .env with {line}.")
            return

        lines = env_file.read_text().splitlines()
        replaced = False
        for index, existing in enumerate(lines):
            if existing.strip().startswith(f"{REPO_ENV_KEY}="):
                lines[index] = line
                replaced = True
                break
        if not replaced:
            lines.append(line)
        env_file.write_text("\n".join(lines) + "\n")
