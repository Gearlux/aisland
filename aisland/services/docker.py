"""Docker engine (Colima) — lifecycle plus the VM mounts a container can bind.

A ``docker run -v <host>:<target>`` on macOS binds a path INSIDE the Colima VM, and the VM
sees only what Colima shares with it. Two facts, both measured on colima 0.10.3 (2026-08-17)
and both load-bearing for every containerised service here:

- **The default share is ``$HOME`` and nothing else** (``colima ssh -- mount | grep virtiofs``
  → ``/Users/<me>``). A bind mount from an external volume such as ``$DATA_ROOT=/Volumes/…``
  therefore mounts an EMPTY directory that Docker creates inside the VM — with no error
  anywhere; the container simply lists nothing.
- **Any ``--mount`` REPLACES that default rather than adding to it.** ``colima start --mount
  /Volumes/Store:r`` left the VM sharing ``/Volumes/Store`` and no ``$HOME``, which breaks
  every bind mount under the home directory (all of ``aisland/data/…``). Colima persists the
  flags into ``~/.colima/default/colima.yaml`` (``--save-config`` defaults to true), so the
  breakage would survive every later restart.

``aisland docker start --mount DIR[:w]`` exists so those two facts are handled in ONE place:
``$HOME:w`` is always re-stated ahead of the requested mounts, and a mount change under a
RUNNING engine is refused with the restart command instead of restarting the VM — and every
container on it — unasked. Mounts apply at VM boot; the profile file is the record of what
the VM currently shares, which is what :meth:`DockerService.configured_mounts` reads.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Sequence

import click
import yaml

from aisland.manager import BaseService

#: The Colima profile aisland drives (its default profile). ``colima start --save-config``
#: (default true) writes the effective flags — mounts included — into ``colima.yaml`` here.
COLIMA_PROFILE_DIR = Path(".colima") / "default"


@dataclass(frozen=True)
class Mount:
    """One VM share, in the shape colima's ``mounts:`` list records it."""

    location: Path
    writable: bool

    @property
    def spec(self) -> str:
        """Colima's own ``--mount`` syntax: ``DIR:w`` writable, ``DIR:r`` read-only."""
        return f"{self.location}:{'w' if self.writable else 'r'}"


def parse_mount(spec: str) -> Mount:
    """``DIR`` / ``DIR:r`` / ``DIR:w`` -> :class:`Mount` (bare = read-only, as colima treats it).

    The directory must exist: colima itself would create the mount point inside the VM and
    share an empty directory, which is exactly the silent failure this flag exists to prevent.
    """
    location, writable = spec, False
    if spec.endswith((":r", ":w")):
        location, writable = spec[:-2], spec.endswith(":w")
    path = Path(location).expanduser().resolve()
    if not path.is_dir():
        raise click.ClickException(f"--mount {spec}: {path} is not a directory.")
    return Mount(path, writable)


def covers(configured: Sequence[Mount], wanted: Mount) -> bool:
    """Whether the VM's current shares already give a container ``wanted``.

    A share at or above the wanted location suffices (``/Volumes`` covers ``/Volumes/Store``),
    and a writable share covers a read-only request — never the reverse.
    """
    for mount in configured:
        if wanted.location == mount.location or mount.location in wanted.location.parents:
            if mount.writable or not wanted.writable:
                return True
    return False


class DockerService(BaseService):
    """Docker Container Engine (via Colima)."""

    def __init__(self) -> None:
        super().__init__("docker")

    # -- VM mounts ------------------------------------------------------------------------

    @property
    def colima_config(self) -> Path:
        return Path.home() / COLIMA_PROFILE_DIR / "colima.yaml"

    def configured_mounts(self) -> List[Mount]:
        """What the profile shares with the VM; an empty ``mounts:`` is colima's ``$HOME:w`` default."""
        mounts: List[Mount] = []
        if self.colima_config.exists():
            document = yaml.safe_load(self.colima_config.read_text()) or {}
            for entry in document.get("mounts") or []:
                mounts.append(Mount(Path(entry["location"]).expanduser(), bool(entry.get("writable", False))))
        return mounts or [Mount(Path.home(), writable=True)]

    def _start_argv(self, mounts: Sequence[Mount]) -> List[str]:
        """``colima start`` — with ``$HOME:w`` re-stated first whenever any mount is requested."""
        argv = ["colima", "start"]
        if not mounts:
            return argv
        home = Path.home()
        ordered = list(mounts)
        if not any(mount.location == home for mount in ordered):
            ordered.insert(0, Mount(home, writable=True))
        else:  # the user named $HOME themselves — keep their choice, but keep it first
            ordered.sort(key=lambda mount: mount.location != home)
        for mount in ordered:
            argv += ["--mount", mount.spec]
        return argv

    # -- lifecycle ------------------------------------------------------------------------

    def is_running(self) -> bool:
        """Whether Colima is running — judged by ``colima status``'s EXIT CODE.

        Not by its text: colima (0.10.3) writes every status line to STDERR as
        ``level=info msg="colima is running …"`` and never prints ``status: running``, so the
        former stdout grep was always False — ``launch()`` then ran ``colima start`` against a
        live VM (colima says "already running, ignoring") and, with ``--mount``, colima still
        SAVED the ignored flags into the profile, leaving it claiming a share the VM did not
        have. Exit 0 = running, non-zero = stopped, and it is what :meth:`status` already used.
        """
        try:
            result = subprocess.run(["colima", "status"], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return False
        return result.returncode == 0

    def launch(self, background: bool = True, mounts: Sequence[str] = (), **kwargs: Any) -> None:
        """Start the Docker engine via Colima.

        Args:
            background: Unused (Colima always detaches); kept for the shared launch signature.
            mounts: Extra host directories to share with the VM, in colima's ``DIR[:w]`` syntax
                (bare = read-only). ``$HOME:w`` is always kept alongside them — see the module
                docstring for why. Mounts apply at VM boot: on a running engine that lacks one,
                this refuses and names ``aisland docker restart --mount …`` rather than
                restarting the engine (and every container on it) unasked.
        """
        wanted = [parse_mount(spec) for spec in mounts]  # validated before colima is touched
        if self.is_running():
            configured = self.configured_mounts()
            missing = [mount for mount in wanted if not covers(configured, mount)]
            if missing:
                flags = " ".join(f"--mount {mount.spec}" for mount in wanted)
                raise click.ClickException(
                    "Docker (Colima) is running WITHOUT these VM mounts: "
                    + ", ".join(str(mount.location) for mount in missing)
                    + "\n  Mounts take effect when the VM boots. Restart it (this stops every running container):\n"
                    f"      aisland docker restart {flags}"
                )
            click.echo("Docker (Colima) is already running.")
            self.configure_socket()
            return

        click.echo("Starting Docker (Colima)...")
        try:
            # Colima start itself backgrounds by default; we only check that it succeeds.
            # Colima persists these flags into its profile, so a later bare start keeps the mounts.
            subprocess.check_call(self._start_argv(wanted))

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

    def info(self) -> None:
        """Show detailed Docker (Colima) configuration."""
        click.echo(click.style("Docker (Colima):", bold=True))
        try:
            result = subprocess.run(["colima", "status"], capture_output=True, text=True)
            if result.returncode == 0:
                click.echo(f"  Status        : {click.style('Running', fg='green')}")
            else:
                click.echo(f"  Status        : {click.style('Stopped', fg='red')}")
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    click.echo(f"  colima        : {line}")
        except FileNotFoundError:
            click.echo(f"  Status        : {click.style('Not Installed', fg='yellow')}")
            click.echo("  Install with  : brew install colima")
            return

        click.echo(
            "  VM mounts     : "
            + ", ".join(mount.spec for mount in self.configured_mounts())
            + "  (a container bind mount sees only these; add one with `aisland docker start --mount DIR[:w]`)"
        )
        colima_socket_default = Path.home() / COLIMA_PROFILE_DIR / "docker.sock"
        colima_socket_legacy = Path.home() / ".colima" / "docker.sock"
        colima_socket = colima_socket_default if colima_socket_default.exists() else colima_socket_legacy
        standard_socket = Path("/var/run/docker.sock")
        click.echo(f"  Colima socket : {colima_socket} " + ("(present)" if colima_socket.exists() else "(missing)"))
        link_info = ""
        if standard_socket.is_symlink():
            try:
                link_info = f" -> {os.readlink(standard_socket)}"
            except OSError:
                link_info = ""
        click.echo(
            f"  Docker socket : {standard_socket}{link_info} "
            + ("(present)" if standard_socket.exists() or standard_socket.is_symlink() else "(missing)")
        )

    def configure_socket(self) -> None:
        """Ensure the Docker client is pointing to the standard /var/run/docker.sock."""
        # Colima socket path
        colima_socket = Path.home() / COLIMA_PROFILE_DIR / "docker.sock"
        standard_socket = Path("/var/run/docker.sock")

        if not colima_socket.exists():
            # Fallback for older colima versions/custom profiles
            colima_socket = Path.home() / ".colima" / "docker.sock"

        if not colima_socket.exists():
            click.echo(f"  - Warning: Colima socket not found at {colima_socket}")
            return

        click.echo("Configuring Docker socket symlink...")
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
            subprocess.run(
                ["sudo", "ln", "-s", str(colima_socket), str(standard_socket)],
                check=True,
            )
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
