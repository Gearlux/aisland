"""Docker engine — on macOS in Colima's VM (lifecycle plus the VM mounts a container can bind), on Linux the
system's own engine (:class:`LinuxDockerService`, checked, never managed); :func:`docker_service` picks one.

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

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Union

import click
import yaml

from aisland import console, failures, probe
from aisland.errors import AislandError
from aisland.services.base import BaseService, ServiceState, service_state, styled_state
from aisland.setup import provision

logger = logging.getLogger(__name__)

#: The Colima profile aisland drives (its default profile). ``colima start --save-config``
#: (default true) writes the effective flags — mounts included — into ``colima.yaml`` here.
COLIMA_PROFILE_DIR = Path(".colima") / "default"
#: How long `colima start` may take: a first start downloads the VM image and boots it, minutes on a slow line.
COLIMA_START_SECONDS = 15 * 60
#: How long `colima stop` and `colima delete` may take.
COLIMA_STOP_SECONDS = 5 * 60


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
        raise AislandError(f"--mount {spec}: {path} is not a directory.")
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


#: Where Docker clients look for the engine by default; aisland links it to Colima's socket.
STANDARD_SOCKET = Path("/var/run/docker.sock")


def colima_socket_path() -> Path:
    """Colima's Docker socket: its default profile's, else the one older colima versions put in ``~/.colima``."""
    default = Path.home() / COLIMA_PROFILE_DIR / "docker.sock"
    return default if default.exists() else Path.home() / ".colima" / "docker.sock"


class DockerService(BaseService):
    """Docker Container Engine (via Colima)."""

    START_SECONDS = COLIMA_START_SECONDS

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
        return probe.succeeds(["colima", "status"])

    def launch(self, background: bool = True, mounts: Sequence[str] = (), **kwargs: object) -> None:
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
        for tool in ("colima", "docker"):  # the engine and its client: installed only once the user agrees
            if shutil.which(tool) is None:
                provision.ask_to_install(tool)
        if self.is_running():
            configured = self.configured_mounts()
            missing = [mount for mount in wanted if not covers(configured, mount)]
            if missing:
                flags = " ".join(f"--mount {mount.spec}" for mount in wanted)
                raise AislandError(
                    "Docker (Colima) is running WITHOUT these VM mounts: "
                    + ", ".join(str(mount.location) for mount in missing)
                    + "\n  Mounts take effect when the VM boots. Restart it (this stops every running container):\n"
                    f"      aisland docker restart {flags}"
                )
            console.echo("Docker (Colima) is already running.")
            self.configure_socket()
            return

        console.echo("Starting Docker (Colima)...")
        try:
            # Colima start itself backgrounds by default; we only check that it succeeds.
            # Colima persists these flags into its profile, so a later bare start keeps the mounts.
            subprocess.check_call(self._start_argv(wanted), timeout=COLIMA_START_SECONDS)

            # Ensure Docker socket is configured
            self.configure_socket()

            console.echo("Docker (Colima) started successfully.")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            failures.report(f"Error starting Docker (Colima): {e}")

    def stop(self) -> None:
        """Stop the Docker engine via Colima."""
        if not self.is_running():
            console.echo("Docker (Colima) is not running.")
            return

        console.echo("Stopping Docker (Colima)...")
        try:
            subprocess.check_call(["colima", "stop"], timeout=COLIMA_STOP_SECONDS)
            console.echo("Docker (Colima) stopped.")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            failures.report(f"Error stopping Docker (Colima): {e}")

    def state(self) -> ServiceState:
        """Colima's state: ``not installed`` without it, else :meth:`is_running`'s."""
        if shutil.which("colima") is None:
            return service_state(self.name, "not installed")
        return service_state(self.name, "running" if self.is_running() else "stopped")

    def status(self) -> None:
        """Display Docker engine status (its socket: :meth:`info`)."""
        state = self.state()["state"]
        console.echo(
            f"  Docker (Colima)  : {styled_state(state, 'Not Installed' if state == 'not installed' else None)}"
        )

    def info(self) -> None:
        """Show detailed Docker (Colima) configuration."""
        console.echo(click.style("Docker (Colima):", bold=True))
        if shutil.which("colima") is None:
            console.echo(f"  Status        : {click.style('Not Installed', fg='yellow')}")
            console.echo("  Install with  : brew install colima")
            return
        result = probe.run(["colima", "status"])
        if result is None:
            console.echo(f"  Status        : {click.style(f'No answer within {probe.PROBE_SECONDS:.0f} s', fg='red')}")
            return
        running = click.style("Running", fg="green") if result.returncode == 0 else click.style("Stopped", fg="red")
        console.echo(f"  Status        : {running}")
        for line in (result.stdout or result.stderr).splitlines():  # colima 0.10 says it all on stderr
            if line.strip():
                console.echo(f"  colima        : {line.strip()}")

        console.echo(
            "  VM mounts     : "
            + ", ".join(mount.spec for mount in self.configured_mounts())
            + "  (a container bind mount sees only these; add one with `aisland docker start --mount DIR[:w]`)"
        )
        colima_socket = colima_socket_path()
        standard_socket = STANDARD_SOCKET
        console.echo(f"  Colima socket : {colima_socket} " + ("(present)" if colima_socket.exists() else "(missing)"))
        link_info = ""
        if standard_socket.is_symlink():
            try:
                link_info = f" -> {os.readlink(standard_socket)}"
            except OSError:
                link_info = ""
        console.echo(
            f"  Docker socket : {standard_socket}{link_info} "
            + ("(present)" if standard_socket.exists() or standard_socket.is_symlink() else "(missing)")
        )

    def configure_socket(self) -> None:
        """Ensure the Docker client is pointing to the standard /var/run/docker.sock."""
        colima_socket = colima_socket_path()
        standard_socket = STANDARD_SOCKET

        if not colima_socket.exists():
            failures.warn(f"  - Warning: Colima socket not found at {colima_socket}")
            return

        console.echo("Configuring Docker socket symlink...")
        try:
            if standard_socket.exists() or standard_socket.is_symlink():
                if standard_socket.is_symlink() and os.readlink(standard_socket) == str(colima_socket):
                    logger.debug("%s already links to %s", standard_socket, colima_socket)
                    return
                # Another engine's socket (Docker Desktop's, a link elsewhere): replaced only once the user says so,
                # never by default — that engine's clients would lose it.
                owner = f"a link to {os.readlink(standard_socket)}" if standard_socket.is_symlink() else "a socket"
                if not provision.agree(
                    f"{standard_socket} is {owner}, not Colima's. Replace it with a link to {colima_socket} (sudo)?",
                    default=False,
                ):
                    failures.warn(
                        f"  - Warning: {standard_socket} left as it is ({owner}); clients that use it reach that "
                        f"engine, not Colima. To switch: sudo rm {standard_socket} && "
                        f"sudo ln -s {colima_socket} {standard_socket}"
                    )
                    return
                console.echo(f"  - Removing existing {standard_socket}...")
                subprocess.run(["sudo", "rm", "-f", str(standard_socket)], check=True)
            elif not provision.agree(f"Link {standard_socket} to {colima_socket} (sudo)?"):
                # No one asked (a script, CI, an MCP tool) or a "no": sudo never runs unasked.
                failures.warn(
                    f"  - Warning: {standard_socket} is missing; clients that use it find no engine. To link it to "
                    f"Colima: sudo ln -s {colima_socket} {standard_socket}"
                )
                return

            # Create symlink (requires sudo for /var/run)
            console.echo(f"  - Creating symlink: {standard_socket} -> {colima_socket}")
            subprocess.run(
                ["sudo", "ln", "-s", str(colima_socket), str(standard_socket)],
                check=True,
            )
            console.echo("  - Socket configuration complete.")
        except subprocess.CalledProcessError as e:
            failures.report(f"  - Error configuring socket symlink: {e}")
            console.echo(
                "    (You may need to manually run: sudo ln -s " + str(colima_socket) + " /var/run/docker.sock)"
            )

    def wipes(self) -> str:
        return "the Colima VM, with every container, image and volume in it"

    def reset(self) -> None:
        """Factory reset Colima (wipes all containers/images). The command asked already, naming what goes
        (:meth:`wipes`, in `aisland docker reset` and `aisland services reset`), so this asks nothing more."""
        if self.is_running():
            self.stop()
        console.echo("Deleting Colima instance...")
        try:
            subprocess.check_call(["colima", "delete"], timeout=COLIMA_STOP_SECONDS)
            console.echo("Docker (Colima) reset complete.")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            failures.report(f"Error resetting Docker (Colima): {e}")


#: Where a Linux machine's Docker Engine comes from: the distribution's packages, installed by root, never by aisland.
DOCKER_ENGINE_INSTALL = "https://docs.docker.com/engine/install/"


class LinuxDockerService(BaseService):
    """The Docker Engine a Linux machine runs natively, as a system service.

    No VM sits between it and the host, so there is no Colima and no ``--mount``: a container binds any host folder.
    The engine is the system's — installed, started and stopped by root (``systemctl``) — so aisland only checks that
    it runs and says how to start it; it never installs, starts, stops or wipes it.
    """

    def __init__(self) -> None:
        super().__init__("docker")

    def is_running(self) -> bool:
        """Whether the engine answers — ``docker info``'s exit code (a client without a running daemon fails)."""
        return probe.succeeds(["docker", "info"])

    def launch(self, background: bool = True, mounts: Sequence[str] = (), **kwargs: object) -> None:
        """Check that the engine runs; refuse, naming how to fix it, when it is missing or stopped."""
        if mounts:
            raise AislandError(
                "--mount shares a folder with Colima's VM on macOS; Docker on Linux runs on the host and a container "
                "binds any host folder already"
            )
        if shutil.which("docker") is None:
            raise AislandError(f"Docker is not installed. Install Docker Engine: {DOCKER_ENGINE_INSTALL}")
        if not self.is_running():
            raise AislandError("the Docker engine is not running. Start it: sudo systemctl start docker")
        console.echo("Docker engine is running.")

    def stop(self) -> None:
        console.echo(
            "Docker engine left running: it is the system's, not aisland's (sudo systemctl stop docker stops it)."
        )

    def state(self) -> ServiceState:
        """The system engine's state: ``not installed`` without the ``docker`` client, else whether it answers."""
        if shutil.which("docker") is None:
            return service_state(self.name, "not installed")
        return service_state(self.name, "running" if self.is_running() else "stopped")

    def status(self) -> None:
        state = self.state()["state"]
        console.echo(
            f"  Docker (engine)  : {styled_state(state, 'Not Installed' if state == 'not installed' else None)}"
        )

    def info(self) -> None:
        console.echo(click.style("Docker (engine):", bold=True))
        if shutil.which("docker") is None:
            console.echo(f"  Status        : {click.style('Not Installed', fg='yellow')}")
            console.echo(f"  Install with  : {DOCKER_ENGINE_INSTALL}")
            return
        running = self.is_running()
        console.echo(
            f"  Status        : {click.style('Running', fg='green') if running else click.style('Stopped', fg='red')}"
        )
        if not running:
            console.echo("  Start with    : sudo systemctl start docker")

    def wipes(self) -> str:
        return "nothing: aisland refuses to wipe the system's Docker engine"

    def reset(self) -> None:
        raise AislandError(
            "aisland does not wipe the system's Docker engine; `docker system prune --all --volumes` deletes its "
            "containers, images and volumes"
        )


def docker_service() -> Union[DockerService, LinuxDockerService]:
    """This machine's container engine: the system's Docker Engine on Linux, Docker in Colima's VM elsewhere (macOS)."""
    return LinuxDockerService() if platform.system() == "Linux" else DockerService()
