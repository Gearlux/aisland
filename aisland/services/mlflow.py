import sys

import click

from aisland.manager import BaseService


class MLflowService(BaseService):
    """MLflow Tracking Server."""

    def __init__(self) -> None:
        super().__init__("mlflow")
        self.db_path = self.service_data_dir / "mlflow.db"
        self.artifacts_path = self.service_data_dir / "artifacts"

    def setup(self) -> None:
        """Fail with the selection command when mlflow is absent — never install it here.

        Installing it ad hoc would contradict the very layer this service depends on: which
        tracking backends a machine has is decided by ``aisland tracker`` and recorded in
        ``trackers.toml`` / ``.git/config``, and a silent ``pip install`` behind the
        operator's back leaves the venv holding something no selection asked for (and gone
        again after the next ``aisland setup``). Point at the command that makes it stick.
        """
        import importlib.util

        if importlib.util.find_spec("mlflow") is None:
            raise click.ClickException(
                "MLflow is not installed in this environment.\n"
                "  Select it, so the choice is recorded and survives the next setup:\n"
                "      aisland tracker set mlflow\n"
                "  Or, for this machine only:\n"
                "      aisland tracker set mlflow --machine-only"
            )

    def info(self) -> None:
        """Show detailed MLflow configuration."""
        super().info()
        default_port = 5005
        ports = self.get_ports()
        url_port = ports[0] if ports else default_port
        click.echo(f"  URL           : http://localhost:{url_port}")
        click.echo(f"  Backend store : sqlite:///{self.db_path}")
        click.echo(f"  Artifacts dir : {self.artifacts_path}")

    def launch(self, port: int = 5005, background: bool = False) -> None:
        """Start the MLflow tracking server."""
        self.setup()
        self.artifacts_path.mkdir(exist_ok=True)
        click.echo(f"MLflow: http://localhost:{port}")

        cmd = [
            sys.executable,
            "-m",
            "mlflow",
            "server",
            "--host",
            "localhost",
            "--port",
            str(port),
            "--backend-store-uri",
            f"sqlite:///{self.db_path}",
            "--default-artifact-root",
            str(self.artifacts_path),
        ]

        # MLflow's tracking server exposes a dedicated /health endpoint.
        self.start(cmd, background=background, readiness={"port": port, "path": "/health"})

    def ui(self, db_path: str, port: int = 5000) -> None:
        """Run MLflow UI with a specific database file."""
        self.setup()

        # Ensure path is absolute for sqlite:///
        from pathlib import Path

        abs_db_path = Path(db_path).resolve()

        cmd = [
            sys.executable,
            "-m",
            "mlflow",
            "ui",
            "--port",
            str(port),
            "--backend-store-uri",
            f"sqlite:///{abs_db_path}",
        ]

        # We run this in the foreground as it's typically for quick local inspection
        click.echo(f"Starting MLflow UI for {abs_db_path} on port {port}...")
        try:
            import subprocess

            subprocess.check_call(cmd)
        except KeyboardInterrupt:
            click.echo("\nStopping MLflow UI...")
        except Exception as e:
            click.echo(f"Error starting MLflow UI: {e}")
