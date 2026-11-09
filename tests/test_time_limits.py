"""A command aisland runs that can stop answering has a time limit, and running past it is said in words.

`colima start|stop|delete`, `brew search`, the Jenkins plugin manager and the calls to macOS about SwiftBar each get
one; past it the failure is reported and the command goes on (exit 1), never a hang. A process-compose client call
that gives no answer is an ``AislandError``, and an ``aisland`` command the MCP server runs that is still running at
its limit is a ``ToolRefusal`` — both reach an MCP client as words, never as "Error executing tool".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from aisland import failures
from aisland.errors import AislandError
from aisland.services import docker, jenkins, jenkins_java, swiftbar
from aisland.services.docker import DockerService
from aisland.services.jenkins import JenkinsService
from aisland.services.swiftbar import SwiftBarService

pytest.importorskip("mcp")
from aisland import server  # noqa: E402  (server needs the [mcp] extra)
from aisland.services import supervisor  # noqa: E402


def _times_out(calls: List[Dict[str, Any]]) -> Any:
    """A ``subprocess`` call that records its arguments, then runs past its limit."""

    def run(argv: List[str], **kwargs: Any) -> Any:
        calls.append({"argv": argv, **kwargs})
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    return run


@pytest.fixture(autouse=True)
def no_failures_left() -> Any:
    failures.reset()
    yield
    failures.reset()


@pytest.mark.parametrize(
    "action, argv, limit",
    [
        ("launch", ["colima", "start"], docker.COLIMA_START_SECONDS),
        ("stop", ["colima", "stop"], docker.COLIMA_STOP_SECONDS),
    ],
)
def test_a_colima_that_stops_answering_is_reported_not_waited_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str, argv: List[str], limit: int
) -> None:
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(docker.subprocess, "check_call", _times_out(calls))
    monkeypatch.setattr(DockerService, "is_running", lambda self: action == "stop")
    monkeypatch.setattr(DockerService, "_start_argv", lambda self, wanted: ["colima", "start"])
    monkeypatch.setattr(docker.shutil, "which", lambda tool: f"/opt/homebrew/bin/{tool}")
    getattr(DockerService(), action)()
    assert [(call["argv"], call["timeout"]) for call in calls] == [(argv, limit)]
    assert f"timed out after {limit} seconds" in capsys.readouterr().err
    assert failures.reported() == 1


def test_a_brew_search_that_stops_answering_reads_as_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(jenkins_java.shutil, "which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(jenkins_java.subprocess, "run", _times_out(calls))
    assert jenkins_java.newest_versioned_formula() is None
    assert calls[0]["timeout"] == jenkins_java.BREW_SEARCH_SECONDS


def test_a_plugin_manager_past_its_limit_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(JenkinsService, "service_data_dir", tmp_path, raising=False)
    monkeypatch.setattr(JenkinsService, "_are_plugins_installed", lambda self: False)
    monkeypatch.setattr(JenkinsService, "java_asking", lambda self: Path("/usr/bin/java"))
    monkeypatch.setattr(jenkins.subprocess, "run", _times_out(calls))
    assert JenkinsService().setup_plugins() is False
    assert calls[0]["timeout"] == jenkins.PLUGIN_INSTALL_SECONDS
    assert f"gave no result in {jenkins.PLUGIN_INSTALL_SECONDS} s" in capsys.readouterr().err
    assert failures.reported() == 1


def test_swiftbar_is_stopped_by_its_exact_name_within_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(swiftbar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(swiftbar.subprocess, "run", lambda argv, **kwargs: calls.append({"argv": argv, **kwargs}))
    SwiftBarService().stop()
    assert calls == [{"argv": ["pkill", "-x", "SwiftBar"], "check": False, "timeout": swiftbar.SWIFTBAR_SECONDS}]


def test_a_swiftbar_launch_past_its_limit_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(swiftbar.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(swiftbar, "swiftbar_installed", lambda: True)
    monkeypatch.setattr(swiftbar.subprocess, "run", _times_out(calls))
    SwiftBarService().launch()
    assert calls[0]["argv"] == ["open", "-a", "SwiftBar"] and calls[0]["timeout"] == swiftbar.SWIFTBAR_SECONDS
    assert "Error launching SwiftBar" in capsys.readouterr().err


def test_a_supervisor_that_gives_no_answer_is_refused_in_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "ensure_binary", lambda: "process-compose")
    monkeypatch.setattr(supervisor.subprocess, "run", _times_out([]))
    with pytest.raises(AislandError, match=r"process-compose gave no answer to `process list` within 30 s"):
        supervisor._run_client(["process", "list"])


def test_an_aisland_command_still_running_at_the_mcp_limit_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []

    def times_out(command: List[str], cwd: Path, timeout: float) -> Any:
        calls.append({"argv": command, "timeout": timeout})
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(server, "get_workspace_root", lambda: Path("/"))
    monkeypatch.setattr(server, "_run_as_a_group", times_out)
    with pytest.raises(
        server.ToolRefusal,
        match="aisland services start docker was still running after 300 s, so it was stopped, with every process",
    ):
        server._run_aisland(["services", "start", "docker"])
    assert calls[0]["timeout"] == server.SERVICE_TIMEOUT
