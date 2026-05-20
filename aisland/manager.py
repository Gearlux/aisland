import os
import platform
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
                # Stale or unparseable PID file — drop it so we don't keep
                # printing "already running (PID: <ghost>)".
                try:
                    self.pid_file.unlink()
                except OSError:
                    pass

        # 2. Fallback: Search for process by name/pattern
        return self._find_process_by_pattern()

    def _find_process_by_pattern(self) -> bool:
        """Search for a running process that looks like this service using pgrep.

        The pattern must be specific enough that the running ``aisland <svc>``
        command itself does not match — otherwise the CLI detects itself and
        falsely reports the service as already running.
        """
        try:
            patterns = {
                "jenkins": "jenkins.war",
                "mlflow": "mlflow server",
                # The actual binary name is ``label-studio`` (with dash);
                # ``labelstudio`` (no dash) would also match the aisland CLI's
                # own argv, the .pid file path, etc.
                "labelstudio": "label-studio start",
            }
            pattern = patterns.get(self.name, self.name)

            # Use pgrep -f to search the full command line, excluding our own
            # process tree so the running CLI invocation isn't a self-match.
            cmd = ["pgrep", "-f", pattern]
            output = subprocess.check_output(cmd, text=True).strip()

            if output:
                own_pid = os.getpid()
                own_pgid = os.getpgrp()
                for raw in output.splitlines():
                    try:
                        pid = int(raw)
                    except ValueError:
                        continue
                    if pid == own_pid:
                        continue
                    try:
                        if os.getpgid(pid) == own_pgid:
                            continue
                    except OSError:
                        continue
                    self.pid_file.write_text(str(pid))
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
        detail = ""
        if running:
            pid = self.get_pid()
            ports = self._get_listening_ports(pid) if pid is not None else []
            if ports:
                ports_str = ", ".join(str(p) for p in ports)
                detail = f"(PID: {pid}, port: {ports_str})"
            else:
                detail = f"(PID: {pid})"
        click.echo(f"  {self.name.capitalize():<15} : {status_str} {detail}")

    @staticmethod
    def _get_listening_ports(pid: int) -> List[int]:
        """Return the TCP ports the given PID (or its children) is LISTENing on.

        Uses ``lsof`` on Unix and ``netstat`` on Windows. Best-effort: if the
        underlying tool is missing or fails, returns an empty list.
        """
        ports: List[int] = []
        try:
            if os.name == "nt":
                # Windows: netstat -ano | findstr LISTENING + filter by PID
                result = subprocess.run(
                    ["netstat", "-ano", "-p", "TCP"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4] == str(pid):
                        local_addr = parts[1]
                        if ":" in local_addr:
                            try:
                                ports.append(int(local_addr.rsplit(":", 1)[1]))
                            except ValueError:
                                continue
            else:
                # Unix: include child processes via -R, then filter listening sockets.
                # -F pn formats output as machine-readable: p<pid>\nn<addr>:port\n...
                pids_to_check = [str(pid)] + BaseService._get_descendant_pids(pid)
                result = subprocess.run(
                    ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", ",".join(pids_to_check), "-F", "n"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                for line in result.stdout.splitlines():
                    if not line.startswith("n"):
                        continue
                    addr = line[1:]
                    if ":" not in addr:
                        continue
                    port_str = addr.rsplit(":", 1)[1]
                    try:
                        port = int(port_str)
                    except ValueError:
                        continue
                    if port not in ports:
                        ports.append(port)
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return []
        return sorted(ports)

    @staticmethod
    def _get_descendant_pids(pid: int) -> List[str]:
        """Return descendant PIDs of ``pid`` using ``pgrep -P`` (Unix only)."""
        descendants: List[str] = []
        stack = [pid]
        while stack:
            parent = stack.pop()
            try:
                output = subprocess.check_output(["pgrep", "-P", str(parent)], text=True).strip()
            except (subprocess.CalledProcessError, FileNotFoundError, OSError):
                continue
            for raw in output.splitlines():
                try:
                    child = int(raw)
                except ValueError:
                    continue
                descendants.append(str(child))
                stack.append(child)
        return descendants

    def info(self) -> None:
        """Display detailed service information.

        Shows the common attributes every BaseService has: data directory,
        log file, PID file, and the live PID/port if running. Subclasses
        extend this to print service-specific details (URLs, credentials,
        config paths) via ``super().info()``.
        """
        click.echo(click.style(f"{self.name.capitalize()}:", bold=True))
        running = self.is_running()
        status_str = click.style("Running", fg="green") if running else click.style("Stopped", fg="red")
        click.echo(f"  Status        : {status_str}")
        if running:
            pid = self.get_pid()
            click.echo(f"  PID           : {pid}")
            if pid is not None:
                ports = self._get_listening_ports(pid)
                if ports:
                    click.echo(f"  Listening port: {', '.join(str(p) for p in ports)}")
        click.echo(f"  Data dir      : {self.service_data_dir}")
        click.echo(f"  Log file      : {self.log_file}")
        click.echo(f"  PID file      : {self.pid_file}")

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


def get_git_email() -> str:
    """Get the git user email from the current project."""
    try:
        # Use workspace root to ensure we get the project's git config
        root = get_workspace_root()
        return subprocess.check_output(["git", "-C", str(root), "config", "user.email"], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "gearlux@gearlux.com"


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

    # Post-install: if `bash` is in the required deps, offer to make brew's
    # bash the login shell — macOS ships bash 3.2 which is missing features
    # (e.g. `compopt`) that modern CLI completions rely on.
    _maybe_offer_shell_switch(brew_packages)


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


def setup_workspace() -> None:
    """Perform a full workspace setup: system provisioning followed by editable pip installs."""
    workspace_root = get_workspace_root()
    venv_bin = workspace_root / ".venv" / "bin"

    if not venv_bin.exists():
        click.echo(f"Error: Virtual environment not found at {workspace_root / '.venv'}. Run aisland/setup.sh first.")
        return

    # 0. Ensure uv is present in the venv — required by everything below and by
    #    every Jenkinsfile.local job that targets this workspace.
    uv_cmd = ensure_uv_in_venv(venv_bin)

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
            subprocess.check_call([uv_cmd, "pip", "install", "-e", target])
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
