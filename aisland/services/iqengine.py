"""IQEngine — the SigMF spectrogram/annotation viewer, run as one container over a local directory.

`IQEngine <https://github.com/IQEngine/IQEngine>`_ is a web app for browsing SigMF recordings:
spectrogram + time/frequency/IQ plots, the recording table with annotation counts, and the
annotation boxes drawn on the spectrogram (jump-to-annotation from the table). Its Docker
image bundles frontend and API; pointed at a directory through
``IQENGINE_BACKEND_LOCAL_FILEPATH`` it walks that directory for ``*.sigmf-meta`` and serves the
pairs it finds — which makes it the quickest way to LOOK at what a SigMF export produced.

What this service is, and is not (all measured 2026-08-17 against the pinned image):

- **One ``docker run``, nothing vendored.** Upstream documents exactly this shape
  (``docker run --env-file .env -v <dir>:/tmp/myrecordings -p 3000:3000 … iqengine``); the
  only settings a local viewer needs are ``IN_MEMORY_DB=1`` (no Mongo — the metadata index is
  rebuilt from the directory at every start) and the local-backend path.
- **The image is pinned by VERSION and DIGEST** (:data:`IQENGINE_VERSION`,
  :data:`IQENGINE_DIGEST`) — the checksum rule ``manager.USERSPACE_TOOLS`` applies to
  downloaded binaries. ``ghcr.io/iqengine/iqengine:latest`` is a 2023 build and ``:pre`` moves
  with every merge; the digest is what makes ``aisland iqengine start`` run the same thing
  next month. Bump both in the same edit (``docker manifest inspect`` or the ghcr manifest
  endpoint gives the digest for a tag).
- **The directory is mounted READ-ONLY** because IQEngine never writes to its local backend:
  its ``upload_blob`` returns immediately for ``account == "local"`` and an annotation edit in
  the UI is a ``PUT …/meta`` into the (in-memory) DB — gone at the next start. Saying ``:ro``
  makes that honest and protects the recordings from a future upstream change.
- **A viewer needs the DATA file.** IQEngine's local sync SKIPS a sidecar whose
  ``<name>.sigmf-data`` is missing (``[SYNC] Data file … does not exist for metadata file``),
  and it ignores SigMF's ``core:dataset``. An annotation-only export (sidecars pointing at raw
  IQ elsewhere) therefore lists NOTHING; :meth:`IQEngineService.launch` counts the incomplete
  pairs and says so, because an empty table gives no hint.
- **The Colima VM must SHARE the directory.** A bind mount from a path Colima does not share
  comes up EMPTY inside the container with no error anywhere (Colima shares only ``$HOME`` by
  default; see :mod:`aisland.services.docker`). ``launch`` compares the host-side sidecar
  count with what the container sees and, on a mismatch, names the fix —
  ``aisland docker restart --mount <dir>:r`` — instead of showing an empty table.

Docker owns the lifecycle (``docker run`` / ``rm``), not the process-compose supervisor, so
this class overrides ``launch`` / ``stop`` / ``status`` — the same shape
:class:`~aisland.services.docker.DockerService` uses for Colima.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, List, Optional, Tuple

import click

from aisland.manager import BaseService

#: The image, pinned. Bump VERSION and DIGEST together — a tag alone can move.
IQENGINE_IMAGE = "ghcr.io/iqengine/iqengine"
IQENGINE_VERSION = "202512200500.0.0-pre-202512200500"
#: ``docker-content-digest`` of that tag's manifest (verified 2026-08-17). Upstream publishes
#: ONE platform, so the digest is a plain image manifest, not a multi-arch index.
IQENGINE_DIGEST = "sha256:1b129f6e5e609c9abbd3ca1f2f3dbef533b31b95173f8845c5610ae202236f42"
#: The only platform upstream builds; stated explicitly so Docker runs it under emulation on
#: Apple silicon without the "requested image's platform does not match" warning.
IQENGINE_PLATFORM = "linux/amd64"

#: IQEngine's own port inside the container, and the default host port.
DEFAULT_PORT = 3000
#: Where upstream's docs mount the recordings; the value of ``IQENGINE_BACKEND_LOCAL_FILEPATH``.
CONTAINER_RECORDINGS = "/tmp/myrecordings"
CONTAINER_NAME = "aisland-iqengine"

#: Workspace ``.env`` key recording the served directory, so the choice survives restarts and
#: a later ``aisland iqengine start`` with no ``--recordings`` serves the same place.
RECORDINGS_ENV_KEY = "IQENGINE_RECORDINGS"

#: Seconds to wait for ``/api/status`` (the amd64 image runs under emulation on Apple silicon;
#: measured ~20 s to first answer).
API_READY_TIMEOUT = 120.0


def image_ref() -> str:
    """``ghcr.io/iqengine/iqengine:<version>@<digest>`` — the tag documents, the digest pins."""
    return f"{IQENGINE_IMAGE}:{IQENGINE_VERSION}@{IQENGINE_DIGEST}"


def _sidecar_pairs(directory: Path) -> Tuple[int, int]:
    """``(sidecars, complete)`` — ``*.sigmf-meta`` found recursively, and how many have their ``.sigmf-data``.

    The same walk IQEngine's local sync does (``os.walk`` for ``.sigmf-meta``, then a
    ``<name>.sigmf-data`` existence check), so the numbers predict what the UI will list.
    """
    sidecars = complete = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.endswith(".sigmf-meta"):
                sidecars += 1
                if (Path(root) / (name[: -len(".sigmf-meta")] + ".sigmf-data")).is_file():
                    complete += 1
    return sidecars, complete


class IQEngineService(BaseService):
    """IQEngine SigMF viewer (one pinned container over a read-only local directory)."""

    def __init__(self) -> None:
        super().__init__("iqengine")

    # -- preconditions --------------------------------------------------------------------

    def setup(self) -> None:
        """Check the ``docker`` CLI and a reachable daemon — install neither."""
        if shutil.which("docker") is None:
            raise click.ClickException(
                "The `docker` CLI was not found.\n"
                "  It is declared in aisland's sys-dependencies; install and link it with:\n"
                "      brew install docker docker-compose && brew link --overwrite docker"
            )
        probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if probe.returncode != 0:
            raise click.ClickException(
                "The Docker daemon is not reachable.\n  Start it with:\n      aisland docker start"
            )

    # -- the directory --------------------------------------------------------------------

    def resolve_recordings(self, explicit: Optional[str]) -> Path:
        """The directory to serve: ``--recordings`` > ``$IQENGINE_RECORDINGS`` in ``.env`` > the service default.

        Symlinks are resolved because the container (and the Colima VM under it) sees the
        REAL path; only the default is created for you — a directory the user named must
        exist, since serving a freshly-created empty one would just look like a broken mount.
        """
        from aisland.supervisor import read_workspace_env

        if explicit is None:
            explicit = read_workspace_env().get(RECORDINGS_ENV_KEY) or None
        if explicit is None:
            default = self.service_data_dir / "recordings"
            default.mkdir(parents=True, exist_ok=True)
            return default
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise click.ClickException(f"--recordings: {path} is not a directory.")
        return path

    # -- docker ---------------------------------------------------------------------------

    def _run_argv(self, recordings: Path, port: int) -> List[str]:
        return [
            "docker",
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "--platform",
            IQENGINE_PLATFORM,
            "-p",
            f"{port}:{DEFAULT_PORT}",
            "-e",
            "IN_MEMORY_DB=1",
            "-e",
            f"IQENGINE_BACKEND_LOCAL_FILEPATH={CONTAINER_RECORDINGS}",
            "-v",
            f"{recordings}:{CONTAINER_RECORDINGS}:ro",
            image_ref(),
        ]

    def _container_state(self) -> Optional[str]:
        """``running`` / ``exited`` / … per ``docker ps -a``, or ``None`` when no such container exists."""
        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$", "--format", "{{.State}}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        state = result.stdout.strip()
        return state or None

    def _container_sidecar_count(self) -> int:
        """How many ``*.sigmf-meta`` the CONTAINER sees under its mount — 0 while the host has some means an empty share."""
        result = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "sh", "-c", f'find {CONTAINER_RECORDINGS} -name "*.sigmf-meta" | wc -l'],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return -1  # could not tell; never mistaken for "saw none"
        try:
            return int(result.stdout.strip())
        except ValueError:
            return -1

    def _wait_for_api(self, port: int, timeout: float = API_READY_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/api/status", timeout=5) as response:
                    if response.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(2)
        return False

    # -- lifecycle ------------------------------------------------------------------------

    def launch(
        self, recordings: Optional[str] = None, port: int = DEFAULT_PORT, background: bool = True, **kwargs: Any
    ) -> None:
        """Start the viewer over ``recordings`` (always detached) and diagnose the two silent-empty cases."""
        from aisland import supervisor
        from aisland.supervisor import write_workspace_env

        self.setup()
        if self._container_state() == "running":
            click.echo(
                f"IQEngine is already running: http://localhost:{port}  (`aisland iqengine stop` first to change it)"
            )
            return

        target = self.resolve_recordings(recordings)
        write_workspace_env({RECORDINGS_ENV_KEY: str(target)})

        owner = supervisor.port_in_use(port)
        if owner is not None:
            raise click.ClickException(
                f"Port {port} is already in use (PID {owner if owner > 0 else 'unknown'}). "
                "Stop that process or pass --port."
            )
        if self._container_state() is not None:  # a stopped leftover would make `--name` collide
            subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, check=False)

        sidecars, complete = _sidecar_pairs(target)
        click.echo(f"IQEngine: http://localhost:{port}")
        click.echo(f"  Recordings: {target}  (read-only; remembered as {RECORDINGS_ENV_KEY} in .env)")
        try:
            subprocess.check_call(self._run_argv(target, port))
        except subprocess.CalledProcessError as exc:
            raise click.ClickException(f"`docker run` failed: {exc}")

        click.echo("  Waiting for the API (the image is amd64; ~20 s under emulation)...")
        if not self._wait_for_api(port):
            click.echo(f"  - Not answering yet; check `docker logs {CONTAINER_NAME}`.")
            return

        # Silent-empty case 1: the VM does not share the directory, so the bind mount is empty.
        seen = self._container_sidecar_count()
        if sidecars and seen == 0:
            self.stop()
            raise click.ClickException(
                f"The container sees NO sidecars under {target} while the host has {sidecars}.\n"
                "  The Docker VM does not share that directory (Colima shares only $HOME by default),\n"
                "  so the bind mount came up empty. Share it, then start again:\n"
                f"      aisland docker restart --mount {target}:r\n"
                "      aisland iqengine start"
            )
        # Silent-empty case 2: sidecars without their data file — IQEngine's sync skips them.
        if sidecars == 0:
            click.echo(f"  - {target} holds no .sigmf-meta files yet; the table will be empty until it does.")
        elif complete < sidecars:
            click.echo(
                f"  - {sidecars - complete} of {sidecars} sidecars have no .sigmf-data beside them; IQEngine lists "
                f"only complete pairs ({complete}). An annotation-only export needs the IQ payload written next to "
                "each sidecar."
            )
        else:
            click.echo(f"  {complete} recording(s) indexed.")
        click.echo(f"IQEngine started. Open http://localhost:{port} → 'Local to Backend'.")

    def stop(self) -> None:
        if self._container_state() is None:
            click.echo("IQEngine is not running.")
            return
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, check=False)
        click.echo("IQEngine stopped.")

    def status(self) -> None:
        state = self._container_state()
        if state is None:
            click.echo("  IQEngine        : Not running (run `aisland iqengine start`)")
            return
        colour = "green" if state == "running" else "yellow"
        click.echo(f"  IQEngine        : {click.style(state.capitalize(), fg=colour)}  ({CONTAINER_NAME})")

    def info(self) -> None:
        # NOT `super().info()`: the base prints a "Supervisor: process-compose" line, and this
        # container is Docker's. Saying otherwise sends an operator to the wrong tool.
        from aisland.supervisor import read_workspace_env

        click.echo(f"  Service       : {self.name}")
        click.echo(f"  Data dir      : {self.service_data_dir}")
        click.echo("  Lifecycle     : docker run/rm (NOT the process-compose supervisor)")
        click.echo(f"  Image         : {image_ref()}")
        click.echo(f"  Container     : {CONTAINER_NAME}  ->  {CONTAINER_RECORDINGS} (read-only bind mount)")
        recordings = read_workspace_env().get(RECORDINGS_ENV_KEY) or f"{self.service_data_dir / 'recordings'} (default)"
        click.echo(f"  Recordings    : {recordings}  ({RECORDINGS_ENV_KEY} in .env)")
        click.echo(f"  URL           : http://localhost:{DEFAULT_PORT} (or --port)")

    def reset(self) -> None:
        """Remove the container and the pinned image (re-pulled on the next start). The recordings are NOT touched."""
        if not click.confirm(f"Remove the IQEngine container and the {image_ref()} image? (recordings are kept)"):
            return
        if self._container_state() is not None:
            subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, check=False)
        subprocess.run(["docker", "image", "rm", image_ref()], capture_output=True, check=False)
        click.echo("IQEngine container and image removed; the recordings directory was left alone.")
