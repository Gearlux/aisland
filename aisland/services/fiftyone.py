import sys
from typing import Any, List

import click

from aisland.manager import BaseService


class FiftyOneService(BaseService):
    """Voxel51 FiftyOne App for visual dataset/annotation review."""

    def __init__(self) -> None:
        super().__init__("fiftyone")
        # FiftyOne uses its own MongoDB-backed database directory; we point it
        # at our service_data_dir so all dataset state stays inside aisland/data/.
        self.database_dir = self.service_data_dir

    def setup(self) -> None:
        """Ensure fiftyone is installed in the current environment."""
        import importlib.util

        if importlib.util.find_spec("fiftyone") is None:
            click.echo("FiftyOne is not installed. Installing via pip...")
            import subprocess

            subprocess.run([sys.executable, "-m", "pip", "install", "fiftyone"], check=True)

    def info(self) -> None:
        """Show detailed FiftyOne configuration."""
        super().info()
        default_port = 5151
        pid = self.get_pid()
        ports = self._get_listening_ports(pid) if pid is not None else []
        url_port = ports[0] if ports else default_port

        env_values = self._read_env_values()
        url = env_values.get("FIFTYONE_URL") or f"http://localhost:{url_port}"
        database_dir = env_values.get("FIFTYONE_DATABASE_DIR") or str(self.database_dir)

        from aisland.manager import get_workspace_root

        click.echo(f"  URL           : {url}")
        click.echo(f"  Database dir  : {database_dir}")
        click.echo(f"  .env file     : {get_workspace_root() / '.env'}")
        click.echo(f"  Datasets      : {', '.join(self._list_datasets()) or '(none)'}")

    def _list_datasets(self) -> List[str]:
        """Return registered FiftyOne dataset names, or an empty list if unavailable.

        Importing fiftyone is expensive and may fail if the install is incomplete;
        info() must keep working in that case.
        """
        try:
            import fiftyone as fo  # type: ignore[import-not-found]

            return list(fo.list_datasets())
        except Exception:
            return []

    def _read_env_values(self) -> dict:
        """Return ``FIFTYONE_*`` entries from the workspace .env file (best-effort)."""
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
            if key.startswith("FIFTYONE_"):
                out[key] = value
        return out

    def launch(self, port: int = 5151, background: bool = False) -> None:
        """Start the FiftyOne App in remote mode."""
        self.setup()

        # Make sure the database dir exists before FiftyOne opens it.
        self.database_dir.mkdir(parents=True, exist_ok=True)

        # Publish URL + database dir for downstream tools (the annotaide review
        # CLI reads FIFTYONE_URL to print a clickable link to the user).
        self._update_env_file(port)

        click.echo(f"FiftyOne App: http://localhost:{port}")
        click.echo(f"  Database dir: {self.database_dir}")

        env = {
            "FIFTYONE_DATABASE_DIR": str(self.database_dir),
            "FIFTYONE_DEFAULT_APP_PORT": str(port),
            "FIFTYONE_DEFAULT_APP_ADDRESS": "0.0.0.0",
        }

        # ``--remote`` keeps the app listening without trying to spawn a browser,
        # which matches the rest of the aisland services and works in background mode.
        cmd: List[str] = [
            sys.executable,
            "-m",
            "fiftyone",
            "app",
            "launch",
            "--port",
            str(port),
            "--remote",
        ]

        self.start(cmd, env=env, background=background)

    def _update_env_file(self, port: int = 5151) -> None:
        """Add/replace FIFTYONE_* entries in the root .env file.

        Writes:
          * ``FIFTYONE_URL`` — base URL the review CLI prints back to the user.
          * ``FIFTYONE_DATABASE_DIR`` — absolute path of the database directory
            FiftyOne should use (so datasets land inside aisland/data/fiftyone/
            instead of polluting the user's home).
        """
        from aisland.manager import get_workspace_root

        workspace_root = get_workspace_root()
        env_file = workspace_root / ".env"
        url = f"http://localhost:{port}"
        entries = {
            "FIFTYONE_URL": url,
            "FIFTYONE_DATABASE_DIR": str(self.database_dir),
        }

        if not env_file.exists():
            env_file.write_text("\n".join(f"{k}={v}" for k, v in entries.items()) + "\n")
            click.echo(f"Created .env with FIFTYONE_URL={url}, FIFTYONE_DATABASE_DIR={self.database_dir}.")
            return

        content = env_file.read_text().splitlines()
        new_content: List[str] = []
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
        click.echo(f"Updated .env with FIFTYONE_URL={url}, FIFTYONE_DATABASE_DIR={self.database_dir}.")

    def restart(self, **kwargs: Any) -> None:
        """Restart the service, forwarding launch kwargs."""
        super().restart(**kwargs)
