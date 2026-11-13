"""Everything that talks to Jenkins reaches it on the port it runs on.

Jenkins starts on 9080 unless `--port` says otherwise (`aisland jenkins start --port 9081`). The
Jenkinsfile validation of `aisland jenkins scaffold`, the nightly report and the SwiftBar menu's links all asked
`localhost:9080` whatever port Jenkins ran on. They now ask `JenkinsService.url()`: the running Jenkins' port, else
`JenkinsService.DEFAULT_PORT`, the one place the number is written.
"""

from __future__ import annotations

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
    monkeypatch.setattr(JenkinsService, "setup", lambda self: True)
    monkeypatch.setattr(JenkinsService, "check_credentials_and_print_instructions", lambda self, *args: None)
    monkeypatch.setattr(JenkinsService, "java_asking", lambda self: Path("/usr/bin/java"))
    monkeypatch.setattr(jenkins, "get_workspace_root", lambda: tmp_path)
    started: List[Any] = []
    monkeypatch.setattr(JenkinsService, "start", lambda self, cmd, **options: started.append((cmd, options)))

    JenkinsService().launch(port=given)

    ((cmd, options),) = started
    assert f"--httpPort={expected}" in cmd and options["readiness"]["port"] == expected
    assert (tmp_path / "jenkins").stat().st_mode & 0o777 == 0o700, "the home of a config.xml holding .env's values"


def test_the_cli_passes_no_port_unless_one_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from aisland.cli import main

    asked: List[Any] = []
    monkeypatch.setattr(JenkinsService, "restart", lambda self, **options: asked.append(options))
    monkeypatch.setattr(JenkinsService, "launch", lambda self, **options: asked.append(options))
    for line in (["jenkins", "restart"], ["jenkins", "start", "--port", "9081"]):
        assert CliRunner().invoke(main, line).exit_code == 0
    assert asked == [{"port": None, "background": False}, {"port": 9081, "background": False}]
