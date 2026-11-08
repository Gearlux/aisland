"""Everything that talks to Jenkins reaches it on the port it runs on.

Jenkins starts on 9080 unless `--port` says otherwise (`aisland jenkins start --port 9081`). The safe restart, the
Jenkinsfile validation of `aisland jenkins scaffold`, the nightly report and the SwiftBar menu's links all asked
`localhost:9080` whatever port Jenkins ran on. They now ask `JenkinsService.url()`: the running Jenkins' port, else
`JenkinsService.DEFAULT_PORT`, the one place the number is written.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any, List, Optional

import pytest

from aisland import paths
from aisland.services import jenkins, nightly
from aisland.services.jenkins import JenkinsService


@pytest.fixture
def on_9081(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(JenkinsService, "get_ports", lambda self: [9081])


@pytest.fixture
def requested(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Every URL opened, refused as if nothing answered."""
    urls: List[str] = []

    def urlopen(request: Any, timeout: float = 0) -> Any:
        urls.append(request.full_url if isinstance(request, urllib.request.Request) else str(request))
        raise OSError("nothing answers")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return urls


def test_the_url_is_the_running_port_else_the_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(JenkinsService, "get_ports", lambda self: [])
    assert JenkinsService().url() == "http://localhost:9080"
    assert JenkinsService.DEFAULT_PORT == 9080
    monkeypatch.setattr(JenkinsService, "get_ports", lambda self: [9081])
    assert JenkinsService().url() == "http://localhost:9081"


def test_info_names_the_running_port(on_9081: None, capsys: pytest.CaptureFixture[str]) -> None:
    JenkinsService().info()
    assert "  URL           : http://localhost:9081" in capsys.readouterr().out


@pytest.mark.usefixtures("empty_workspace")
def test_the_safe_restart_asks_the_running_jenkins(
    on_9081: None, requested: List[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing answers, so the restart falls back to stopping and launching Jenkins: recorded, never run — a launch
    needs Jenkins' WAR, which a clone of aisland alone (GitHub CI's) has not downloaded, and this machine's Jenkins
    is no test's to restart."""
    monkeypatch.setattr(JenkinsService, "is_running", lambda self: True)
    fallback: List[str] = []
    monkeypatch.setattr(JenkinsService, "stop", lambda self: fallback.append("stop"))
    monkeypatch.setattr(
        JenkinsService, "launch", lambda self, background=False: fallback.append(f"launch {background}")
    )
    service = JenkinsService()
    (service.service_data_dir / "secrets").mkdir(parents=True)
    (service.service_data_dir / "secrets" / "initialAdminPassword").write_text("pw\n")
    service.safe_restart()
    local = [url for url in requested if "localhost" in url]
    assert local and all(url.startswith("http://localhost:9081/") for url in local)
    assert fallback == ["stop", "launch True"]


def test_the_nightly_report_links_the_running_jenkins(on_9081: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JENKINS_URL", raising=False)
    assert nightly.jenkins_url() == "http://localhost:9081/"


def test_the_swiftbar_menu_links_the_running_jenkins(on_9081: None) -> None:
    from aisland.services.swiftbar import SwiftBarService

    bar = SwiftBarService()
    bar._generate_jenkins_script(["models"])
    script = next(bar.plugins_dir.glob("*jenkins*")).read_text()
    assert "http://localhost:9081" in script and "9080" not in script


@pytest.mark.parametrize("registered, given, expected", [(None, None, 9080), (9081, None, 9081), (9081, 9082, 9082)])
def test_a_launch_with_no_port_takes_the_one_jenkins_last_ran_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    registered: Optional[int],
    given: Optional[int],
    expected: int,
) -> None:
    """`--port` once given stays: a restart, `jenkins init`'s relaunch and a plain `start` find it in the supervisor's
    registration (a reset drops it, and 9080 comes back)."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(JenkinsService, "get_ports", lambda self: [])
    monkeypatch.setattr(jenkins.supervisor, "get_registered_port", lambda name: registered)
    for name in ("setup", "check_credentials_and_print_instructions"):
        monkeypatch.setattr(JenkinsService, name, lambda self, *args: None)
    monkeypatch.setattr(JenkinsService, "java_asking", lambda self: Path("/usr/bin/java"))
    monkeypatch.setattr(jenkins, "get_workspace_root", lambda: tmp_path)
    started: List[Any] = []
    monkeypatch.setattr(JenkinsService, "start", lambda self, cmd, **options: started.append((cmd, options)))

    JenkinsService().launch(port=given)

    ((cmd, options),) = started
    assert f"--httpPort={expected}" in cmd and options["readiness"]["port"] == expected


def test_the_cli_passes_no_port_unless_one_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from aisland.cli import main

    asked: List[Any] = []
    monkeypatch.setattr(JenkinsService, "restart", lambda self, **options: asked.append(options))
    monkeypatch.setattr(JenkinsService, "launch", lambda self, **options: asked.append(options))
    for line in (["jenkins", "restart"], ["jenkins", "start", "--port", "9081"]):
        assert CliRunner().invoke(main, line).exit_code == 0
    assert asked == [{"port": None, "background": False}, {"port": 9081, "background": False}]
