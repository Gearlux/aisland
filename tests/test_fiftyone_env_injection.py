"""Tests for ``FiftyOneService._update_env_file``.

The fiftyone service publishes ``FIFTYONE_URL`` and ``FIFTYONE_DATABASE_DIR``
into the workspace ``.env`` so downstream tools (annotaide review CLI) can
discover the running app without hardcoded ports. These tests pin that the
upsert is non-destructive: it adds missing keys, replaces existing ones, and
leaves unrelated entries untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisland import manager
from aisland.services.fiftyone import FiftyOneService


def _make_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FiftyOneService, Path]:
    """Build a FiftyOneService whose service_data_dir + workspace root live under tmp_path."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # get_workspace_root walks up looking for TASKS.md; give it one to find.
    (workspace / "TASKS.md").write_text("# tasks\n", encoding="utf-8")
    monkeypatch.setattr(manager, "get_workspace_root", lambda: workspace)

    svc = FiftyOneService()
    data_dir = tmp_path / "fiftyone"
    data_dir.mkdir()
    svc.service_data_dir = data_dir
    svc.database_dir = data_dir
    return svc, workspace


def _read_env(workspace: Path) -> dict[str, str]:
    env_file = workspace / ".env"
    if not env_file.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key] = value
    return out


def test_update_env_creates_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workspace = _make_service(tmp_path, monkeypatch)
    svc._update_env_file(port=5151)

    env = _read_env(workspace)
    assert env["FIFTYONE_URL"] == "http://localhost:5151"
    assert env["FIFTYONE_DATABASE_DIR"] == str(svc.database_dir)


def test_update_env_replaces_existing_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workspace = _make_service(tmp_path, monkeypatch)
    (workspace / ".env").write_text(
        "FIFTYONE_URL=http://localhost:9999\nFIFTYONE_DATABASE_DIR=/old\n",
        encoding="utf-8",
    )

    svc._update_env_file(port=5151)

    env = _read_env(workspace)
    assert env["FIFTYONE_URL"] == "http://localhost:5151"
    assert env["FIFTYONE_DATABASE_DIR"] == str(svc.database_dir)


def test_update_env_preserves_unrelated_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workspace = _make_service(tmp_path, monkeypatch)
    (workspace / ".env").write_text(
        "# comment\nLABELSTUDIO_TOKEN=abc123\nDATA_ROOT=/tmp/data\n",
        encoding="utf-8",
    )

    svc._update_env_file(port=5151)

    env = _read_env(workspace)
    assert env["LABELSTUDIO_TOKEN"] == "abc123"
    assert env["DATA_ROOT"] == "/tmp/data"
    assert env["FIFTYONE_URL"] == "http://localhost:5151"


def test_update_env_custom_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workspace = _make_service(tmp_path, monkeypatch)
    svc._update_env_file(port=6000)

    env = _read_env(workspace)
    assert env["FIFTYONE_URL"] == "http://localhost:6000"


def test_read_env_values_returns_only_fiftyone_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workspace = _make_service(tmp_path, monkeypatch)
    (workspace / ".env").write_text(
        "FIFTYONE_URL=http://localhost:5151\nLABELSTUDIO_TOKEN=abc\nFIFTYONE_DATABASE_DIR=/tmp/db\n",
        encoding="utf-8",
    )

    values = svc._read_env_values()
    assert values == {
        "FIFTYONE_URL": "http://localhost:5151",
        "FIFTYONE_DATABASE_DIR": "/tmp/db",
    }


def test_read_env_values_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _workspace = _make_service(tmp_path, monkeypatch)
    assert svc._read_env_values() == {}
