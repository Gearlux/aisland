import shutil

"""The workspace's MCP servers are DISCOVERED and registered with every MCP client we use.

Discovery is a `<pkg>.server:main` console script whose module imports the MCP SDK's server
package (`mcp.server`) — a class-name check (`FastMCP`) went stale with the SDK-2.x rename and
silently dropped app and never saw annotaide (measured 2026-09-05: a regeneration listed
navigaitor alone). One server map feeds two client files, both gitignored because their
commands are absolute venv paths: `.gemini/settings.json` (Gemini CLI) and `.mcp.json`
(Claude Code's project-scoped servers).
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import List

import pytest

from aisland import failures
from aisland.setup import manager, mcp_config
from aisland.setup.mcp_config import discover_mcp_servers, write_gemini_settings, write_mcp_json
from aisland.workspace.projects import get_workspace_root
from tests.test_setup_installs_git_aliases import stubbed_setup
from tests.workspace_layout import needs_workspace

__all__ = ["stubbed_setup"]  # the fixture that runs `aisland setup` with its machine steps stubbed


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
        servers = discover_mcp_servers(fake_workspace, venv_bin)
        assert set(servers) == {"modern", "legacy"}, "both SDK spellings are servers; the Flask module is not"
        assert servers["modern"] == {"command": str(venv_bin / "modern-mcp")}
        assert servers["legacy"] == {"command": str(venv_bin / "legacy")}

    @needs_workspace
    def test_the_real_workspace_lists_every_mcp_server_it_ships(self) -> None:
        root = get_workspace_root()
        servers = discover_mcp_servers(root, root / ".venv" / "bin")
        expected = {"annotaide", "navigaitor"}
        assert expected <= set(servers), sorted(servers)
        for name, entry in servers.items():
            assert Path(entry["command"]).is_absolute(), name


class TestTheTwoClientFiles:
    def test_both_files_carry_the_same_server_map(self, fake_workspace: Path) -> None:
        venv_bin = fake_workspace / ".venv" / "bin"
        write_gemini_settings(fake_workspace, venv_bin)
        write_mcp_json(fake_workspace, venv_bin)
        gemini = json.loads((fake_workspace / ".gemini" / "settings.json").read_text())
        claude = json.loads((fake_workspace / ".mcp.json").read_text())
        assert gemini == claude
        assert set(claude["mcpServers"]) == {"modern", "legacy"}
        assert claude["mcpServers"]["modern"]["command"] == str(venv_bin / "modern-mcp")

    def test_the_claude_file_is_the_project_scoped_shape(self, fake_workspace: Path) -> None:
        write_mcp_json(fake_workspace, fake_workspace / ".venv" / "bin")
        text = (fake_workspace / ".mcp.json").read_text()
        assert text.endswith("\n") and list(json.loads(text)) == ["mcpServers"]

    def test_the_gemini_file_keeps_every_setting_of_its_own(self, fake_workspace: Path) -> None:
        """Gemini CLI keeps a person's whole settings in `.gemini/settings.json`; setup wrote the file over with the
        servers alone."""
        settings = fake_workspace / ".gemini" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"theme": "GitHub"}))
        write_gemini_settings(fake_workspace, fake_workspace / ".venv" / "bin")
        written = json.loads(settings.read_text())
        assert written["theme"] == "GitHub"
        assert set(written["mcpServers"]) == {"modern", "legacy"}

    def test_a_server_the_person_added_stays_and_one_a_project_dropped_goes(
        self, fake_workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Setup replaced the whole server list: a server added with `claude mcp add --scope project`, and the
        `aisland-mcp` entry docs/mcp.md has a person write, were gone after the next setup."""
        subprocess.run(["git", "init", "-q"], cwd=fake_workspace, check=True)
        venv_bin = fake_workspace / ".venv" / "bin"
        write_mcp_json(fake_workspace, venv_bin)  # aisland writes modern and legacy, and remembers it did
        theirs = {
            "tools": {"command": "npx", "args": ["-y", "@acme/tools-mcp"]},
            "aisland": {"command": str(venv_bin / "aisland-mcp"), "env": {"AISLAND_WORKSPACE": str(fake_workspace)}},
        }
        written = json.loads((fake_workspace / ".mcp.json").read_text())
        (fake_workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {**written["mcpServers"], **theirs}}))
        shutil.rmtree(fake_workspace / "legacy")  # a project that no longer ships a server

        write_mcp_json(fake_workspace, venv_bin)
        servers = json.loads((fake_workspace / ".mcp.json").read_text())["mcpServers"]
        assert set(servers) == {"modern", "tools", "aisland"}
        assert {name: servers[name] for name in theirs} == theirs
        assert "kept 2 of yours: aisland, tools" in capsys.readouterr().out

    def test_a_server_started_through_the_shell_init_is_aislands_even_unrecorded(self, fake_workspace: Path) -> None:
        """An entry an older aisland wrote, before it recorded the names: its program is aisland's alone."""
        (fake_workspace / "project.bashrc").write_text("")
        venv_bin = fake_workspace / ".venv" / "bin"
        gone = mcp_config._with_workspace_env(fake_workspace, {"gone": {"command": str(venv_bin / "gone-mcp")}})
        (fake_workspace / ".mcp.json").write_text(json.dumps({"mcpServers": gone}))
        write_mcp_json(fake_workspace, venv_bin)
        assert "gone" not in json.loads((fake_workspace / ".mcp.json").read_text())["mcpServers"] or not shutil.which(
            "bash"
        )

    def test_a_client_file_that_is_no_json_is_reported_and_left_as_it_is(
        self, fake_workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = fake_workspace / ".gemini" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"theme": "GitHub",\n')
        failures.reset()
        write_gemini_settings(fake_workspace, fake_workspace / ".venv" / "bin")
        assert settings.read_text() == '{"theme": "GitHub",\n'
        assert failures.reported() == 1
        assert f"Error: {settings} is no JSON aisland can read" in capsys.readouterr().err

    def test_a_discovered_command_missing_from_the_venv_is_named(
        self, fake_workspace: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An editable install that predates a project's `[project.scripts]` entry has no
        console script yet — the client would spawn a missing binary. The map is still written
        (the reinstall is the fix) and the warning names project, path and the command."""
        venv_bin = fake_workspace / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "modern-mcp").write_text("#!/bin/sh\n")
        write_mcp_json(fake_workspace, venv_bin)
        out = capsys.readouterr().err
        assert "legacy's MCP command" in out and "uv pip install -e ./legacy" in out
        assert "modern's MCP command" not in out
        assert set(json.loads((fake_workspace / ".mcp.json").read_text())["mcpServers"]) == {"modern", "legacy"}

    def test_setup_writes_both_files(
        self, stubbed_setup: List[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`aisland setup` itself, its machine steps stubbed (the fixture) but the two writers real, over a
        workspace of the servers above: both client files appear, carrying both servers."""
        monkeypatch.setattr(manager, "write_gemini_settings", write_gemini_settings)
        monkeypatch.setattr(manager, "write_mcp_json", write_mcp_json)
        _project(tmp_path, "modern", "modern-mcp", "from mcp.server.mcpserver import MCPServer\n")
        _project(tmp_path, "legacy", "legacy", "from mcp.server.fastmcp import FastMCP\n")

        manager.setup_workspace(non_interactive=True)

        venv_bin = tmp_path / ".venv" / "bin"
        for client_file in (tmp_path / ".gemini" / "settings.json", tmp_path / ".mcp.json"):
            servers = json.loads(client_file.read_text())["mcpServers"]
            assert set(servers) == {"modern", "legacy"}, client_file
            assert servers["modern"]["command"] == str(venv_bin / "modern-mcp"), client_file


class TestTheServersGetTheWorkspaceEnvironment:
    """A client starts a server with ITS OWN environment, never the workspace's ``.env``
    (measured 2026-10-21: the desktop app's app-mcp ran with ``DATA_ROOT`` unset), so with a
    ``project.bashrc`` at the root each server is started through bash, which sources it — output
    discarded, stdout being the server's JSON-RPC stream — and then ``exec``s the server."""

    @pytest.fixture()
    def with_bashrc(self, fake_workspace: Path) -> Path:
        (fake_workspace / "project.bashrc").write_text(
            'export DATA_ROOT="/data root"\necho "noise from the shell init"\n'
        )
        return fake_workspace

    def test_each_server_is_started_through_bash_sourcing_the_workspace_shell_init(self, with_bashrc: Path) -> None:
        venv_bin = with_bashrc / ".venv" / "bin"
        write_mcp_json(with_bashrc, venv_bin)
        entry = json.loads((with_bashrc / ".mcp.json").read_text())["mcpServers"]["modern"]
        assert Path(entry["command"]).is_absolute() and Path(entry["command"]).name == "bash"
        assert entry["args"][-2:] == [str(with_bashrc / "project.bashrc"), str(venv_bin / "modern-mcp")]

    def test_the_server_sees_the_workspace_environment_and_its_stdout_carries_nothing_else(
        self, with_bashrc: Path, tmp_path: Path
    ) -> None:
        venv_bin = with_bashrc / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        server = venv_bin / "modern-mcp"
        server.write_text('#!/bin/sh\necho "server: $DATA_ROOT: $*"\n')
        server.chmod(0o755)
        write_mcp_json(with_bashrc, venv_bin)
        entry = json.loads((with_bashrc / ".mcp.json").read_text())["mcpServers"]["modern"]
        env = {k: v for k, v in os.environ.items() if k != "DATA_ROOT"}
        result = subprocess.run(
            [entry["command"], *entry["args"]], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "server: /data root: \n"

    def test_both_files_carry_the_same_started_through_bash_map(self, with_bashrc: Path) -> None:
        venv_bin = with_bashrc / ".venv" / "bin"
        write_gemini_settings(with_bashrc, venv_bin)
        write_mcp_json(with_bashrc, venv_bin)
        gemini = json.loads((with_bashrc / ".gemini" / "settings.json").read_text())
        assert gemini == json.loads((with_bashrc / ".mcp.json").read_text())
        assert "args" in gemini["mcpServers"]["legacy"]

    def test_a_missing_console_script_is_still_named(
        self, with_bashrc: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_mcp_json(with_bashrc, with_bashrc / ".venv" / "bin")
        assert "modern's MCP command" in capsys.readouterr().err

    @needs_workspace
    def test_the_real_shell_init_prints_nothing_and_gives_the_server_the_workspace_root(self, tmp_path: Path) -> None:
        root = get_workspace_root()
        if not (root / "project.bashrc").exists():
            pytest.skip("this checkout has no project.bashrc yet (`aisland setup` links it)")
        servers = mcp_config._with_workspace_env(root, {"probe": {"command": "/usr/bin/printenv"}})
        entry = servers["probe"]
        result = subprocess.run(
            [entry["command"], *entry["args"], "PROJECT_ROOT"],
            cwd=tmp_path,
            env={k: v for k, v in os.environ.items() if k != "PROJECT_ROOT"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{root}\n", "the shell init's own output must never reach the server's stdout"

    def test_without_a_shell_init_the_server_is_started_directly(self, fake_workspace: Path) -> None:
        venv_bin = fake_workspace / ".venv" / "bin"
        write_mcp_json(fake_workspace, venv_bin)
        entry = json.loads((fake_workspace / ".mcp.json").read_text())["mcpServers"]["modern"]
        assert entry == {"command": str(venv_bin / "modern-mcp")}

    def test_without_bash_the_server_is_started_directly(
        self, with_bashrc: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        venv_bin = with_bashrc / ".venv" / "bin"
        write_mcp_json(with_bashrc, venv_bin)
        entry = json.loads((with_bashrc / ".mcp.json").read_text())["mcpServers"]["modern"]
        assert entry == {"command": str(venv_bin / "modern-mcp")}


class TestTheWiring:
    def test_the_mcp_command_group_offers_both_clients(self) -> None:
        from aisland.cli import main

        mcp = main.commands["mcp"]
        assert set(mcp.commands) >= {"gemini", "claude"}  # type: ignore[attr-defined]

    @needs_workspace
    def test_both_generated_files_are_gitignored_at_the_workspace_root(self) -> None:
        ignored = (get_workspace_root() / ".gitignore").read_text()
        assert re.search(r"^/\.gemini/settings\.json$", ignored, re.M)
        assert re.search(r"^/\.mcp\.json$", ignored, re.M)
