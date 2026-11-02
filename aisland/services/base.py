"""What every aisland service shares: one process under the supervisor (:mod:`aisland.supervisor`), its data
folder and log, and its start, stop, status, info and reset."""

import os
import shutil
from typing import Any, Dict, List, Optional

import click

from aisland import failures, supervisor
from aisland.paths import DATA_DIR
from aisland.projects import get_workspace_root


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
        env: Optional[Dict[str, str]] = None,
        background: bool = False,
        readiness: Optional[supervisor.Readiness] = None,
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
                failures.report(
                    f"Error: port {readiness['port']} is already in use by {owner_hint} — "
                    f"not managed by the aisland supervisor. Stop that process first "
                    f"(it may be a {self.name} instance started by an older aisland).",
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
            failures.report(
                f"Error: {self.name.capitalize()} failed to start "
                f"(status: {status}, exit code: {exit_code}, restarts: {restarts}). See {self.log_file}.",
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
            failures.report(f"Error: {self.name.capitalize()} did not stop in time.")
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
                failures.warn(
                    f"  - Warning: port {port} is still in use by {owner_hint} — "
                    f"orphaned {self.name} child processes may have survived; stop them manually."
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

    def restart(self, **kwargs: Any) -> None:  # Any: launch's options, each typed by the subclass
        """Restart the service."""
        click.echo(f"Restarting {self.name}...")
        self.stop()
        # We assume launch is the method to start with default/stored config
        # Individual services might need to override this if launch signatures differ significantly
        if hasattr(self, "launch"):
            getattr(self, "launch")(**kwargs)
        else:
            failures.report(f"Error: {self.name} service does not have a launch method.")

    def reset(self) -> None:
        """Stop the service and wipe its data directory, logs, and registration."""
        if self.is_running():
            self.stop()

        # Drop the supervisor registration too — a reset service should not
        # linger (disabled) in the desired-state document.
        if supervisor.remove_process(self.name) and supervisor.is_server_running():
            supervisor.apply()

        if self.service_data_dir.exists():
            click.echo(f"Wiping data for {self.name} at {self.service_data_dir}...")
            shutil.rmtree(self.service_data_dir)
            self.service_data_dir.mkdir(parents=True, exist_ok=True)

        if self.log_file.exists():
            self.log_file.unlink()

        click.echo(f"{self.name.capitalize()} data reset.")
