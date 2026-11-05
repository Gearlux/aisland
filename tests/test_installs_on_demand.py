"""aisland installs one of its own tools only when a feature first needs it, and only after the user agrees.

`aisland setup` installs what the workspace's projects declare and nothing of aisland's own; `aisland workspace
provision install`, asked for by name, still installs everything. A feature that finds its tool missing asks — in a
terminal — and otherwise refuses with the command that installs it, installing nothing:

- a service command and process-compose,
- `aisland docker start` and colima and docker,
- `aisland swiftbar` and SwiftBar,
- `aisland jenkins` and the Jenkins WAR and the Java it runs on;

and `aisland check` without `act` fails that gate naming the command.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List, Optional

import click
import pytest
from click.testing import CliRunner

from aisland import cli, console, locations, paths, supervisor
from aisland.setup import installers, manager, provision


@pytest.fixture
def installs(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[object]]:
    """Every install recorded instead of run."""
    calls: Dict[str, List[object]] = {"brew": [], "userspace": [], "java": [], "download": []}
    monkeypatch.setattr(installers, "install_brew_packages", lambda packages: calls["brew"].append(sorted(packages)))
    monkeypatch.setattr(installers, "install_userspace_tool", lambda name: calls["userspace"].append(name))
    monkeypatch.setattr(installers, "offer_shell_switch", lambda packages: None)
    monkeypatch.setattr(
        "aisland.services.jenkins.JenkinsService.provision_java", lambda self: calls["java"].append("java")
    )
    return calls


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(provision, "_brew_installed", lambda: set())


def _terminal(monkeypatch: pytest.MonkeyPatch, answer: Optional[bool]) -> List[str]:
    """A terminal whose user answers `answer` (None: no terminal at all); the questions asked are returned."""
    asked: List[str] = []
    monkeypatch.setattr(provision, "_can_ask", lambda: answer is not None)

    def _confirm(question: str, default: bool = True) -> bool:
        asked.append(question)
        return bool(answer)

    monkeypatch.setattr(console, "confirm", _confirm)
    return asked


def _workspace(tmp_path: Path) -> Path:
    project = tmp_path / "app"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1.0"\n[tool.aisland.sys-dependencies]\nbrew = ["graphviz"]\n'
    )
    return tmp_path


# --- setup installs only what the projects declare -------------------------------------------------------------------


def test_setup_installs_the_projects_tools_and_nothing_of_aislands_own(
    tmp_path: Path, macos: None, installs: Dict[str, List[object]]
) -> None:
    provision.install(_workspace(tmp_path), aislands_own=False)
    assert installs["brew"] == [["graphviz"]]
    assert installs["java"] == []


def test_the_provision_command_still_installs_everything(
    tmp_path: Path, macos: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    result = CliRunner().invoke(cli.main, ["workspace", "provision", "install"])
    assert result.exit_code == 0, result.output
    assert installs["brew"] == [sorted({"graphviz", *provision.AISLAND_TOOLS["brew"]})]
    assert installs["java"] == ["java"]


def test_setup_calls_the_install_without_aislands_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stop(Exception):
        pass

    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    seen: List[bool] = []

    def stop(workspace: Path, aislands_own: bool = True) -> None:
        seen.append(aislands_own)
        raise _Stop()

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(manager, "link_project_bashrc", lambda workspace: None)
    monkeypatch.setattr(manager, "install_harness", lambda workspace: [])
    monkeypatch.setattr(provision, "install", stop)
    with pytest.raises(_Stop):
        manager.setup_workspace(non_interactive=True)
    assert seen == [False]


# --- a feature asks before it installs its tool ----------------------------------------------------------------------


@pytest.fixture
def no_process_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "find_binary", lambda: None)


def test_without_a_terminal_a_missing_supervisor_is_refused_with_its_command(
    macos: None, no_process_compose: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _terminal(monkeypatch, None)
    with pytest.raises(click.ClickException) as refused:
        supervisor.ensure_binary()
    assert refused.value.message == (
        "aisland's services need process-compose, which is not installed. "
        "Install it: brew install f1bonacc1/tap/process-compose"
    )
    assert installs["brew"] == []


def test_in_a_terminal_the_user_is_asked_and_yes_installs_it(
    macos: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    found = iter([None, "/opt/homebrew/bin/process-compose"])
    monkeypatch.setattr(supervisor, "find_binary", lambda: next(found))
    asked = _terminal(monkeypatch, True)
    assert supervisor.ensure_binary() == "/opt/homebrew/bin/process-compose"
    assert asked == [
        "aisland's services need process-compose, which is not installed. "
        "Install it (brew install f1bonacc1/tap/process-compose)?"
    ]
    assert installs["brew"] == [["f1bonacc1/tap/process-compose"]]


def test_no_installs_nothing_and_refuses(
    macos: None, no_process_compose: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _terminal(monkeypatch, False)
    with pytest.raises(click.ClickException, match="Install it: brew install f1bonacc1/tap/process-compose"):
        supervisor.ensure_binary()
    assert installs["brew"] == []


def test_on_linux_yes_downloads_it_into_aislands_folder(
    no_process_compose: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.platform, "system", lambda: "Linux")
    asked = _terminal(monkeypatch, True)
    with pytest.raises(click.ClickException):  # still missing after the (recorded) install
        supervisor.ensure_binary()
    assert asked == [
        "aisland's services need process-compose, which is not installed. "
        "Install it (aisland downloads it into its bin folder)?"
    ]
    assert installs["userspace"] == ["process-compose"]


def test_docker_without_colima_is_refused_and_installs_nothing(
    macos: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aisland.services import docker

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(docker.shutil, "which", lambda name: None)
    engine = docker.DockerService()
    monkeypatch.setattr(engine, "is_running", lambda: False)
    _terminal(monkeypatch, None)
    with pytest.raises(click.ClickException, match="aisland docker needs colima, which is not installed. "):
        engine.launch()
    assert installs["brew"] == []


def test_swiftbar_without_the_app_is_refused(
    macos: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aisland.services import swiftbar

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(swiftbar, "swiftbar_installed", lambda: False)
    _terminal(monkeypatch, None)
    with pytest.raises(click.ClickException, match="Install it: brew install swiftbar"):
        swiftbar.SwiftBarService().launch()


def test_jenkins_asks_before_it_downloads_its_war(
    installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aisland.services import jenkins

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    service = jenkins.JenkinsService()
    service.war_file = tmp_path / "bin" / "jenkins.war"

    def fetch(url: str, path: Path) -> None:
        installs["download"].append(url)
        path.write_bytes(b"a war")

    monkeypatch.setattr(jenkins, "download", fetch)
    monkeypatch.setattr(jenkins.JenkinsService, "WAR_SHA256", hashlib.sha256(b"a war").hexdigest())
    _terminal(monkeypatch, None)
    with pytest.raises(click.ClickException, match="Jenkins needs its WAR"):
        service.setup()
    assert installs["download"] == []
    asked = _terminal(monkeypatch, True)
    assert service.ensure_war_asking() is True
    assert asked and asked[0].startswith("Jenkins needs its WAR")
    assert installs["download"] == [jenkins.JenkinsService.URL]


def test_jenkins_asks_before_it_installs_java(
    macos: None, installs: Dict[str, List[object]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aisland.services import jenkins

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)

    class _NoJava:
        runtime = None

        def problem(self) -> str:
            return "Jenkins 2.555.2 runs on Java 21 or 25, and none is installed"

        def remedy(self) -> str:
            return "brew install openjdk@25"

        def refusal(self) -> str:
            return f"Error: {self.problem()}.\nFix: {self.remedy()}"

    service = jenkins.JenkinsService()
    monkeypatch.setattr(service, "resolve_java", lambda: _NoJava())
    _terminal(monkeypatch, None)
    with pytest.raises(click.ClickException) as refused:
        service.java_asking()
    assert refused.value.message == (
        "Jenkins 2.555.2 runs on Java 21 or 25, and none is installed.\nFix: brew install openjdk@25"
    )
    assert installs["java"] == []
    asked = _terminal(monkeypatch, True)
    with pytest.raises(click.ClickException):  # the recorded install installs nothing, so it is still refused
        service.java_asking()
    assert asked == [
        "Jenkins 2.555.2 runs on Java 21 or 25, and none is installed. Install one (brew install openjdk@25)?"
    ]
    assert installs["java"] == ["java"]


# --- check -----------------------------------------------------------------------------------------------------------


def test_check_without_act_fails_that_gate_naming_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos: None
) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    project = tmp_path / "app"
    (project / ".github" / "workflows").mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "app"\nversion = "0.1.0"\n')
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = CliRunner().invoke(cli.main, ["check", "act", "app"])
    assert result.exit_code == 1
    assert "act is not installed — brew install act" in result.output
