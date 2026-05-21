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
        """Ensure mlflow is installed in the current environment."""
        import importlib.util

        if importlib.util.find_spec("mlflow") is None:
            click.echo("MLflow is not installed. Installing via pip...")
            import subprocess

            subprocess.run(
                [sys.executable, "-m", "pip", "install", "mlflow"], check=True
            )

    def info(self) -> None:
        """Show detailed MLflow configuration."""
        super().info()
        default_port = 5005
        pid = self.get_pid()
        ports = self._get_listening_ports(pid) if pid is not None else []
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

        self.start(cmd, background=background)

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
