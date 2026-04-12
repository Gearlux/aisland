import sys
import click
from aisland.manager import BaseService

class LabelStudioService(BaseService):
    \"\"\"Label Studio Annotation Server.\"\"\"

    def __init__(self) -> None:
        super().__init__(\"labelstudio\")
        # Label Studio uses a base directory for its data, including its own SQLite DB
        self.data_dir = self.service_data_dir

    def setup(self) -> None:
        \"\"\"Ensure label-studio is installed in the current environment.\"\"\"
        import importlib.util

        if importlib.util.find_spec(\"label_studio\") is None:
            click.echo(\"Label Studio is not installed. Installing via pip...\")
            import subprocess
            # Use the project-standard venv's python to install
            subprocess.run([sys.executable, \"-m\", \"pip\", \"install\", \"label-studio\"], check=True)

    def launch(self, port: int = 8080, background: bool = False) -> None:
        \"\"\"Start the Label Studio server.\"\"\"
        self.setup()

        # Label Studio environment variables for data persistence
        env = {
            \"LABEL_STUDIO_BASE_DATA_DIR\": str(self.data_dir),
            \"LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK\": \"true\", # Security best practice
        }

        cmd = [
            sys.executable,
            \"-m\",
            \"label_studio\",
            \"start\",
            \"--port\",
            str(port),
        ]

        self.start(cmd, env=env, background=background)
