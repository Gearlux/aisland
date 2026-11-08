"""What every aisland service shares: one process under the supervisor (:mod:`aisland.services.supervisor`), its data
folder and log, and its start, stop, status, info and reset."""

import os
import shutil
from typing import Any, Dict, List, Literal, Optional, Sequence, TypedDict

import click

from aisland import console, failures, paths, venv
from aisland.services import supervisor
from aisland.workspace.projects import get_workspace_root

#: What a service's state can be: ``not installed`` (its program is missing), ``not supported`` (not on this platform),
#: ``unknown`` (a project's service whose processes are not the supervisor's; its own ``status`` prints the detail).
State = Literal["running", "stopped", "not installed", "not supported", "unknown"]

_STATE_STYLE = {"running": "green", "stopped": "red"}  # the others: yellow


class ServiceState(TypedDict):
    """One service's state, as ``aisland services status --json`` and the MCP tool ``services_status`` give it."""

    service: str
    state: State
    pid: Optional[int]
    ports: List[int]
    #: The supervisor's readiness answer (``Ready``, ``Not Ready``) for a service with a probe, else ``None``.
    ready: Optional[str]


def service_state(
    name: str, state: State, pid: Optional[int] = None, ports: Sequence[int] = (), ready: Optional[str] = None
) -> ServiceState:
    return {"service": name, "state": state, "pid": pid, "ports": list(ports), "ready": ready}


def styled_state(state: State, words: Optional[str] = None) -> str:
    """``state`` as ``status`` prints it: capitalised (or ``words``), green running, red stopped, else yellow."""
    return click.style(words or state.capitalize(), fg=_STATE_STYLE.get(state, "yellow"))


class BaseService:
    """Standardized management logic for aisland services.

    Process supervision (spawn, liveness, readiness, crash recovery, log
    capture, port discovery) is delegated to the process-compose supervisor
    (see :mod:`aisland.services.supervisor`). Service classes keep the domain logic:
    building the command line, environment, and readiness probe that
    ``launch()`` hands to :meth:`start`.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.service_data_dir = paths.data_dir() / name
        self.log_file = paths.data_dir() / f"{name}.log"
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
            console.echo(f"{self.name.capitalize()} is already running (PID: {self.get_pid()}).")
            return
        if readiness is not None and self._port_taken(readiness["port"]):
            return
        console.echo(f"Starting {self.name.capitalize()}...")
        self._register_and_apply(cmd, self._process_env(env), readiness)
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
        console.echo(f"{self.name.capitalize()} started (PID: {state.get('pid')})")
        if outcome == "pending":
            console.echo(f"  - Still initializing (readiness probe pending) — check 'aisland {self.name} status'.")
        console.echo(f"Logs: {self.log_file}")
        if not background:
            self._follow_then_stop()

    def _port_taken(self, port: int) -> bool:
        """Whether ``port`` is held by a process the supervisor does not know — reported, since the service would
        otherwise crash-loop through its whole restart budget with only a log-side error. The usual culprit is an
        orphan instance (e.g. started by a pre-supervisor aisland and never stopped)."""
        owner = supervisor.port_in_use(port)
        if owner is None:
            return False
        owner_hint = f"PID {owner}" if owner > 0 else "an unknown process"
        failures.report(
            f"Error: port {port} is already in use by {owner_hint} — "
            f"not managed by the aisland supervisor. Stop that process first "
            f"(it may be a {self.name} instance started by an older aisland).",
        )
        return True

    @staticmethod
    def _process_env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
        """``env``, with the workspace venv put first on ``PATH`` unless the caller gave a ``PATH`` of its own."""
        process_env = dict(env or {})
        if "PATH" not in process_env:
            venv_bin = venv.venv_bin(get_workspace_root())
            current_path = os.environ.get("PATH", "")
            if str(venv_bin) not in current_path:
                current_path = f"{venv_bin}:{current_path}"
            process_env["PATH"] = current_path
        return process_env

    def _register_and_apply(
        self, cmd: List[str], env: Dict[str, str], readiness: Optional[supervisor.Readiness]
    ) -> None:
        """Register the process with the supervisor, enabled, and apply the document."""
        was_registered = supervisor.get_process(self.name) is not None
        supervisor.register_process(
            self.name,
            cmd,
            env=env,
            log_file=self.log_file,
            readiness=readiness,
            enabled=True,
        )
        supervisor.apply()
        if was_registered and not self.is_running():
            # `project update` is a no-op when the config is byte-identical
            # (a re-start after a failed run), so launch the process directly.
            supervisor.start_process(self.name)

    def _follow_then_stop(self) -> None:
        """Stream the service's log until Ctrl+C, then stop the service (the old foreground meaning)."""
        console.echo(f"Following logs — press Ctrl+C to stop {self.name.capitalize()}...")
        try:
            supervisor.follow_logs(self.name)
        except KeyboardInterrupt:
            pass
        console.echo("")
        self.stop()

    def stop(self) -> None:
        """Stop the running service (disable it in the desired state)."""
        if not self.is_running():
            console.echo(f"{self.name.capitalize()} is not running.")
            return

        console.echo(f"Stopping {self.name.capitalize()} (PID: {self.get_pid()})...")
        # Disable + apply (not a bare `process stop`): the desired-state
        # document must record the stop, or the next config update would
        # resurrect the process.
        supervisor.set_enabled(self.name, False)
        supervisor.apply()
        state = supervisor.wait_for(self.name, running=False, timeout=15)
        if state is not None and state.get("is_running"):
            failures.report(f"Error: {self.name.capitalize()} did not stop in time.")
            return
        console.echo(f"{self.name.capitalize()} stopped.")

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

    def state(self) -> ServiceState:
        """Its state, as the supervisor knows it."""
        proc = supervisor.get_process(self.name)
        if not (proc and proc.get("is_running")):
            return service_state(self.name, "stopped")
        ready = proc.get("is_ready")
        return service_state(
            self.name,
            "running",
            pid=int(proc["pid"]) if proc.get("pid") else None,
            ports=supervisor.get_ports(self.name),
            ready=ready if ready and ready != "-" else None,
        )

    def status(self) -> None:
        """Display service status: :meth:`state` on one line."""
        state = self.state()
        detail = ""
        if state["state"] == "running":
            extras = [f"PID: {state['pid']}"]
            if state["ports"]:
                extras.append(f"port: {', '.join(str(p) for p in state['ports'])}")
            if state["ready"]:
                extras.append(f"ready: {state['ready']}")
            detail = f"({', '.join(extras)})"
        console.echo(f"  {self.name.capitalize():<15} : {styled_state(state['state'])} {detail}")

    def info(self) -> None:
        """Display detailed service information.

        Shows the common attributes every BaseService has: data directory,
        log file, and the live PID/port if running. Subclasses extend this to
        print service-specific details (URLs, credentials, config paths) via
        ``super().info()``.
        """
        console.echo(click.style(f"{self.name.capitalize()}:", bold=True))
        running = self.is_running()
        status_str = click.style("Running", fg="green") if running else click.style("Stopped", fg="red")
        console.echo(f"  Status        : {status_str}")
        if running:
            pid = self.get_pid()
            console.echo(f"  PID           : {pid}")
            ports = self.get_ports()
            if ports:
                console.echo(f"  Listening port: {', '.join(str(p) for p in ports)}")
        console.echo(f"  Data dir      : {self.service_data_dir}")
        console.echo(f"  Log file      : {self.log_file}")
        console.echo(f"  Supervisor    : process-compose ({supervisor.config_file()})")

    def restart(self, **kwargs: Any) -> None:  # Any: launch's options, each typed by the subclass
        """Restart the service."""
        console.echo(f"Restarting {self.name}...")
        self.stop()
        if self.is_running():  # the stop said why; a second copy would fight the first for its port
            return
        # We assume launch is the method to start with default/stored config
        # Individual services might need to override this if launch signatures differ significantly
        if hasattr(self, "launch"):
            getattr(self, "launch")(**kwargs)
        else:
            failures.report(f"Error: {self.name} service does not have a launch method.")

    def reset(self) -> None:
        """Stop the service and wipe its data directory, logs, and registration — never while it still runs: a
        stop that failed leaves everything as it was, since a running service writes into the folder being wiped."""
        if self.is_running():
            self.stop()
            if self.is_running():
                failures.report(
                    f"Error: {self.name.capitalize()} is still running, so its data was not wiped. "
                    f"Stop it (aisland {self.name} stop), then reset again."
                )
                return

        # Drop the supervisor registration too — a reset service should not
        # linger (disabled) in the desired-state document.
        if supervisor.remove_process(self.name) and supervisor.is_server_running():
            supervisor.apply()

        if self.service_data_dir.exists():
            console.echo(f"Wiping data for {self.name} at {self.service_data_dir}...")
            shutil.rmtree(self.service_data_dir)
            self.service_data_dir.mkdir(parents=True, exist_ok=True)

        if self.log_file.exists():
            self.log_file.unlink()

        console.echo(f"{self.name.capitalize()} data reset.")
