"""The local Jenkins is reachable from this machine only, and aisland never shows its secrets.

Jenkins listens on every network interface unless told otherwise, so `aisland jenkins start` passes
`--httpListenAddress=127.0.0.1` (pinned in `tests/test_jenkins_java.py::TestJenkinsStart`). Its initial admin password
is named by its FILE, never printed: a command's output reaches scrollback, CI logs and, through `aisland-mcp`, an AI
client. `aisland jenkins init --local` may allow anonymous read access; it never turns the login or CSRF protection off.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aisland import paths
from aisland.services import base, jenkins_jobs
from aisland.services.jenkins import JenkinsService

PASSWORD = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> JenkinsService:
    """A Jenkins whose initial admin password is written, running on 9080."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(JenkinsService, "get_ports", lambda self: [9080])
    monkeypatch.setattr(JenkinsService, "is_running", lambda self: True)
    monkeypatch.setattr(base.BaseService, "status", lambda self: None)
    jenkins = JenkinsService()
    jenkins.admin_password_file.parent.mkdir(parents=True)
    jenkins.admin_password_file.write_text(PASSWORD + "\n")
    return jenkins


def test_status_names_the_password_file_and_never_prints_the_password(
    service: JenkinsService, capsys: pytest.CaptureFixture[str]
) -> None:
    service.status()
    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert f"Initial admin password: in {service.admin_password_file}" in out


def test_info_names_the_password_file_and_never_prints_the_password(
    service: JenkinsService, capsys: pytest.CaptureFixture[str]
) -> None:
    service.info()
    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert f"Admin password: in {service.admin_password_file}" in out


def test_the_start_instructions_name_the_password_file(
    service: JenkinsService, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(JenkinsService, "_has_remote_pipelines", lambda self: True)
    service.check_credentials_and_print_instructions(9080)
    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert str(service.admin_password_file) in out


CONFIG = """<?xml version='1.1' encoding='UTF-8'?>
<hudson>
  <useSecurity>true</useSecurity>
  <authorizationStrategy class="hudson.security.FullControlOnceLoggedInAuthorizationStrategy">
    <denyAnonymousReadAccess>true</denyAnonymousReadAccess>
  </authorizationStrategy>
  <crumbIssuer class="hudson.security.csrf.DefaultCrumbIssuer">
    <excludeClientIPFromCrumb>false</excludeClientIPFromCrumb>
  </crumbIssuer>
</hudson>
"""


def test_anonymous_read_keeps_the_login_and_csrf_protection(tmp_path: Path) -> None:
    (tmp_path / "config.xml").write_text(CONFIG)
    jenkins_jobs.allow_anonymous_read(tmp_path)
    root = ET.parse(tmp_path / "config.xml").getroot()
    assert root.findtext("authorizationStrategy/denyAnonymousReadAccess") == "false"
    assert root.findtext("useSecurity") == "true"
    assert root.find("crumbIssuer") is not None


def test_a_config_without_the_setting_is_left_as_it_is(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = CONFIG.replace("<denyAnonymousReadAccess>true</denyAnonymousReadAccess>", "")
    (tmp_path / "config.xml").write_text(text)
    jenkins_jobs.allow_anonymous_read(tmp_path)
    assert (tmp_path / "config.xml").read_text() == text
    assert "left as it is" in capsys.readouterr().out
