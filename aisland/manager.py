import hashlib
import json
import os
import platform
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from aisland import supervisor

# Path Configuration (relative to the file itself)
SERVICES_DIR = Path(__file__).parent.absolute()
# The package is at aisland/aisland/manager.py, so project root is SERVICES_DIR.parent
BASE_DIR = SERVICES_DIR.parent.absolute()
BIN_DIR = BASE_DIR / "bin"
DATA_DIR = BASE_DIR / "data"
TOOLS_DIR = BASE_DIR / "tools"

# Registry of tools aisland can install entirely in userspace (no sudo / package
# manager required). Used on Linux where the macOS brew flow is unavailable.
# Each entry pins a specific version AND per-architecture sha256 digests taken
# from the upstream SHASUMS / checksums files. The downloaded tarball is verified
# against its digest BEFORE extraction (see `_install_userspace_tool`); a mismatch
# aborts the install rather than running an unverified binary. `sha256` is keyed
# by the same {arch} token used in the URL — node ships an x64-only asset, so it
# is keyed "amd64". When bumping `version`, refresh the digests from upstream in
# the SAME edit (e.g. `curl -sL <checksums-url>`), or the install will abort.
USERSPACE_TOOLS: dict[str, dict[str, Any]] = {
    "node": {
        "version": "v22.11.0",
        "url": "https://nodejs.org/dist/{version}/node-{version}-linux-x64.tar.xz",
        "extract_subdir": "node-{version}-linux-x64",
        "binaries": ["node", "npm", "npx"],
        # https://nodejs.org/dist/v22.11.0/SHASUMS256.txt
        "sha256": {"amd64": "83bf07dd343002a26211cf1fcd46a9d9534219aad42ee02847816940bf610a72"},
    },
    # The aisland service supervisor (see aisland/supervisor.py). macOS gets it
    # via Homebrew (f1bonacc1/tap, declared in aisland's sys-dependencies);
    # this entry covers Linux. Flat archive: the binary sits at the tarball
    # root (bin_subdir "" — no bin/ directory), and upstream publishes
    # per-architecture assets ({arch} = amd64 / arm64).
    "process-compose": {
        "version": "v1.116.0",
        "url": (
            "https://github.com/F1bonacc1/process-compose/releases/download/"
            "{version}/process-compose_linux_{arch}.tar.gz"
        ),
        "extract_subdir": "process-compose-{version}",
        "bin_subdir": "",
        "binaries": ["process-compose"],
        # github.com/F1bonacc1/process-compose/releases/download/v1.116.0/process-compose_checksums.txt
        "sha256": {
            "amd64": "f3550750dd17c3d0d5e378aa6239b87ee8405dd0eeb460a83b8cc03ef48a87fb",
            "arm64": "b82c4d6c633b4775d6f11c41dc5f7943f7b94a99ff6699298de813cb9c9ba76d",
        },
    },
}

# platform.machine() → the architecture token used in release-asset names.
_LINUX_ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class BaseService:
    """Standardized management logic for aisland services.

    Process supervision (spawn, liveness, readiness, crash recovery, log
    capture, port discovery) is delegated to the process-compose supervisor
    (see :mod:`aisland.supervisor`). Service classes keep the domain logic:
    building the command line, environment, and readiness probe that
    ``launch()`` hands to :meth:`start`.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.service_data_dir = DATA_DIR / name
        self.log_file = DATA_DIR / f"{name}.log"
        self.service_data_dir.mkdir(parents=True, exist_ok=True)

    def is_running(self) -> bool:
        """Check if the service is currently running (per the supervisor)."""
        proc = supervisor.get_process(self.name)
        return bool(proc and proc.get("is_running"))

    def get_pid(self) -> Optional[int]:
        """Return the current PID if running."""
        proc = supervisor.get_process(self.name)
        if proc and proc.get("is_running"):
            return int(proc["pid"])
        return None

    def get_ports(self) -> List[int]:
        """Return the TCP ports the service is listening on (best-effort)."""
        if not self.is_running():
            return []
        return supervisor.get_ports(self.name)

    def start(
        self,
        cmd: List[str],
        env: Optional[dict] = None,
        background: bool = False,
        readiness: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register the service with the supervisor and bring it up.

        Args:
            cmd: argv list for the service process.
            env: Extra environment variables (merged over the supervisor's
                inherited environment). A caller-provided ``PATH`` wins;
                otherwise the workspace venv is prepended so ``python3`` and
                friends resolve to the project-standard environment.
            background: When False, stream the service log to the terminal
                after startup; Ctrl+C stops the service (matching the old
                foreground semantics). The process itself always runs
                detached under the supervisor.
            readiness: Optional HTTP readiness probe ``{"port": int, "path": str}``.
        """
        if self.is_running():
            click.echo(f"{self.name.capitalize()} is already running (PID: {self.get_pid()}).")
            return

        # Pre-flight: a taken port means the service would crash-loop through
        # its whole restart budget with only a log-side error. The usual
        # culprit is an orphan instance the supervisor doesn't know about
        # (e.g. started by a pre-supervisor aisland and never stopped).
        if readiness is not None:
            owner = supervisor.port_in_use(readiness["port"])
            if owner is not None:
                owner_hint = f"PID {owner}" if owner > 0 else "an unknown process"
                click.echo(
                    f"Error: port {readiness['port']} is already in use by {owner_hint} — "
                    f"not managed by the aisland supervisor. Stop that process first "
                    f"(it may be a {self.name} instance started by an older aisland).",
                    err=True,
                )
                return

        click.echo(f"Starting {self.name.capitalize()}...")

        process_env = dict(env or {})
        if "PATH" not in process_env:
            venv_bin = get_workspace_root() / ".venv" / "bin"
            current_path = os.environ.get("PATH", "")
            if str(venv_bin) not in current_path:
                current_path = f"{venv_bin}:{current_path}"
            process_env["PATH"] = current_path

        was_registered = supervisor.get_process(self.name) is not None
        supervisor.register_process(
            self.name,
            cmd,
            env=process_env,
            log_file=self.log_file,
            readiness=readiness,
            enabled=True,
        )
        supervisor.apply()
        if was_registered and not self.is_running():
            # `project update` is a no-op when the config is byte-identical
            # (a re-start after a failed run), so launch the process directly.
            supervisor.start_process(self.name)

        outcome, state = supervisor.wait_for_outcome(self.name)
        if outcome == "failed":
            status = (state or {}).get("status", "unknown")
            exit_code = (state or {}).get("exit_code")
            restarts = (state or {}).get("restarts", 0)
            click.echo(
                f"Error: {self.name.capitalize()} failed to start "
                f"(status: {status}, exit code: {exit_code}, restarts: {restarts}). See {self.log_file}.",
                err=True,
            )
            return

        assert state is not None  # ready/running/pending all carry a state
        click.echo(f"{self.name.capitalize()} started (PID: {state.get('pid')})")
        if outcome == "pending":
            click.echo(f"  - Still initializing (readiness probe pending) — check 'aisland {self.name} status'.")
        click.echo(f"Logs: {self.log_file}")

        if not background:
            click.echo(f"Following logs — press Ctrl+C to stop {self.name.capitalize()}...")
            try:
                supervisor.follow_logs(self.name)
            except KeyboardInterrupt:
                pass
            click.echo("")
            self.stop()

    def stop(self) -> None:
        """Stop the running service (disable it in the desired state)."""
        if not self.is_running():
            click.echo(f"{self.name.capitalize()} is not running.")
            return

        click.echo(f"Stopping {self.name.capitalize()} (PID: {self.get_pid()})...")
        # Disable + apply (not a bare `process stop`): the desired-state
        # document must record the stop, or the next config update would
        # resurrect the process.
        supervisor.set_enabled(self.name, False)
        supervisor.apply()
        state = supervisor.wait_for(self.name, running=False, timeout=15)
        if state is not None and state.get("is_running"):
            click.echo(f"Error: {self.name.capitalize()} did not stop in time.", err=True)
            return
        click.echo(f"{self.name.capitalize()} stopped.")

        # Post-flight: children that escaped the supervisor's process group
        # (e.g. workers re-parented after their launcher died) can survive a
        # stop and silently keep the port. Detect and tell the operator —
        # the next `start` would otherwise fail its port pre-check mysteriously.
        port = supervisor.get_registered_port(self.name)
        if port is not None:
            owner = supervisor.port_in_use(port)
            if owner is not None:
                owner_hint = f"PID {owner}" if owner and owner > 0 else "an unknown process"
                click.echo(
                    f"  - Warning: port {port} is still in use by {owner_hint} — "
                    f"orphaned {self.name} child processes may have survived; stop them manually.",
                    err=True,
                )

    def status(self) -> None:
        """Display service status."""
        proc = supervisor.get_process(self.name)
        running = bool(proc and proc.get("is_running"))
        status_str = click.style("Running", fg="green") if running else click.style("Stopped", fg="red")
        detail = ""
        if running and proc is not None:
            ports = supervisor.get_ports(self.name)
            ready = proc.get("is_ready")
            extras = [f"PID: {proc.get('pid')}"]
            if ports:
                extras.append(f"port: {', '.join(str(p) for p in ports)}")
            if ready and ready != "-":
                extras.append(f"ready: {ready}")
            detail = f"({', '.join(extras)})"
        click.echo(f"  {self.name.capitalize():<15} : {status_str} {detail}")

    def info(self) -> None:
        """Display detailed service information.

        Shows the common attributes every BaseService has: data directory,
        log file, and the live PID/port if running. Subclasses extend this to
        print service-specific details (URLs, credentials, config paths) via
        ``super().info()``.
        """
        click.echo(click.style(f"{self.name.capitalize()}:", bold=True))
        running = self.is_running()
        status_str = click.style("Running", fg="green") if running else click.style("Stopped", fg="red")
        click.echo(f"  Status        : {status_str}")
        if running:
            pid = self.get_pid()
            click.echo(f"  PID           : {pid}")
            ports = self.get_ports()
            if ports:
                click.echo(f"  Listening port: {', '.join(str(p) for p in ports)}")
        click.echo(f"  Data dir      : {self.service_data_dir}")
        click.echo(f"  Log file      : {self.log_file}")
        click.echo(f"  Supervisor    : process-compose ({supervisor.CONFIG_FILE})")

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
        """Stop the service and wipe its data directory, logs, and registration."""
        if self.is_running():
            self.stop()

        # Drop the supervisor registration too — a reset service should not
        # linger (disabled) in the desired-state document.
        if supervisor.remove_process(self.name) and supervisor.is_server_running():
            supervisor.apply()

        if self.service_data_dir.exists():
            import shutil

            click.echo(f"Wiping data for {self.name} at {self.service_data_dir}...")
            shutil.rmtree(self.service_data_dir)
            self.service_data_dir.mkdir(parents=True, exist_ok=True)

        if self.log_file.exists():
            self.log_file.unlink()

        click.echo(f"{self.name.capitalize()} data reset.")


def get_git_email() -> str:
    """Get the git user email from the current project."""
    try:
        # Use workspace root to ensure we get the project's git config
        root = get_workspace_root()
        return subprocess.check_output(["git", "-C", str(root), "config", "user.email"], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "gearlux@gearlux.com"


def get_workspace_root() -> Path:
    """Resolve the absolute path to the workspace root by searching upwards for a marker.

    Marker choice is load-bearing. `TASKS.md` alone is NOT a workspace marker: the
    2026-07-26 backlog restructuring made backlogs PROJECT-OWNED, so every project
    dir carries one and an innermost-match walk stops at a PROJECT. Concretely,
    `aisland/TASKS.md` made this return ``<root>/aisland``, which then named the
    Jenkins orchestrator job "aisland" instead of "acme", set the global
    ``WORKSPACE_ROOT`` env to the aisland dir (so every job's `customWorkspace`
    pointed at ``<root>/aisland/<project>``), and looked for the workspace `.env`
    one level too deep ("No workspace .env found; skipping injection").

    The workspace root is the SUPERPROJECT — the single directory carrying
    ``.gitmodules`` (a submodule has a `.git` *file* and no `.gitmodules`). Prefer
    that; when a checkout genuinely has no submodules, fall back to the OUTERMOST
    `TASKS.md` — never the innermost — so a nested project can't win.
    """
    start = Path(__file__).resolve()
    # start itself is a file; its parents are the candidate directories.
    chain: List[Path] = list(start.parents)[:6]

    # 1. The superproject: the one directory with .gitmodules (+ a TASKS.md, so a
    #    stray .gitmodules elsewhere in the tree can't hijack the result).
    for cand in chain:
        if (cand / ".gitmodules").is_file() and (cand / "TASKS.md").exists():
            return cand

    # 2. No submodules in this checkout: OUTERMOST TASKS.md wins over a project's.
    with_tasks = [cand for cand in chain if (cand / "TASKS.md").exists()]
    if with_tasks:
        return with_tasks[-1]

    # 3. Outermost .git (the superproject's directory, not a submodule's file).
    with_git = [cand for cand in chain if (cand / ".git").exists()]
    if with_git:
        return with_git[-1]

    # 4. Absolute fallback: <root>/aisland/aisland/manager.py -> <root>.
    return start.parents[2]


def get_workspace_name(root: Optional[Path] = None) -> str:
    """Return the name of the workspace (repo name or root directory name)."""
    root = root or get_workspace_root()
    try:
        # Try to get the repo name from git remote
        import subprocess

        output = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
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
    """Scan all projects for [tool.aisland.sys-dependencies] and install them.

    Honoured schema (per pyproject)::

        [tool.aisland.sys-dependencies]
        brew      = ["pkg", ...]   # macOS: installed via Homebrew
        userspace = ["tool", ...]  # Linux: installed via aisland's userspace registry

    Behaviour by platform:
      * Darwin: install ``brew`` deps via Homebrew; ``userspace`` is ignored. Then install
        the Java the Jenkins WAR accepts (:meth:`JenkinsService.provision_java`).
      * Linux:  install ``userspace`` deps via :data:`USERSPACE_TOOLS`; ``brew`` is ignored.
      * Other:  log and skip — no provisioning implemented yet.
    """
    workspace_root = get_workspace_root()
    click.echo(f"==> Scanning for system dependencies in {workspace_root}...")

    import tomllib

    brew_packages: set[str] = set()
    userspace_tools: set[str] = set()

    for pyproject in discover_projects(workspace_root):
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
                sys_deps = data.get("tool", {}).get("aisland", {}).get("sys-dependencies", {})
                brew_deps = sys_deps.get("brew", [])
                if isinstance(brew_deps, list):
                    brew_packages.update(brew_deps)
                user_deps = sys_deps.get("userspace", [])
                if isinstance(user_deps, list):
                    userspace_tools.update(user_deps)
        except Exception as e:
            click.echo(f"  - Warning: Failed to parse {pyproject}: {e}")

    if not brew_packages and not userspace_tools:
        click.echo("  - No system dependencies found.")
        return

    system = platform.system()
    if system == "Darwin":
        if brew_packages:
            click.echo(f"  - Found required brew packages: {', '.join(sorted(brew_packages))}")
            _install_brew_packages(brew_packages)
            _maybe_offer_shell_switch(brew_packages)
        else:
            click.echo("  - No brew packages declared; nothing to install on macOS.")
        # Jenkins' Java is no declared formula: the WAR names the Java it runs on (see
        # services/jenkins_java.py). Imported here because services.jenkins imports this module.
        from aisland.services.jenkins import JenkinsService

        JenkinsService().provision_java()
    elif system == "Linux":
        if userspace_tools:
            click.echo(f"  - Found required userspace tools: {', '.join(sorted(userspace_tools))}")
            for tool in sorted(userspace_tools):
                _install_userspace_tool(tool)
        else:
            click.echo("  - No userspace tools declared; nothing to install on Linux.")
    else:
        click.echo(f"  - sys-deps provisioning not implemented for platform '{system}'; skipping.")


def _install_brew_packages(packages: set) -> None:
    """Install the given packages via Homebrew (macOS).

    Silent no-op if ``brew`` is not on PATH. Idempotent — brew skips packages
    that are already installed.
    """
    try:
        subprocess.check_call(["brew", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        click.echo("  - Error: 'brew' command not found. Homebrew is required for system provisioning.")
        return

    env = os.environ.copy()
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

    for pkg in sorted(packages):
        click.echo(f"==> Installing {pkg} via Homebrew...")
        try:
            subprocess.check_call(["brew", "install", "--quiet", pkg], env=env)
            click.echo(f"  - {pkg} installed successfully.")
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error: Failed to install {pkg}: {e}")


def _sha256(path: Path) -> str:
    """Return the hex sha256 of a file, read in chunks (constant memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_userspace_tool(name: str) -> None:
    """Install a tool from :data:`USERSPACE_TOOLS` entirely in userspace.

    Downloads the upstream tarball into ``aisland/tools/cache/``, extracts it
    under ``aisland/tools/``, and symlinks the named binaries into
    ``aisland/bin/`` (which ``project.bashrc`` puts on ``PATH``). Idempotent:
    if every target binary symlink already exists, the download is skipped.

    Integrity: the tarball (freshly downloaded OR reused from the cache) is
    verified against the pinned per-arch sha256 in :data:`USERSPACE_TOOLS`
    before ANY extraction. A mismatch aborts and deletes the tarball; a tool
    with no pinned digest for this arch installs with a loud warning.
    """
    spec = USERSPACE_TOOLS.get(name)
    if spec is None:
        click.echo(f"  - Error: '{name}' is not a known userspace tool. Known: {sorted(USERSPACE_TOOLS)}")
        return

    version = spec["version"]
    arch = _LINUX_ARCH_ALIASES.get(platform.machine().lower(), "amd64")
    url = spec["url"].format(version=version, arch=arch)
    extract_subdir = spec["extract_subdir"].format(version=version, arch=arch)
    # "" means a flat archive: binaries at the archive root, no bin/ subdir.
    bin_subdir = spec.get("bin_subdir", "bin")
    binaries: list[str] = spec["binaries"]

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    if all((BIN_DIR / b).exists() for b in binaries):
        click.echo(f"  - {name} {version} already installed (symlinks present in {BIN_DIR}).")
        return

    cache_dir = TOOLS_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tarball = cache_dir / Path(url).name

    if not tarball.exists() or tarball.stat().st_size == 0:
        click.echo(f"==> Downloading {name} {version} from {url}...")
        try:
            urllib.request.urlretrieve(url, tarball)
        except Exception as e:
            click.echo(f"  - Error: Failed to download {url}: {e}")
            if tarball.exists():
                tarball.unlink()
            return
        click.echo(f"  - Cached at {tarball}.")
    else:
        click.echo(f"  - Using cached tarball {tarball}.")

    # Integrity gate: verify BEFORE extracting anything. This covers both the
    # fresh download and a reused cache entry (a poisoned cache must not be
    # trusted just because it is on disk). A mismatch => corrupt download or a
    # tampered/re-uploaded asset: drop the tarball so a re-run re-fetches, and
    # refuse to extract. A tool with no pinned digest for this arch installs
    # UNVERIFIED with a loud warning (keeps a not-yet-pinned addition working
    # while making the gap visible) — see the USERSPACE_TOOLS docstring.
    expected = spec.get("sha256", {}).get(arch)
    if expected:
        actual = _sha256(tarball)
        if actual.lower() != expected.lower():
            click.echo(
                f"  - Error: checksum mismatch for {tarball.name} "
                f"(expected {expected}, got {actual}). Refusing to install {name}; "
                f"deleting the tarball — re-run to re-download."
            )
            tarball.unlink()
            return
        click.echo(f"  - Verified {tarball.name} (sha256 OK).")
    else:
        click.echo(
            f"  - Warning: no pinned sha256 for {name}/{arch}; installing UNVERIFIED. "
            f"Add a digest to USERSPACE_TOOLS['{name}']['sha256']['{arch}']."
        )

    extract_root = TOOLS_DIR / extract_subdir
    if not extract_root.exists():
        # A flat archive has no top-level directory of its own, so it must be
        # extracted INTO extract_root; a subdir-style archive (node) brings
        # its own top-level directory and extracts next to it in TOOLS_DIR.
        extract_target = extract_root if not bin_subdir else TOOLS_DIR
        extract_target.mkdir(parents=True, exist_ok=True)
        click.echo(f"==> Extracting {tarball.name} into {extract_target}...")
        try:
            with tarfile.open(tarball, "r:*") as tf:
                # `filter="data"` is the safe default in py3.14+ — block links/devices outside dest.
                tf.extractall(extract_target, filter="data")
        except Exception as e:
            click.echo(f"  - Error: Failed to extract {tarball}: {e}")
            return

    tool_bin_dir = extract_root / bin_subdir if bin_subdir else extract_root
    if not tool_bin_dir.is_dir():
        click.echo(f"  - Error: Expected {tool_bin_dir} to exist after extraction.")
        return

    for binary in binaries:
        src = tool_bin_dir / binary
        dst = BIN_DIR / binary
        if not src.exists():
            click.echo(f"  - Warning: {src} missing in extracted tarball; skipping.")
            continue
        if dst.is_symlink() or dst.exists():
            # Replace stale symlinks; refuse to clobber real files.
            if dst.is_symlink():
                dst.unlink()
            else:
                click.echo(f"  - Error: {dst} exists and is not a symlink; refusing to overwrite.")
                continue
        os.symlink(src, dst)
        click.echo(f"  - linked {binary} -> {src}")

    click.echo(f"==> {name} {version} installed under {extract_root}.")
    click.echo(f"  - Ensure {BIN_DIR} is on PATH (project.bashrc handles this).")


def _maybe_offer_shell_switch(required_pkgs: set) -> None:
    """If ``bash`` is declared as a sys-dep, offer to make brew's bash the login shell.

    No-op on non-macOS (chsh / /etc/shells flow is macOS-specific) and when
    ``bash`` is not among the required packages. Interactive: prompts before
    each side-effecting step (adding to ``/etc/shells`` via sudo, and ``chsh``
    itself which prompts the user for their own password).
    """
    if platform.system() != "Darwin":
        return
    if "bash" not in required_pkgs:
        return

    # brew installs to /opt/homebrew/bin/bash (Apple Silicon) or
    # /usr/local/bin/bash (Intel). Probe both.
    candidates = [Path("/opt/homebrew/bin/bash"), Path("/usr/local/bin/bash")]
    new_bash = next((str(p) for p in candidates if p.exists()), None)
    if not new_bash:
        click.echo("  - Warning: Homebrew bash not found on disk; skipping shell-switch offer.")
        return

    current_shell = os.environ.get("SHELL", "")
    if current_shell == new_bash:
        return  # Already using brew bash — nothing to do.

    click.echo(f"\n==> Modern bash is installed at {new_bash}, but your login shell is {current_shell or '(unknown)'}.")
    if not click.confirm("    Switch your login shell to the new bash?", default=False):
        return

    # /etc/shells gate: chsh refuses any shell not listed there.
    shells_path = Path("/etc/shells")
    listed = False
    if shells_path.exists():
        listed = new_bash in shells_path.read_text().splitlines()
    if not listed:
        click.echo(f"  - {new_bash} is not in /etc/shells (chsh requires it).")
        if not click.confirm("    Add it now? (requires sudo)", default=True):
            click.echo("  - Skipping shell switch.")
            return
        try:
            # `sudo tee -a` is the canonical pattern for appending to a
            # root-owned file with a single password prompt.
            subprocess.run(
                ["sudo", "tee", "-a", str(shells_path)],
                input=f"{new_bash}\n",
                text=True,
                stdout=subprocess.DEVNULL,
                check=True,
            )
            click.echo(f"  - Added {new_bash} to {shells_path}.")
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Failed to update {shells_path}: {e}")
            return

    click.echo("==> Running chsh (will prompt for your USER password, not sudo)...")
    try:
        subprocess.check_call(["chsh", "-s", new_bash])
        click.echo(f"  - Login shell switched to {new_bash}.")
        click.echo("  - Open a new terminal window for it to take effect.")
    except subprocess.CalledProcessError as e:
        click.echo(f"  - chsh failed: {e}")


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

        return {
            "name": name,
            "path": pyproject_path.parent,
            "dependencies": deps,
            "has_dev": "dev" in optional_deps,
        }


def _internal_dependencies(projects: List[dict]) -> Dict[str, List[str]]:
    """Map each project name to the names of the workspace projects it depends on."""
    project_names = {p["name"] for p in projects}
    adj: Dict[str, List[str]] = {p["name"]: [] for p in projects}
    for p in projects:
        for dep in p["dependencies"]:
            # Extract package name from dependency string (e.g., 'loggair>=0.1.0' -> 'loggair')
            dep_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
            if dep_name in project_names and dep_name != p["name"]:
                adj[p["name"]].append(dep_name)
    return adj


def install_groups(projects: List[dict]) -> List[List[dict]]:
    """Group projects into install units, dependencies first; a dependency CYCLE is one unit.

    Tarjan's strongly-connected-components: a cycle (``signals[dev]`` requires ``annotaide``
    and ``annotaide`` requires ``signals``) cannot be installed one project at a time — uv
    resolves the missing half from the package index and fails — so its members must go through
    ONE ``uv pip install -e A -e B``. Every acyclic project is a singleton unit. Tarjan emits a
    component only after everything it depends on, which is exactly install order.
    """
    adj = _internal_dependencies(projects)
    by_name = {p["name"]: p for p in projects}
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: set[str] = set()
    groups: List[List[dict]] = []

    def visit(name: str) -> None:
        index[name] = low[name] = len(index)
        stack.append(name)
        on_stack.add(name)
        for dep in adj[name]:
            if dep not in index:
                visit(dep)
                low[name] = min(low[name], low[dep])
            elif dep in on_stack:
                low[name] = min(low[name], index[dep])
        if low[name] == index[name]:
            members: List[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                members.append(member)
                if member == name:
                    break
            groups.append([by_name[n] for n in reversed(members)])

    for p in projects:
        if p["name"] not in index:
            visit(p["name"])
    return groups


def _run_installs(workspace_root: Path, uv_cmd: str, projects: List[dict]) -> Tuple[List[str], List[str]]:
    """Editable-install every project, one unit per :func:`install_groups` group.

    Returns ``(failed, skipped)``. A project whose dependency failed (or was itself skipped) is
    SKIPPED and named, never attempted: it would fail against the stale index copy of what just
    failed, and a dozen such repeats buried the one real error. Unrelated projects still install.
    """
    from aisland.selection import editable_target  # local: selection imports this module

    adj = _internal_dependencies(projects)
    failed: List[str] = []
    skipped: List[str] = []
    for group in install_groups(projects):
        names = [p["name"] for p in group]
        blocked = sorted({dep for n in names for dep in adj[n] if dep in failed or dep in skipped} - set(names))
        if blocked:
            for n in names:
                click.echo(f"==> Skipping {n}: its dependency {', '.join(blocked)} did not install.")
            skipped.extend(names)
            continue
        # Each target carries every extra the project DECLARES, narrowed to what the machine SELECTED
        # (shared with `aisland source … local`, so the two installs cannot drift).
        targets = [editable_target(workspace_root, p["path"], p["name"]) for p in group]
        suffix = " — a dependency cycle, installed together" if len(group) > 1 else ""
        click.echo(f"==> Installing {', '.join(names)} ({' '.join(targets)}){suffix}...")
        cmd = [uv_cmd, "pip", "install"] + [arg for t in targets for arg in ("-e", t)]
        try:
            # Run from the workspace root so uv reads the generated root uv.toml (the merged
            # private indexes); the editable target is absolute, so cwd doesn't change WHAT installs.
            subprocess.check_call(cmd, cwd=workspace_root)
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error: Failed to install {', '.join(names)}: {e}")
            failed.extend(names)
    return failed, skipped


def get_install_order(workspace_root: Path) -> List[dict]:
    """Determine the correct installation order using topological sort based on workspace dependencies."""
    pyprojects = discover_projects(workspace_root)
    return _dependency_order([get_project_metadata(p) for p in pyprojects])


def _dependency_order(projects: List[dict]) -> List[dict]:
    """``projects`` with every workspace dependency before its dependents; otherwise in the order given."""
    adj = _internal_dependencies(projects)

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


def ensure_uv_in_venv(venv_bin: Path) -> str:
    """Guarantee ``uv`` is present in the workspace venv and return its path.

    Every Jenkins .local job, every ``aisland setup`` run, and every call into the
    workspace's Python tools assumes ``${VENV_BIN}/uv`` exists. If the venv was
    created without uv (or uv got uninstalled by some downstream ``uv pip install``
    shenanigan), this function installs it via the venv's own ``pip``.

    Idempotent: if uv is already executable, returns its path immediately.
    """
    uv_path = venv_bin / "uv"
    if uv_path.exists() and os.access(uv_path, os.X_OK):
        return str(uv_path)

    pip_path = venv_bin / "pip"
    if not pip_path.exists():
        click.echo(
            f"  - Error: {pip_path} not found. Cannot bootstrap uv; " f"the venv is broken. Re-run aisland/setup.sh."
        )
        raise click.ClickException("Workspace venv missing pip")

    click.echo(f"==> Bootstrapping uv into {venv_bin}...")
    subprocess.check_call([str(pip_path), "install", "--quiet", "--upgrade", "uv"])
    return str(uv_path)


def _link_project_bashrc(workspace_root: Path) -> None:
    """Symlink ``<workspace>/project.bashrc`` to the canonical aisland-managed copy.

    The source of truth lives at ``aisland/project.bashrc`` (tracked). The
    workspace-root entry is a symlink so `source ./project.bashrc` from the
    workspace still works for muscle-memory. Replaces a stale symlink in place,
    but refuses to clobber a real file — that would silently destroy the user's
    customizations if they ever predated this migration.
    """
    target = BASE_DIR / "project.bashrc"
    link = workspace_root / "project.bashrc"

    if not target.exists():
        click.echo(f"  - Warning: {target} not found; skipping project.bashrc symlink.")
        return

    if link.is_symlink():
        if link.resolve() == target.resolve():
            return  # Already pointing at the right place.
        link.unlink()
    elif link.exists():
        click.echo(
            f"  - Warning: {link} exists as a regular file. Move or delete it manually so "
            f"`aisland setup` can symlink it to {target}."
        )
        return

    rel = os.path.relpath(target, workspace_root)
    os.symlink(rel, link)
    click.echo(f"==> Linked {link} -> {rel}")


# Shared pyright exclude globs for the root and per-project configs below.
_PYRIGHT_EXCLUDE = [
    "**/__pycache__",
    "**/.venv",
    "**/.*_cache",
    "**/node_modules",
]


def _ensure_gitignore_line(project_dir: Path, line: str) -> None:
    """Append ``line`` to the project's ``.gitignore`` unless already covered (append-only, idempotent).

    A generator that drops an artifact into a COMMITTED repo must also keep that
    artifact out of git — otherwise every setup dirties the tree (the same
    reasoning as the root ``.gitignore`` entries for ``pyrightconfig.json`` /
    ``uv.toml``). Append-only like ``_sync_mypy_path``: existing entries are
    never touched, and an entry with or without the leading ``/`` counts as
    already covering the artifact.
    """
    gitignore = project_dir / ".gitignore"
    covered = {line, line.lstrip("/")}
    if gitignore.exists():
        content = gitignore.read_text()
        if any(entry.strip() in covered for entry in content.splitlines()):
            return
        sep = "" if (not content or content.endswith("\n")) else "\n"
        gitignore.write_text(content + sep + line + "\n")
    else:
        gitignore.write_text(line + "\n")


def _write_pyrightconfig(workspace_root: Path, projects: List[dict]) -> None:
    """Generate ``pyrightconfig.json`` at the workspace root AND inside every project.

    The mono-repo lays each project out as ``<proj>/<pkg>/`` and installs them
    editable into the shared ``.venv``. Pyright / Pylance need each project root
    on ``extraPaths`` to resolve cross-project imports the same way the venv does
    (e.g. ``import confluid`` finds ``confluid/confluid``) — the editable-install
    ``__editable__`` finder hooks are not statically resolvable.

    Two audiences, one generator:
    - **Workspace root** — for editors opened on the whole mono-repo:
      ``venvPath: "."`` + each project dir on ``extraPaths``.
    - **Each project dir** — for editors opened on a single project as its own
      workspace folder (pyright only reads the config at the opened root):
      ``venvPath`` walks back to the shared workspace ``.venv`` and
      ``extraPaths`` lists the sibling projects. This replaces the previously
      COMMITTED per-project ``.vscode/settings.json`` ``extraPaths``, which
      drifted (they still named pre-rename projects) and leaked
      workspace-specific sibling paths to external consumers of a standalone
      project. Note pyright IGNORES ``python.analysis.extraPaths`` from
      ``settings.json`` whenever a ``pyrightconfig.json`` is present, so a
      stale local ``settings.json`` is neutralized, not merged.

    Portability: every path is RELATIVE to the file that carries it — no
    hardcoded absolute path, satisfying the workspace No-Hardcoded-Paths
    mandate. All copies are generated artifacts (gitignored — the root via the
    root ``.gitignore``, each project via ``_ensure_gitignore_line``) and are
    rewritten on every ``aisland setup`` so they always track the live project
    set.

    Plain JSON (no comments): ``pyrightconfig.json`` is consumed by an external
    tool whose comment tolerance we don't control, so provenance is carried by
    the ``.gitignore`` entry + this function rather than an in-file header.
    """
    extra_paths = sorted(os.path.relpath(p["path"], workspace_root) for p in projects)
    config = {
        "venvPath": ".",
        "venv": ".venv",
        "extraPaths": extra_paths,
        "exclude": list(_PYRIGHT_EXCLUDE),
    }
    out = workspace_root / "pyrightconfig.json"
    out.write_text(json.dumps(config, indent=4) + "\n")
    click.echo(f"==> Wrote {out} ({len(extra_paths)} project paths)")

    for project in projects:
        project_dir = Path(project["path"])
        siblings = sorted(os.path.relpath(p["path"], project_dir) for p in projects if Path(p["path"]) != project_dir)
        project_config = {
            "venvPath": os.path.relpath(workspace_root, project_dir),
            "venv": ".venv",
            "extraPaths": siblings,
            "exclude": list(_PYRIGHT_EXCLUDE),
        }
        (project_dir / "pyrightconfig.json").write_text(json.dumps(project_config, indent=4) + "\n")
        _ensure_gitignore_line(project_dir, "/pyrightconfig.json")
    click.echo(f"==> Wrote per-project pyrightconfig.json into {len(projects)} project dir(s)")


def _sync_mypy_path(workspace_root: Path, projects: List[dict]) -> None:
    """Append missing project dirs to the ``mypy_path`` line of the root mypy.ini.

    Unlike ``pyrightconfig.json``, the root ``mypy.ini`` is COMMITTED,
    hand-maintained policy (dozens of per-module ``[mypy-*]`` sections) — not a
    generated artifact. The only part that must track the live project set is
    the ``mypy_path`` search path (the run-mypy-from-workspace-root mandate),
    so setup keeps exactly that line complete. Append-only by design:

    * entries are never removed — inactive submodules (e.g. ``vendorcli`` on a
      non-corporate machine) stay listed, and mypy silently ignores path
      entries that don't exist on this machine, so the committed file stays
      identical across machines instead of churning per activation set;
    * hand-added nested entries (``confluid/confluid``, ``aisland/aisland``)
      are preserved untouched.
    """
    ini = workspace_root / "mypy.ini"
    if not ini.exists():
        click.echo("  - Warning: no mypy.ini at the workspace root; skipping mypy_path sync.")
        return

    lines = ini.read_text().splitlines()
    line_no = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("mypy_path") and "=" in line),
        None,
    )
    if line_no is None:
        click.echo("  - Warning: mypy.ini has no 'mypy_path =' line; skipping mypy_path sync.")
        return

    key, _, value = lines[line_no].partition("=")
    # mypy accepts ':' or ',' as the separator; preserve whichever the file uses.
    separator = "," if "," in value else ":"
    entries = [entry.strip() for entry in value.split(separator) if entry.strip()]

    known = set(entries)
    missing = sorted({rel for p in projects if (rel := os.path.relpath(p["path"], workspace_root)) not in known})
    if not missing:
        return

    lines[line_no] = f"{key.rstrip()} = {separator.join(entries + missing)}"
    ini.write_text("\n".join(lines) + "\n")
    click.echo(f"==> mypy.ini: added {', '.join(missing)} to mypy_path")


def _discover_mcp_servers(workspace_root: Path, venv_bin: Path) -> dict[str, dict]:
    """Discover workspace MCP servers and how to launch them.

    A project is treated as an MCP server when its ``[project.scripts]`` declares
    an entry whose target is ``<pkg>.server:main`` AND that ``server.py`` imports the
    MCP SDK's server package (``mcp.server`` — ``mcp.server.mcpserver.MCPServer`` in
    SDK 2.x, ``mcp.server.fastmcp.FastMCP`` in 1.x; the check reads the PACKAGE, not a
    class name, because the class-name check went stale with the 2.x rename and a
    regeneration silently listed navigaitor alone, 2026-09-05). The script name is the
    console command installed into the venv; the server is keyed by the project name.
    Discovery (not a hand-maintained list) keeps the output tracking the live set
    — drop in a new ``*-mcp`` project and it appears automatically.

    Returns ``{server_name: {"command": "<venv_bin>/<script>"}}`` with ABSOLUTE
    commands resolved against the LOCAL venv. Every client launches each server as a
    subprocess from an arbitrary cwd, so the command must be absolute — which is
    exactly why the client files (``.gemini/settings.json``, ``.mcp.json``) are
    gitignored and regenerated per machine instead of committed.
    """
    import tomllib

    servers: dict[str, dict] = {}
    for pyproject in discover_projects(workspace_root):
        try:
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue
        project = data.get("project", {})
        name = project.get("name")
        scripts = project.get("scripts", {})
        if not name or not isinstance(scripts, dict):
            continue
        for script_name, target in scripts.items():
            module, _, attr = str(target).partition(":")
            if not module.endswith(".server") or attr != "main":
                continue
            server_file = pyproject.parent.joinpath(*module.split(".")).with_suffix(".py")
            try:
                text = server_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if "mcp.server" not in text:
                continue  # a `.server:main` script that is not an MCP server (a web server, say)
            servers[name] = {"command": str(venv_bin / script_name)}
            break  # one MCP server per project
    return servers


def _warn_missing_commands(servers: Dict[str, dict]) -> None:
    """Name every discovered server whose console script is NOT in the venv — the tell of an
    editable install that predates the project's ``[project.scripts]`` entry (measured
    2026-09-05: annotaide's ``annotaide-mcp`` was discovered and written, and did not exist,
    so the client would have spawned a missing binary). The map is still written — the
    reinstall is the fix, and the next ``aisland setup`` performs it."""
    for name, entry in sorted(servers.items()):
        command = Path(entry["command"])
        if not command.exists():
            click.echo(
                f"  - Warning: {name}'s MCP command {command} is not installed — refresh the editable install"
                f" (`uv pip install -e ./{name}`) so the console script exists."
            )


def _write_gemini_settings(workspace_root: Path, venv_bin: Path) -> None:
    """Generate ``<workspace>/.gemini/settings.json`` registering workspace MCP servers.

    Unlike ``pyrightconfig.json`` (relative paths), Gemini CLI launches each
    server as a subprocess, so ``command`` must be an ABSOLUTE venv path. That
    machine-specific absolute is why this file is gitignored and regenerated on
    every ``aisland setup`` (and via ``aisland mcp gemini``) rather than
    committed. The server set is discovered, never hand-maintained.
    """
    servers = _discover_mcp_servers(workspace_root, venv_bin)
    _warn_missing_commands(servers)
    gemini_dir = workspace_root / ".gemini"
    gemini_dir.mkdir(exist_ok=True)
    out = gemini_dir / "settings.json"
    out.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")
    listed = ", ".join(sorted(servers)) or "none"
    click.echo(f"==> Wrote {out} ({len(servers)} MCP server(s): {listed})")


def _write_mcp_json(workspace_root: Path, venv_bin: Path) -> None:
    """Generate ``<workspace>/.mcp.json`` — Claude Code's PROJECT-scoped MCP servers.

    Same discovered server map as the Gemini settings, same absolute venv commands,
    same reason to stay gitignored. Claude Code reads the file when a session starts in
    the workspace and asks once whether to use the project's servers; until this file
    existed no workspace server reached a Claude Code session (``claude mcp list`` showed
    only the user's own servers, measured 2026-09-05).
    """
    servers = _discover_mcp_servers(workspace_root, venv_bin)
    _warn_missing_commands(servers)
    out = workspace_root / ".mcp.json"
    out.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")
    listed = ", ".join(sorted(servers)) or "none"
    click.echo(f"==> Wrote {out} ({len(servers)} MCP server(s): {listed})")


def _venv_bin_or_none(workspace_root: Path) -> Optional[Path]:
    venv_bin = workspace_root / ".venv" / "bin"
    if not venv_bin.exists():
        click.echo(f"Error: Virtual environment not found at {workspace_root / '.venv'}. Run aisland/setup.sh first.")
        return None
    return venv_bin


def generate_gemini_settings() -> None:
    """Regenerate ``.gemini/settings.json`` for the local workspace + venv (standalone command)."""
    workspace_root = get_workspace_root()
    venv_bin = _venv_bin_or_none(workspace_root)
    if venv_bin is not None:
        _write_gemini_settings(workspace_root, venv_bin)


def generate_mcp_json() -> None:
    """Regenerate ``.mcp.json`` (Claude Code) for the local workspace + venv (standalone command)."""
    workspace_root = get_workspace_root()
    venv_bin = _venv_bin_or_none(workspace_root)
    if venv_bin is not None:
        _write_mcp_json(workspace_root, venv_bin)


#: The settings and extension recommendations every machine's workspace file carries. The folder list is the only
#: part written per machine, so the template holds none.
CODE_WORKSPACE_TEMPLATE = BASE_DIR / "code-workspace.json"

# VS Code reads the file as JSONC, so the provenance header can live in the file itself.
_CODE_WORKSPACE_HEADER = """\
// =========================================================================
// AUTO-GENERATED FILE — DO NOT EDIT BY HAND
// Generated by: aisland setup
// Source template: aisland/code-workspace.json (aisland.manager.write_code_workspace)
// To regenerate:  aisland setup
// =========================================================================
"""


def code_workspace_path(workspace_root: Path) -> Path:
    """``<workspace>/<name>.code-workspace``, the name the template's ``${workspaceFolder:<name>}`` references use."""
    return workspace_root / f"{get_workspace_name(workspace_root)}.code-workspace"


def _loggair_log_dir(workspace_root: Path) -> Optional[Path]:
    """The folder loggair writes log files into for a run in this workspace, or None when loggair cannot say.

    Asked the way a run asks: from the workspace root, with the workspace ``.env`` exported over this process's
    environment (``source project.bashrc`` does the same), through loggair's own report
    (``python -m loggair --json``), which configures nothing and creates no folder. Out of process, so aisland
    takes no dependency on loggair; ``-P`` keeps the root's ``loggair/`` checkout from shadowing the package.
    """
    python = workspace_root / ".venv" / "bin" / "python"
    if not python.exists():
        return None
    env = {**os.environ, **supervisor.read_workspace_env(workspace_root)}
    try:
        result = subprocess.run(
            [str(python), "-P", "-m", "loggair", "--json"],
            cwd=workspace_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        click.echo("  - Note: loggair did not report its log folder within 60 s; no log folder listed.")
        return None
    if result.returncode != 0:
        click.echo("  - Note: `python -m loggair --json` failed; no log folder listed.")
        return None
    try:
        log_dir = json.loads(result.stdout).get("resolved", {}).get("log_dir")
    except json.JSONDecodeError:
        log_dir = None
    if not log_dir:
        click.echo("  - Note: loggair's report names no log folder; no log folder listed.")
        return None
    return Path(log_dir)


def write_code_workspace(workspace_root: Path) -> Optional[Path]:
    """Write this machine's VS Code workspace file. Returns its path when the content changed, else None.

    Generated and gitignored like ``pyrightconfig.json``: the folder list follows what is checked out HERE, so a
    per-machine source override shows in the editor without dirtying a tracked file. Folders: the workspace, then
    every project with a checkout in dependency order (sorted by folder name first, so the disk's listing order
    never reorders it), then loggair's log folder when it exists. Settings and extensions come from
    :data:`CODE_WORKSPACE_TEMPLATE`. An unchanged file is not rewritten, so VS Code does not reload it.
    """
    name = get_workspace_name(workspace_root)
    projects = sorted(
        (get_project_metadata(p) for p in discover_projects(workspace_root)), key=lambda p: p["path"].name
    )
    folders = [{"name": name, "path": "."}]
    folders += [
        {"name": p["path"].name, "path": os.path.relpath(p["path"], workspace_root)}
        for p in _dependency_order(projects)
    ]
    log_dir = _loggair_log_dir(workspace_root)
    if log_dir is not None and log_dir.is_dir():
        folders.append({"name": log_dir.name, "path": os.path.relpath(log_dir, workspace_root)})

    document = {"folders": folders, **json.loads(CODE_WORKSPACE_TEMPLATE.read_text())}
    text = _CODE_WORKSPACE_HEADER + json.dumps(document, indent=4) + "\n"
    out = code_workspace_path(workspace_root)
    if out.exists() and out.read_text() == text:
        return None
    out.write_text(text)
    click.echo(f"==> Wrote {out.name} ({len(folders)} folders)")
    return out


def submodule_active_map(workspace_root: Path) -> List[Tuple[str, str, bool]]:
    """Return ``(name, path, active)`` for every submodule declared in ``.gitmodules``.

    ``active`` is the declared intent: ``False`` only when the entry has an explicit
    ``active = false`` key; otherwise ``True`` (git's default — a submodule with no ``active``
    key is active). This is the shared, committed source of truth for which submodules a
    checkout should carry; an ``active = false`` marks one this workspace deliberately skips
    (e.g. an optional or access-gated remote).
    """
    gitmodules = workspace_root / ".gitmodules"
    if not gitmodules.exists():
        return []
    listing = subprocess.run(
        ["git", "config", "-f", str(gitmodules), "--get-regexp", r"^submodule\..*\.path$"],
        cwd=workspace_root,
        text=True,
        capture_output=True,
    ).stdout
    entries: List[Tuple[str, str, bool]] = []
    for line in listing.splitlines():
        key, _, path = line.partition(" ")
        name = key[len("submodule.") : -len(".path")]
        active_raw = subprocess.run(
            ["git", "config", "-f", str(gitmodules), "--get", f"submodule.{name}.active"],
            cwd=workspace_root,
            text=True,
            capture_output=True,
        ).stdout.strip()
        entries.append((name, path, active_raw.lower() != "false"))
    return entries


def reconcile_submodules(workspace_root: Path) -> None:
    """Align each submodule's checkout with its ``.gitmodules`` ``active`` flag.

    * active (``true`` / key absent) and not checked out -> ``git submodule update --init <path>``.
    * inactive (``active = false``) and checked out       -> ``git submodule deinit <path>``.

    Idempotent and non-fatal: an init that fails (e.g. an auth-gated remote unreachable from this
    machine) or a deinit that refuses (a submodule with uncommitted changes) is logged and skipped,
    so one inaccessible/dirty submodule never aborts ``aisland setup``. Keeps ``git submodule
    foreach`` (and tools like push-all) seeing exactly the submodules this workspace declares.
    """
    from aisland.sources import detached_projects

    entries = submodule_active_map(workspace_root)
    if not entries:
        return
    # A ``github`` or ``pypi`` source (see aisland/sources.py) means "consume it as a package;
    # the directory stays empty" — the same working-tree state as ``active = false``. Folding it
    # in here keeps the two mechanisms from fighting: without this, a source-driven deinit would
    # be undone by the very next ``aisland setup``, which would re-init a submodule whose
    # ``.gitmodules`` entry still says active.
    detached = detached_projects(workspace_root)
    click.echo("==> Reconciling submodule checkouts with active flags...")
    for name, path, committed_active in entries:
        # Effective state = the per-machine ``.git/config`` override (set by the
        # activation prompt) when present, else the shared ``.gitmodules`` default.
        # This is what lets one machine activate vendorcli while another skips it,
        # without committing the choice.
        local = _local_submodule_active(workspace_root, name)
        active = committed_active if local is None else local
        if name in detached:
            active = False
        # A submodule's ``.git`` is a gitlink file (or dir) only when it is checked out.
        initialized = (workspace_root / path / ".git").exists()
        if active and not initialized:
            click.echo(f"  - init {path} (active)")
            try:
                subprocess.check_call(["git", "submodule", "update", "--init", path], cwd=workspace_root)
            except subprocess.CalledProcessError as e:
                click.echo(f"    skip {path}: init failed ({e}); remote may be unreachable from this machine.")
        elif not active and initialized:
            click.echo(f"  - deinit {path} (inactive)")
            try:
                # No ``-f``: a deinit that would discard uncommitted work refuses, protecting it.
                subprocess.check_call(["git", "submodule", "deinit", path], cwd=workspace_root)
            except subprocess.CalledProcessError as e:
                click.echo(f"    skip {path}: deinit refused ({e}); resolve local changes and re-run.")


def _local_submodule_active(workspace_root: Path, name: str) -> Optional[bool]:
    """Per-machine activation override from ``.git/config`` (``submodule.<name>.active``).

    Returns ``True`` / ``False`` for an explicit local value, or ``None`` when undecided
    (no local override yet). Read from ``--local`` only, so the shared ``.gitmodules``
    default is NOT consulted here — that's the caller's fallback.
    """
    res = subprocess.run(
        ["git", "config", "--local", "--get", f"submodule.{name}.active"],
        cwd=workspace_root,
        text=True,
        capture_output=True,
    )
    if res.returncode != 0:
        return None
    val = res.stdout.strip().lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    return None


def _set_local_submodule_active(workspace_root: Path, name: str, active: bool) -> None:
    """Record the per-machine activation decision in ``.git/config`` (never committed)."""
    subprocess.run(
        ["git", "config", "--local", f"submodule.{name}.active", "true" if active else "false"],
        cwd=workspace_root,
        check=False,
    )


def _project_is_optional(project_path: Path, committed_active: bool) -> bool:
    """A submodule is *optional* (activation is prompted) when it ships its own ``uv.toml``
    (private-index needs) or is ``active = false`` in ``.gitmodules`` (a deliberately opt-in /
    access-gated remote). Ordinary submodules auto-activate and are never prompted.
    """
    return (project_path / "uv.toml").exists() or committed_active is False


def _resolve_submodule_activation(
    workspace_root: Path,
    non_interactive: bool = False,
    activate: Tuple[str, ...] = (),
    deactivate: Tuple[str, ...] = (),
) -> None:
    """Decide + record (per machine) which OPTIONAL submodules are active, prompting as needed.

    Explicit ``activate`` / ``deactivate`` names are applied first (write the local override,
    no prompt). Each remaining optional submodule is then resolved:

    * already decided locally AND ``non_interactive`` -> keep the saved choice silently;
    * otherwise (interactive, or a NEW/undecided module even under ``non_interactive``) ->
      ``click.confirm`` (default = current effective state) and record the answer.

    Decisions are written to ``.git/config submodule.<name>.active`` (per machine, never
    committed); :func:`reconcile_submodules` then checks out / deinits accordingly.
    """
    entries = submodule_active_map(workspace_root)
    if not entries:
        return
    forced: Dict[str, bool] = {name: True for name in activate}
    forced.update({name: False for name in deactivate})
    optional = [(n, p, c) for n, p, c in entries if _project_is_optional(workspace_root / p, c)]
    if not optional:
        return
    click.echo("==> Resolving optional (corporate/private) submodule activation...")
    for name, _path, committed in optional:
        if name in forced:
            _set_local_submodule_active(workspace_root, name, forced[name])
            click.echo(f"  - {name}: {'activate' if forced[name] else 'deactivate'} (flag)")
            continue
        local = _local_submodule_active(workspace_root, name)
        if non_interactive and local is not None:
            continue  # previously decided — honor it silently
        default = committed if local is None else local
        active = click.confirm(f"  Activate '{name}'? (clone + install + merge its uv.toml)", default=default)
        _set_local_submodule_active(workspace_root, name, active)


# Workspace-wide uv settings that are no single project's concern. Merged into the generated
# root uv.toml beneath every activated project's own uv.toml. The never-true marker
# (`sys_platform == 'never'`) is uv's idiom for "drop this requirement everywhere".
#
# * ``lightning`` — the unified package conflicts with ``pytorch-lightning`` workspace-wide.
# * ``opencv-python`` — it and ``opencv-python-headless`` install the SAME files (both RECORDs
#   claim ``cv2/cv2.abi3.so`` and ``cv2/.dylibs/*``) with DIFFERENT bundled native libraries, so
#   having both installed lets any install that moves one variant leave ``cv2/.dylibs/`` mixed
#   between two bundles — a dlopen failure at ``import cv2`` (seen: a leftover ffmpeg-6
#   ``libavformat.60`` beside the other wheel's ``libsrt.1.5.4``). The workspace keeps HEADLESS
#   only: nothing here needs the GUI build (ultralytics' cv2.imshow paths are all gated behind
#   ``check_imshow``, which try/excepts; first-party code uses cv2.resize and friends).
# * ``opencv-python-headless`` — pinned to the label-studio-sdk-compatible range because
#   ``roboflow`` pins ``==4.10.0.84`` while ``label-studio-sdk`` needs ``>=4.12,<5``: the two are
#   mutually unsatisfiable, so the choice is made explicitly here rather than left to install
#   order (verified: roboflow + rfdetr import fine on 4.13).
_ROOT_UV_BASE: Dict[str, Any] = {
    "override-dependencies": [
        "lightning ; sys_platform == 'never'",
        "opencv-python ; sys_platform == 'never'",
        "opencv-python-headless>=4.12,<5",
    ],
}


def _merge_uv(into: Dict[str, Any], other: Dict[str, Any]) -> None:
    """Merge ``other`` uv-config into ``into``: list values are unioned (dedup, order-preserving);
    scalars are last-wins."""
    for key, value in other.items():
        if isinstance(value, list):
            base = into.get(key)
            base_list = list(base) if isinstance(base, list) else []
            into[key] = base_list + [v for v in value if v not in base_list]
        else:
            into[key] = value


def _emit_uv_toml(data: Dict[str, Any]) -> str:
    """Hand-emit the flat key set uv.toml uses (string scalars + string/scalar lists + bools).

    No `tomli-w` dependency for the realistic case; an unsupported (table/array-of-table) key is
    skipped with a warning rather than mis-serialized — add `tomli-w` if a project ever needs one.
    """
    header = (
        "# =========================================================================\n"
        "# AUTO-GENERATED by `aisland setup` (manager._write_root_uv_toml) — DO NOT EDIT.\n"
        "# Merged from the workspace base + each ACTIVATED project's uv.toml. Regenerate\n"
        "# with `aisland setup`. Gitignored: a private index is present only when its\n"
        "# project is activated, so a machine that skips it never contacts that index.\n"
        "# =========================================================================\n\n"
    )
    lines: List[str] = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        elif isinstance(value, str):
            lines.append(f'{key} = "{value}"')
        elif isinstance(value, list):
            lines.append(f"{key} = [")
            for item in value:
                lines.append(f'    "{item}",')
            lines.append("]")
        else:
            click.echo(f"  - Warning: skipping uv.toml key {key!r} (unsupported type {type(value).__name__})")
    return header + "\n".join(lines) + "\n"


def _write_root_uv_toml(workspace_root: Path, activated: List[dict]) -> None:
    """Generate ``<workspace>/uv.toml`` = workspace base merged with each activated project's uv.toml.

    Gitignored + regenerated on every setup (like ``pyrightconfig.json`` / ``.gemini/settings.json``)
    so a private index (e.g. vendorcli's Company Artifactory URLs) is present ONLY when that project
    is activated — never committed, so a machine that doesn't activate it never stalls on the index.
    """
    import tomllib

    merged: Dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in _ROOT_UV_BASE.items()}
    n_merged = 0
    for project in activated:
        uv_file = Path(project["path"]) / "uv.toml"
        if not uv_file.exists():
            continue
        try:
            with open(uv_file, "rb") as f:
                _merge_uv(merged, tomllib.load(f))
            n_merged += 1
        except (OSError, tomllib.TOMLDecodeError) as e:
            click.echo(f"  - Warning: failed to read {uv_file}: {e}")
    out = workspace_root / "uv.toml"
    out.write_text(_emit_uv_toml(merged))
    extra = len(merged.get("extra-index-url", []) or [])
    click.echo(f"==> Wrote {out} (base + {n_merged} project uv.toml; {extra} extra index URL(s))")


def setup_workspace(
    non_interactive: bool = False,
    activate: Tuple[str, ...] = (),
    deactivate: Tuple[str, ...] = (),
    framework: Tuple[str, ...] = (),
) -> None:
    """Perform a full workspace setup: system provisioning followed by editable pip installs.

    ``framework`` names the ML frameworks to install (``--framework torch --framework keras``).
    Passing any RECORDS the selection in ``config.toml``, so it is a one-time flag rather than
    something to repeat on every run; omitting it uses the recorded selection, and prompts when
    there is none. See :mod:`aisland.frameworks`.
    """
    workspace_root = get_workspace_root()
    venv_bin = workspace_root / ".venv" / "bin"

    if not venv_bin.exists():
        click.echo(f"Error: Virtual environment not found at {workspace_root / '.venv'}. Run aisland/setup.sh first.")
        return

    # 0. Ensure uv is present in the venv — required by everything below and by
    #    every Jenkinsfile.local job that targets this workspace.
    uv_cmd = ensure_uv_in_venv(venv_bin)

    # 0a. Fold any pre-2026-08-01 sources/frameworks/trackers.toml into the single config.toml.
    #     A no-op (and byte-silent) once there is nothing left to migrate — see aisland.config.
    from aisland.config import migrate as migrate_config

    migrate_config(workspace_root)

    # 0b. Symlink the aisland-managed project.bashrc to the workspace root so
    #     users can `source ./project.bashrc` from the workspace as before.
    _link_project_bashrc(workspace_root)

    # 1. System Provisioning
    provision_system()

    # 1b. Optional-submodule activation — prompt (per machine, recorded in .git/config) which
    #     corporate/private submodules to activate, then reconcile checkouts to match: init the
    #     active ones / deinit the inactive ones so the install set (and discovery, push-all,
    #     `git submodule foreach`) matches. Runs before discovery so inactive checkouts drop out.
    _resolve_submodule_activation(workspace_root, non_interactive, activate, deactivate)
    reconcile_submodules(workspace_root)

    # 1c. Per-project sources — put each managed project on its configured branch (git) or
    #     replace it with the published wheel (pypi, which empties its directory). Runs BEFORE
    #     discovery so a pypi-sourced project drops out of the editable-install loop below on
    #     its own, and its wheel is already installed when a consumer's `.[dev]` resolves it.
    from aisland.sources import apply_all

    apply_all(workspace_root, uv_cmd)

    # 1d. Per-project mirrors — each checkout's `mirror` remote follows `[mirrors.*]` (and this
    #     machine's override); a remote whose mirror was unset elsewhere is removed. No network.
    from aisland.mirrors import apply_all as apply_mirrors

    apply_mirrors(workspace_root)

    # 2. Dependency Discovery (now reflects the active checkouts)
    click.echo(f"==> Resolving workspace dependency graph in {workspace_root}...")
    install_order = get_install_order(workspace_root)

    # 2b. Editor config — generate pyrightconfig.json so Pyright/Pylance resolve
    #     cross-project imports against the same project set we install editable.
    _write_pyrightconfig(workspace_root, install_order)

    # 2b'. Keep the COMMITTED root mypy.ini's mypy_path covering every discovered
    #      project (append-only — see _sync_mypy_path for why nothing is removed).
    _sync_mypy_path(workspace_root, install_order)

    # 2b''. Framework selection — which ML frameworks this machine installs. Resolved AFTER
    #       discovery (the vocabulary comes from the discovered projects' declarations) and
    #       BEFORE the install loop, which reads it per project. Prompts once when nothing has
    #       been chosen yet; a recorded choice is used silently.
    from aisland.selection import (
        AXES,
        PLAYWRIGHT_BROWSER,
        browser_install_command,
        require_defined_declarations,
        resolve_for_setup,
    )

    for axis in AXES:
        # Only the framework axis takes a flag; the tracker axis resolves from its recorded
        # selection (or prompts once), which is why `requested` is None for it.
        selected, _just_chosen = resolve_for_setup(
            workspace_root, framework if axis.plural == "frameworks" else None, non_interactive, axis
        )
        if selected:
            click.echo(f"==> {axis.plural.capitalize()}: {', '.join(selected)}")

    # 2c. Generated root uv.toml — workspace base merged with each activated project's uv.toml,
    #     so a private index (e.g. vendorcli's Company Artifactory URLs) is present ONLY when that
    #     project is active. Written BEFORE the install loop so `uv pip install` sees the index.
    _write_root_uv_toml(workspace_root, install_order)

    # 2d. Refuse while any project declares an extra it never defined. Checked over the
    #     WHOLE workspace before ANYTHING is installed, so one run names every project to
    #     fix rather than failing at the twelfth (user ruling 2026-08-25, option B).
    require_defined_declarations(workspace_root)

    # 3. Provisioning (Git & Python)
    for project in install_order:
        project_dir = project["path"]

        # Git Configuration (pull.rebase false)
        try:
            subprocess.check_call(["git", "config", "pull.rebase", "false"], cwd=project_dir)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    # Python provisioning (editable installs): cycles together, dependents of a failure skipped.
    failed, skipped = _run_installs(workspace_root, uv_cmd, install_order)
    if failed or skipped:
        click.echo(f"==> {len(failed)} project(s) failed to install: {', '.join(failed) or '-'}")
        if skipped:
            click.echo(f"==> {len(skipped)} skipped because a dependency failed: {', '.join(skipped)}")

    for project in install_order:
        if project["name"] in failed or project["name"] in skipped:
            continue
        # A `playwright` dev dependency needs its browser fetched once the package is in — the
        # SAME declaration the scaffolded CI reads, so a browser-driven test runs here as it
        # runs there instead of skipping (see `selection.browser_install_command`).
        project_dir = project["path"]
        browser = browser_install_command(project_dir / "pyproject.toml", venv_bin)
        if browser:
            click.echo(f"==> Installing Playwright's {PLAYWRIGHT_BROWSER} for {project['name']}...")
            try:
                subprocess.check_call(browser, cwd=project_dir)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                click.echo(f"  - Error: Failed to install Playwright's {PLAYWRIGHT_BROWSER} for {project['name']}: {e}")

    # 4. MCP client config — register every workspace MCP server (navigaitor,
    #    vendorcli, annotaide, …) for the Gemini CLI AND for Claude Code, with
    #    locally-resolved absolute venv paths. Written after the editable installs
    #    so the console scripts exist.
    _write_gemini_settings(workspace_root, venv_bin)
    _write_mcp_json(workspace_root, venv_bin)

    # 5. The VS Code workspace file — this machine's checkouts, and loggair's log folder, which needs loggair
    #    installed to be asked.
    write_code_workspace(workspace_root)

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
