import sys
from typing import Optional

import click

from sandbox.manager import BaseService


class MLflowService(BaseService):
    """MLflow Tracking Server."""

    def __init__(self) -> None:
        super().__init__("mlflow")
        self.db_path = self.service_data_dir / "mlflow.db"
        self.artifacts_path = self.service_data_dir / "artifacts"

    def setup(self) -> None:
        """Ensure mlflow is installed in the current environment."""
        try:
            import mlflow
        except ImportError:
            click.echo("MLflow is not installed. Installing via pip...")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "mlflow"], check=True)

    def launch(self, port: int = 5000, background: bool = False) -> None:
        """Start the MLflow tracking server."""
        self.setup()
        self.artifacts_path.mkdir(exist_ok=True)
        
        cmd = [
            sys.executable, "-m", "mlflow", "server",
            "--host", "localhost",
            "--port", str(port),
            "--backend-store-uri", f"sqlite:///{self.db_path}",
            "--default-artifact-root", str(self.artifacts_path),
        ]
        
        self.start(cmd, background=background)
