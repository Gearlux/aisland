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
        # Where Label Studio serves user-supplied local files from. Pipelines
        # that produce PNGs (e.g. the signals segmentation visualize stage)
        # write into this directory so LS references them via
        # /data/local-files/?d=<rel-path> without a re-encode.
        self.local_files_root = self.data_dir / "local-files"

    def setup(self) -> None:
        """Ensure label-studio is installed in the current environment."""
        import importlib.util

        if importlib.util.find_spec("label_studio") is None:
            click.echo("Label Studio is not installed. Installing via pip...")
            import subprocess

            # Use the project-standard venv's python to install
            subprocess.run([sys.executable, "-m", "pip", "install", "label-studio"], check=True)

    def info(self) -> None:
        """Show detailed Label Studio configuration."""
        super().info()
        default_port = 9081
        ports = self.get_ports()
        url_port = ports[0] if ports else default_port

        env_values = self._read_env_values()
        url = env_values.get("LABELSTUDIO_URL") or f"http://localhost:{url_port}"
        token = env_values.get("LABELSTUDIO_TOKEN")
        local_files_root = env_values.get("LABELSTUDIO_LOCAL_FILES_ROOT") or str(self.local_files_root)

        from aisland.manager import get_git_email, get_workspace_name, get_workspace_root

        click.echo(f"  URL           : {url}")
        click.echo(f"  Username      : {get_git_email()} (default — git email)")
        click.echo(f"  Password      : {get_workspace_name()} (default — workspace name)")
        click.echo(f"  API token     : {token if token else '(not yet generated — start Label Studio to create it)'}")
        click.echo(f"  Data dir      : {self.data_dir}")
        click.echo(f"  Local files   : {local_files_root}")
        click.echo(f"  .env file     : {get_workspace_root() / '.env'}")

    def _read_env_values(self) -> dict:
        """Return ``LABELSTUDIO_*`` entries from the workspace .env file (best-effort)."""
        from aisland.manager import get_workspace_root

        env_file = get_workspace_root() / ".env"
        if not env_file.exists():
            return {}
        out: dict = {}
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.startswith("LABELSTUDIO_"):
                out[key] = value
        return out

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

        # Ensure the local-files document root exists before LS opens it.
        self.local_files_root.mkdir(parents=True, exist_ok=True)

        # Update .env file with the token, URL, and local-files root (so
        # downstream pipelines like signals segmentation can write their
        # PNGs inside it and annotaide can reference them via
        # /data/local-files/?d=<rel>).
        self._update_env_file(user_token, port)

        click.echo(f"Label Studio: http://localhost:{port}")
        click.echo(f"  Username: {username}")
        click.echo(f"  Password: {password}")
        click.echo(f"  Local files root: {self.local_files_root}")

        # Label Studio environment variables for data persistence + local-files serving
        env = {
            "LABEL_STUDIO_BASE_DATA_DIR": str(self.data_dir),
            "LABEL_STUDIO_DISABLE_SIGNUP_WITHOUT_LINK": "true",  # Security best practice
            "LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN": "true",  # Allow simple token auth
            "LABEL_STUDIO_USERNAME": username,
            "LABEL_STUDIO_PASSWORD": password,
            "LABEL_STUDIO_USER_TOKEN": user_token,
            # Enable reading PNGs / images from the local-files root so tasks
            # can use /data/local-files/?d=<rel-path> image URLs.
            "LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED": "true",
            "LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT": str(self.local_files_root),
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
            "--enable-legacy-api-token",
            "--no-browser",
        ]

        # Label Studio (Django) exposes /health for load-balancer checks.
        self.start(cmd, env=env, background=background, readiness={"port": port, "path": "/health"})

    def _update_env_file(self, token: str, port: int = 9081) -> None:
        """Add/replace LABELSTUDIO_* entries in the root .env file.

        Writes:
          * ``LABELSTUDIO_TOKEN`` — legacy API token for programmatic access.
          * ``LABELSTUDIO_URL`` — base URL the client uses.
          * ``LABELSTUDIO_LOCAL_FILES_ROOT`` — absolute path to the directory
            LS serves via ``/data/local-files/?d=<rel>``. Pipelines write
            PNGs under this path so LS can reference them without a copy.
        """
        from aisland.manager import get_workspace_root

        workspace_root = get_workspace_root()
        env_file = workspace_root / ".env"
        url = f"http://localhost:{port}"
        entries = {
            "LABELSTUDIO_TOKEN": token,
            "LABELSTUDIO_URL": url,
            "LABELSTUDIO_LOCAL_FILES_ROOT": str(self.local_files_root),
        }

        if not env_file.exists():
            env_file.write_text("\n".join(f"{k}={v}" for k, v in entries.items()) + "\n")
            click.echo(
                f"Created .env with LABELSTUDIO_TOKEN, LABELSTUDIO_URL={url}, "
                f"LABELSTUDIO_LOCAL_FILES_ROOT={self.local_files_root}."
            )
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
        click.echo(
            f"Updated .env with LABELSTUDIO_TOKEN, LABELSTUDIO_URL={url}, LABELSTUDIO_LOCAL_FILES_ROOT={self.local_files_root}."
        )
