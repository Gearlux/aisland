"""`aisland setup` installs the workspace git aliases however aisland is installed.

There is no separate command for them: from a wheel they name the scripts by their path in the venv, so the next
`aisland setup` points them at a recreated venv or a new aisland, and from a checkout it gives a newly activated
submodule its copy. The steps before them need a real machine (provisioning, the installs), so they are stubbed.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
from click.testing import CliRunner

from aisland import cli, locations, manager


@pytest.fixture
def stubbed_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[Path]:
    """`setup_workspace` over an empty workspace with every step but the aliases' stubbed; the aliases' roots."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    installed: List[Path] = []
    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr("aisland.config.migrate", lambda root: None)
    monkeypatch.setattr(manager, "_link_project_bashrc", lambda root: None)
    monkeypatch.setattr(manager, "install_harness", lambda root: [])
    monkeypatch.setattr(manager, "provision_system", lambda: None)
    monkeypatch.setattr(manager, "_resolve_submodule_activation", lambda *a: None)
    monkeypatch.setattr(manager, "reconcile_submodules", lambda root: None)
    monkeypatch.setattr("aisland.sources.apply_all", lambda *a, **k: None)
    monkeypatch.setattr("aisland.mirrors.apply_all", lambda *a, **k: None)
    monkeypatch.setattr(manager, "get_install_order", lambda root: [])
    monkeypatch.setattr(manager, "_write_pyrightconfig", lambda *a: None)
    monkeypatch.setattr(manager, "_sync_mypy_path", lambda *a: None)
    monkeypatch.setattr("aisland.selection.resolve_for_setup", lambda *a, **k: ([], False))
    monkeypatch.setattr("aisland.selection.require_defined_declarations", lambda root: None)
    monkeypatch.setattr(manager, "_write_root_uv_toml", lambda *a: None)
    monkeypatch.setattr(manager, "_run_installs", lambda *a: ([], []))
    monkeypatch.setattr(manager, "_write_gemini_settings", lambda *a: None)
    monkeypatch.setattr(manager, "_write_mcp_json", lambda *a: None)
    monkeypatch.setattr(manager, "write_code_workspace", lambda root: None)

    def install(root: Path) -> bool:
        installed.append(root)
        return True

    monkeypatch.setattr(manager, "install_git_aliases", install)
    return installed


@pytest.mark.parametrize("from_checkout", [True, False], ids=["from a checkout", "from a wheel"])
def test_setup_installs_the_git_aliases(
    stubbed_setup: List[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_checkout: bool
) -> None:
    monkeypatch.setattr(locations, "FROM_CHECKOUT", from_checkout)
    manager.setup_workspace(non_interactive=True)
    assert stubbed_setup == [tmp_path]


def test_there_is_no_separate_command_for_the_aliases() -> None:
    result = CliRunner().invoke(cli.main, ["git", "aliases"])
    assert result.exit_code == 2
    assert "No such command 'git'" in result.output
