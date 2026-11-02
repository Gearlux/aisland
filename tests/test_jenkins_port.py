"""Everything that talks to Jenkins reaches it on the port it runs on.

Jenkins starts on 9080 unless `--port` says otherwise (`aisland jenkins start --port 9081`). The safe restart, the
Jenkinsfile validation of `aisland jenkins scaffold`, the nightly report and the SwiftBar menu's links all asked
`localhost:9080` whatever port Jenkins ran on. They now ask `JenkinsService.url()`: the running Jenkins' port, else
`JenkinsService.DEFAULT_PORT`, the one place the number is written.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any, List

import pytest

from aisland import nightly
from aisland.services import base, jenkins
from aisland.services.jenkins import JenkinsService


@pytest.fixture
def on_9081(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
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
    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
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


def test_the_cli_starts_jenkins_on_the_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisland.cli.services import jenkins_start

    port = next(param for param in jenkins_start.params if param.name == "port")
    assert port.default == jenkins.JenkinsService.DEFAULT_PORT
