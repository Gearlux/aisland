"""The workspace's MCP servers are DISCOVERED and registered with every MCP client we use.

Discovery is a `<pkg>.server:main` console script whose module imports the MCP SDK's server
package (`mcp.server`) — a class-name check (`FastMCP`) went stale with the SDK-2.x rename and
silently dropped vendorcli and never saw annotaide (measured 2026-09-05: a regeneration listed
navigaitor alone). One server map feeds two client files, both gitignored because their
commands are absolute venv paths: `.gemini/settings.json` (Gemini CLI) and `.mcp.json`
(Claude Code's project-scoped servers).
"""

import json
import re
from pathlib import Path

import pytest

from aisland import manager
from aisland.manager import _discover_mcp_servers, _write_gemini_settings, _write_mcp_json, get_workspace_root


def _project(root: Path, name: str, script: str, module_text: str) -> None:
    pkg = name.replace("-", "_")
    (root / name / pkg).mkdir(parents=True)
    (root / name / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n\n[project.scripts]\n{script} = "{pkg}.server:main"\n'
    )
    (root / name / pkg / "server.py").write_text(module_text)


@pytest.fixture()
def fake_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    _project(root, "modern", "modern-mcp", "from mcp.server.mcpserver import MCPServer\n\ndef main():\n    pass\n")
    _project(root, "legacy", "legacy", "from mcp.server.fastmcp import FastMCP\n\ndef main():\n    pass\n")
    _project(root, "webby", "webby-mcp", "from flask import Flask\n\ndef main():\n    Flask(__name__).run()\n")
    return root


class TestDiscovery:
    def test_a_server_is_a_script_whose_module_imports_the_sdks_server_package(self, fake_workspace: Path) -> None:
        venv_bin = fake_workspace / ".venv" / "bin"
        servers = _discover_mcp_servers(fake_workspace, venv_bin)
        assert set(servers) == {"modern", "legacy"}, "both SDK spellings are servers; the Flask module is not"
        assert servers["modern"] == {"command": str(venv_bin / "modern-mcp")}
        assert servers["legacy"] == {"command": str(venv_bin / "legacy")}

    def test_the_real_workspace_lists_every_mcp_server_it_ships(self) -> None:
        root = get_workspace_root()
        servers = _discover_mcp_servers(root, root / ".venv" / "bin")
        expected = {"annotaide", "navigaitor"} | (
            {"vendorcli"} if (root / "vendorcli" / "pyproject.toml").is_file() else set()
        )
        assert expected <= set(servers), sorted(servers)
        for name, entry in servers.items():
            assert Path(entry["command"]).is_absolute(), name


class TestTheTwoClientFiles:
    def test_both_files_carry_the_same_server_map(self, fake_workspace: Path) -> None:
        venv_bin = fake_workspace / ".venv" / "bin"
        _write_gemini_settings(fake_workspace, venv_bin)
        _write_mcp_json(fake_workspace, venv_bin)
        gemini = json.loads((fake_workspace / ".gemini" / "settings.json").read_text())
        claude = json.loads((fake_workspace / ".mcp.json").read_text())
        assert gemini == claude
        assert set(claude["mcpServers"]) == {"modern", "legacy"}
        assert claude["mcpServers"]["modern"]["command"] == str(venv_bin / "modern-mcp")

    def test_the_claude_file_is_the_project_scoped_shape(self, fake_workspace: Path) -> None:
        _write_mcp_json(fake_workspace, fake_workspace / ".venv" / "bin")
        text = (fake_workspace / ".mcp.json").read_text()
        assert text.endswith("\n") and list(json.loads(text)) == ["mcpServers"]

    def test_a_discovered_command_missing_from_the_venv_is_named(
        self, fake_workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An editable install that predates a project's `[project.scripts]` entry has no
        console script yet — the client would spawn a missing binary. The map is still written
        (the reinstall is the fix) and the warning names project, path and the command."""
        venv_bin = fake_workspace / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "modern-mcp").write_text("#!/bin/sh\n")
        _write_mcp_json(fake_workspace, venv_bin)
        out = capsys.readouterr().out
        assert "legacy's MCP command" in out and "uv pip install -e ./legacy" in out
        assert "modern's MCP command" not in out
        assert set(json.loads((fake_workspace / ".mcp.json").read_text())["mcpServers"]) == {"modern", "legacy"}

    def test_setup_writes_both_files(self) -> None:
        import inspect

        source = inspect.getsource(manager.setup_workspace)
        assert "_write_gemini_settings(workspace_root, venv_bin)" in source
        assert "_write_mcp_json(workspace_root, venv_bin)" in source


class TestTheWiring:
    def test_the_mcp_command_group_offers_both_clients(self) -> None:
        from aisland.cli import main

        mcp = main.commands["mcp"]
        assert set(mcp.commands) >= {"gemini", "claude"}  # type: ignore[attr-defined]

    def test_both_generated_files_are_gitignored_at_the_workspace_root(self) -> None:
        ignored = (get_workspace_root() / ".gitignore").read_text()
        assert re.search(r"^/\.gemini/settings\.json$", ignored, re.M)
        assert re.search(r"^/\.mcp\.json$", ignored, re.M)
