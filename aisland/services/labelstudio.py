import sys
from typing import Optional

import click

from aisland.manager import BaseService, get_git_email


class LabelStudioService(BaseService):
    """Label Studio Annotation Server."""

    def __init__(self) -> None:
        super().__init__("labelstudio")
        # Label Studio uses a base directory for its data, including its own SQLite DB
        self.data_dir = self.service_data_dir

    def setup(self) -> None:
        """Ensure label-studio is installed in the current environment."""
        import importlib.util

        if importlib.util.find_spec("label_studio") is None:
            click.echo("Label Studio is not installed. Installing via pip...")
            import subprocess

            # Use the project-standard venv's python to install
            subprocess.run([sys.executable, "-m", "pip", "install", "label-studio"], check=True)

    def launch(
        self,
        port: int = 9081,
        background: bool = False,
        username: Optional[str] = None,
        password: Optional[str] = None,
        user_token: Optional[str] = None,
    ) -> None:
        """Start the Label Studio server."""
        self.setup()

        from aisland.manager import get_workspace_name

        ws_name = get_workspace_name()
        if password is None:
            password = ws_name
        if user_token is None:
            user_token = f"{ws_name}_token_1234567890abcdef12345678"

        if username is None:
            username = get_git_email()

        # Update .env file with the token and URL
        self._update_env_file(user_token, port)

        click.echo("Label Studio Login:")
        click.echo(f"  Username: {username}")
        click.echo(f"  Password: {password}")

        # Label Studio environment variables for data persistence
        env = {
            "LABEL_STUDIO_BASE_DATA_DIR": str(self.data_dir),
            "LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK": "true",  # Security best practice
            "LABEL_STUDIO_USERNAME": username,
            "LABEL_STUDIO_PASSWORD": password,
            "LABEL_STUDIO_USER_TOKEN": user_token,
        }

        cmd = [
            "label-studio",
            "start",
            "--port",
            str(port),
            "--username",
            username,
            "--password",
            password,
            "--user-token",
            user_token,
            "--no-browser",
        ]

        self.start(cmd, env=env, background=background)

    def _update_env_file(self, token: str, port: int = 9081) -> None:
        """Add or replace LABELSTUDIO_TOKEN and LABELSTUDIO_URL in the root .env file."""
        from aisland.manager import get_workspace_root

        workspace_root = get_workspace_root()
        env_file = workspace_root / ".env"
        url = f"http://localhost:{port}"
        entries = {
            "LABELSTUDIO_TOKEN": token,
            "LABELSTUDIO_URL": url,
        }

        if not env_file.exists():
            env_file.write_text("\n".join(f"{k}={v}" for k, v in entries.items()) + "\n")
            click.echo(f"Created .env with LABELSTUDIO_TOKEN and LABELSTUDIO_URL={url}.")
            return

        content = env_file.read_text().splitlines()
        new_content = []
        found_keys: set[str] = set()
        for line in content:
            replaced = False
            for key, value in entries.items():
                if line.startswith(f"{key}="):
                    new_content.append(f"{key}={value}")
                    found_keys.add(key)
                    replaced = True
                    break
            if not replaced:
                new_content.append(line)

        for key, value in entries.items():
            if key not in found_keys:
                new_content.append(f"{key}={value}")

        env_file.write_text("\n".join(new_content) + "\n")
        click.echo(f"Updated .env with LABELSTUDIO_TOKEN and LABELSTUDIO_URL={url}.")
