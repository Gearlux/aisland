"""Tests for Label Studio credential handling via the workspace ``.env``.

The workspace ``.env`` is the single source of truth: ``launch()`` resolves
credentials as *explicit argument → .env → derived default*, writes the
result back to ``.env``, and hands the server process ``${LABELSTUDIO_*}``
REFERENCES (expanded by ``supervisor.apply()``) — never literal secrets in
the argv (``ps``-visible) or in the generated ``process-compose.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from aisland.services.labelstudio import LabelStudioService


def _launch(
    tmp_path: Path,
    env_file: Optional[str] = None,
    **launch_kwargs: Any,
) -> Tuple[List[str], Dict[str, str], Path]:
    """Run ``launch()`` fully mocked; return (cmd, env, env_file_path)."""
    env_path = tmp_path / ".env"
    if env_file is not None:
        env_path.write_text(env_file)
    svc = LabelStudioService()
    with (
        patch.object(LabelStudioService, "setup"),
        patch.object(LabelStudioService, "start") as start_mock,
        patch("aisland.manager.get_workspace_root", return_value=tmp_path),
        patch("aisland.supervisor._workspace_root", return_value=tmp_path),
        patch("aisland.manager.get_workspace_name", return_value="acme"),
        patch("aisland.services.labelstudio.get_git_email", return_value="dev@example.com"),
    ):
        svc.launch(background=True, **launch_kwargs)
    args, kwargs = start_mock.call_args
    return args[0], kwargs["env"], env_path


def test_launch_argv_carries_no_credentials(tmp_path: Path) -> None:
    cmd, _env, _ = _launch(tmp_path)
    assert cmd[:2] == ["label-studio", "start"]
    for flag in ("--username", "--password", "--user-token", "--enable-legacy-api-token"):
        assert flag not in cmd
    joined = " ".join(cmd)
    assert "dev@example.com" not in joined and "acme" not in joined


def test_launch_env_uses_references_not_literals(tmp_path: Path) -> None:
    _cmd, env, _ = _launch(tmp_path)
    assert env["LABEL_STUDIO_USERNAME"] == "${LABELSTUDIO_USERNAME}"
    assert env["LABEL_STUDIO_PASSWORD"] == "${LABELSTUDIO_PASSWORD}"
    assert env["LABEL_STUDIO_USER_TOKEN"] == "${LABELSTUDIO_TOKEN}"
    assert env["LABEL_STUDIO_ENABLE_LEGACY_API_TOKEN"] == "true"


def test_launch_defaults_written_to_env_file(tmp_path: Path) -> None:
    _cmd, _env, env_path = _launch(tmp_path)
    content = env_path.read_text()
    assert "LABELSTUDIO_USERNAME=dev@example.com" in content
    assert "LABELSTUDIO_PASSWORD=acme" in content
    assert "LABELSTUDIO_TOKEN=acme_token_" in content
    assert "LABELSTUDIO_URL=http://localhost:9081" in content


def test_launch_respects_existing_env_values(tmp_path: Path) -> None:
    """A user-edited .env wins over the derived defaults and is preserved."""
    _cmd, _env, env_path = _launch(
        tmp_path,
        env_file=("LABELSTUDIO_USERNAME=me@corp.com\nLABELSTUDIO_PASSWORD=hunter2\nLABELSTUDIO_TOKEN=custom_tok\n"),
    )
    content = env_path.read_text()
    assert "LABELSTUDIO_USERNAME=me@corp.com" in content
    assert "LABELSTUDIO_PASSWORD=hunter2" in content
    assert "LABELSTUDIO_TOKEN=custom_tok" in content
    assert "dev@example.com" not in content


def test_launch_explicit_args_win_over_env_file(tmp_path: Path) -> None:
    _cmd, _env, env_path = _launch(
        tmp_path,
        env_file="LABELSTUDIO_USERNAME=me@corp.com\n",
        username="cli@override.com",
    )
    content = env_path.read_text()
    assert "LABELSTUDIO_USERNAME=cli@override.com" in content
    assert "me@corp.com" not in content


def test_update_env_file_preserves_unrelated_lines(tmp_path: Path) -> None:
    _cmd, _env, env_path = _launch(
        tmp_path,
        env_file="# workspace config\nDATA_ROOT=/data/gert\nLABELSTUDIO_TOKEN=old_tok\n",
    )
    content = env_path.read_text()
    assert "# workspace config" in content
    assert "DATA_ROOT=/data/gert" in content
    # Existing .env token was read back and re-written unchanged.
    assert content.count("LABELSTUDIO_TOKEN=") == 1
    assert "LABELSTUDIO_TOKEN=old_tok" in content


@pytest.mark.parametrize("key", ["LABELSTUDIO_USERNAME", "LABELSTUDIO_PASSWORD"])
def test_info_reads_credentials_from_env_file(tmp_path: Path, key: str, capsys: pytest.CaptureFixture) -> None:
    (tmp_path / ".env").write_text(f"{key}=from-env-file\n")
    svc = LabelStudioService()
    with (
        patch("aisland.manager.get_workspace_root", return_value=tmp_path),
        patch("aisland.supervisor._workspace_root", return_value=tmp_path),
        patch("aisland.manager.get_workspace_name", return_value="acme"),
        patch("aisland.manager.get_git_email", return_value="dev@example.com"),
        patch.object(LabelStudioService, "get_ports", return_value=[]),
        patch("aisland.manager.BaseService.info"),
    ):
        svc.info()
    assert "from-env-file" in capsys.readouterr().out
