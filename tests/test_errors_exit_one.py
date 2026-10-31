"""A command that prints an `Error:` line exits 1.

Two kinds of failure. One that ends the command (no such project, no `.venv`, a bad argument) is raised: one `Error:`
line on stderr, exit 1. One the command goes on past — a project that fails to install while setup installs the
others, a service that fails to start beside the others — is reported (`aisland.failures.report`): its line goes to
stderr, the command finishes its work, and the run then exits 1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Iterator

import click
import pytest
from click.testing import CliRunner

import aisland
from aisland import failures, locations, manager, supervisor
from aisland.cli import main
from aisland.errors import AislandError
from aisland.projects import get_project_metadata
from aisland.services import base


@pytest.fixture(autouse=True)
def clean_slate() -> Iterator[None]:
    failures.reset()
    yield
    failures.reset()


def _invoke(*args: str) -> click.testing.Result:
    return CliRunner().invoke(main, list(args))


def _with_command(monkeypatch: pytest.MonkeyPatch, name: str, body: Callable[[], None]) -> None:
    @click.command(name)
    def command() -> None:
        body()

    monkeypatch.setitem(main.commands, name, command)


# --- the mechanism --------------------------------------------------------------------------------------------------


def test_a_reported_failure_lets_the_command_finish_and_then_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def body() -> None:
        failures.report("  - Error: one part failed")
        click.echo("the rest still ran")

    _with_command(monkeypatch, "partly", body)
    result = _invoke("partly")
    assert result.exit_code == 1
    assert "  - Error: one part failed" in result.stderr
    assert "the rest still ran" in result.stdout


def test_a_command_that_reports_and_then_exits_0_still_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def body() -> None:
        failures.report("Error: x")
        raise SystemExit(0)

    _with_command(monkeypatch, "zero", body)
    assert _invoke("zero").exit_code == 1


def test_a_command_exiting_with_its_own_failure_code_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    def body() -> None:
        failures.report("Error: x")
        raise SystemExit(3)

    _with_command(monkeypatch, "three", body)
    assert _invoke("three").exit_code == 3


def test_the_next_command_starts_with_no_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_command(monkeypatch, "fails", lambda: failures.report("Error: x"))
    _with_command(monkeypatch, "fine", lambda: click.echo("ok"))
    assert _invoke("fails").exit_code == 1
    assert _invoke("fine").exit_code == 0


# --- one root, and --debug -----------------------------------------------------------------------------------------


def test_every_exception_aisland_defines_is_an_aisland_error_and_the_builtin_it_stands_for() -> None:
    from aisland import discovery, editor, nightly, server, sources, wheels_prune, workspace_file

    assert issubclass(discovery.DeclarationError, (AislandError, ValueError))
    for error, builtin in (
        (discovery.DeclarationError, ValueError),
        (workspace_file.FileError, ValueError),
        (editor.EditorChoiceError, ValueError),
        (sources.ReleaseProbeError, ConnectionError),
        (nightly.JenkinsUnreachable, ConnectionError),
        (wheels_prune.PruneError, RuntimeError),
        (server.ToolRefusal, Exception),
    ):
        assert issubclass(error, AislandError) and issubclass(error, builtin)


def test_no_library_module_raises_clicks_own_exception_or_exits() -> None:
    """A refusal is an AislandError; only the CLI's group turns it into the Error line and the exit status."""
    package = Path(aisland.__file__).parent
    hits = [
        f"{path.relative_to(package)}:{number}"
        for path in package.rglob("*.py")
        # main() of the MCP server is its own entry point; harness/ holds scripts a workspace runs, not aisland's code
        if path.name != "server.py" and "harness" not in path.relative_to(package).parts
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if "raise click.ClickException(" in line or "raise SystemExit(" in line and path.name != "cli.py"
    ]
    assert hits == []


@pytest.mark.parametrize(
    "error, line",
    [
        (
            PermissionError(13, "Permission denied", "/ws/.git/config"),
            "Error: [Errno 13] Permission denied: '/ws/.git/config'",
        ),
        (
            subprocess.CalledProcessError(128, ["git", "fetch"]),
            "Error: Command '['git', 'fetch']' returned non-zero exit status 128.",
        ),
    ],
)
def test_what_the_machine_refuses_is_one_error_line_and_debug_shows_the_traceback(
    monkeypatch: pytest.MonkeyPatch, error: Exception, line: str
) -> None:
    def body() -> None:
        raise error

    _with_command(monkeypatch, "machine", body)
    result = _invoke("machine")
    assert result.exit_code == 1
    assert result.stderr == f"{line} (run `aisland --debug …` for the traceback)\n"
    debug = _invoke("--debug", "machine")
    assert debug.exception is error


def test_debug_shows_the_traceback_of_aislands_own_refusal_too(monkeypatch: pytest.MonkeyPatch) -> None:
    def body() -> None:
        raise AislandError("no such project")

    _with_command(monkeypatch, "refuse", body)
    plain = _invoke("refuse")
    assert plain.exit_code == 1 and plain.stderr == "Error: no such project\n"
    debug = _invoke("--debug", "refuse")
    assert debug.exit_code == 1
    assert "Traceback (most recent call last)" in debug.stderr and debug.stderr.endswith("Error: no such project\n")


def test_a_bug_in_aisland_keeps_its_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def body() -> None:
        raise KeyError("x")

    _with_command(monkeypatch, "bug", body)
    assert isinstance(_invoke("bug").exception, KeyError)


# --- a service that does not start or stop -------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> base.BaseService:
    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "get_process", lambda name: None)
    return base.BaseService("demo")


def test_a_taken_port_is_reported(service: base.BaseService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "port_in_use", lambda port: 4242)
    service.start(["demo"], readiness={"port": 9999, "path": "/"})
    assert failures.reported() == 1


@pytest.mark.usefixtures("empty_workspace")
def test_a_service_that_fails_to_start_is_reported(service: base.BaseService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "port_in_use", lambda port: None)
    for name in ("register_process", "apply", "start_process"):
        monkeypatch.setattr(supervisor, name, lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "wait_for_outcome", lambda name: ("failed", {"status": "Completed"}))
    service.start(["demo"], readiness={"port": 9999, "path": "/"})
    assert failures.reported() == 1


def test_a_service_that_does_not_stop_in_time_is_reported(
    service: base.BaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "get_process", lambda name: {"is_running": True, "pid": 7})
    for name in ("set_enabled", "apply"):
        monkeypatch.setattr(supervisor, name, lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "wait_for", lambda *a, **k: {"is_running": True})
    service.stop()
    assert failures.reported() == 1


def test_docker_that_fails_to_start_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aisland.services import docker

    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
    engine = docker.DockerService()
    monkeypatch.setattr(engine, "is_running", lambda: False)
    monkeypatch.setattr(docker.shutil, "which", lambda name: f"/usr/local/bin/{name}")  # colima and docker are there

    def _fails(argv: list[str]) -> int:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "check_call", _fails)
    engine.launch()
    assert failures.reported() == 1


def test_swiftbar_that_fails_to_launch_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aisland.services import swiftbar

    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
    bar = swiftbar.SwiftBarService()
    monkeypatch.setattr(bar, "_require_macos", lambda: True)
    monkeypatch.setattr(swiftbar, "swiftbar_installed", lambda: True)

    def _fails(argv: list[str], **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(subprocess, "run", _fails)
    bar.launch()
    assert failures.reported() == 1


# --- setup ----------------------------------------------------------------------------------------------------------


def _project(root: Path, name: str) -> Path:
    (root / name).mkdir(parents=True)
    (root / name / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    return root / name / "pyproject.toml"


def test_setup_without_a_venv_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    result = _invoke("setup", "-y")
    assert result.exit_code == 1
    assert "Error: Virtual environment not found" in result.stderr


def test_a_project_that_fails_to_install_is_reported_and_the_others_are_still_installed(tmp_path: Path) -> None:
    metadata = [get_project_metadata(_project(tmp_path, name)) for name in ("app", "lib")]
    failed, skipped = manager._run_installs(tmp_path, "false", metadata)
    assert sorted(failed) == ["app", "lib"]
    assert failures.reported() == 2


# --- check ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    _project(tmp_path, "app")
    return tmp_path


def test_check_without_a_venv_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    result = _invoke("check")
    assert result.exit_code == 1
    assert "Error: Virtual environment not found" in result.stderr


def test_check_with_a_word_that_is_no_project_or_tool_exits_1(workspace: Path) -> None:
    result = _invoke("check", "nosuch")
    assert result.exit_code == 1
    assert "Error: Argument 'nosuch' is neither a project nor a supported tool" in result.stderr


def test_check_of_a_tool_for_an_unknown_project_exits_1(workspace: Path) -> None:
    result = _invoke("check", "pytest", "nosuch")
    assert result.exit_code == 1
    assert "Error: Project 'nosuch' not found." in result.stderr


def test_check_with_a_tool_not_installed_fails_that_gate_without_a_traceback(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = _invoke("check", "isort", "app")
    assert result.exit_code == 1
    assert "[FAIL] isort" in result.output
    assert "isort is not installed" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in result.output


# --- jenkins --------------------------------------------------------------------------------------------------------


def test_scaffolding_a_project_that_does_not_exist_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    result = _invoke("jenkins", "scaffold", "nosuch")
    assert result.exit_code == 1
    assert "Error: Project directory 'nosuch' not found" in result.stderr


def test_scaffolding_a_folder_without_pyproject_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    (tmp_path / "notes").mkdir()
    result = _invoke("jenkins", "scaffold", "notes")
    assert result.exit_code == 1
    assert "Error: pyproject.toml not found in notes" in result.stderr


def test_a_jenkins_download_that_fails_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aisland.services import jenkins

    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
    service = jenkins.JenkinsService()
    service.war_file = tmp_path / "bin" / "jenkins.war"

    def _offline(url: str, path: object) -> None:
        raise OSError("offline")

    monkeypatch.setattr(jenkins, "download", _offline)
    assert service.ensure_war() is False
    assert failures.reported() == 1


def test_a_supervisor_that_does_not_stop_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "is_server_running", lambda: True)
    monkeypatch.setattr(supervisor, "_run_client", lambda args: subprocess.CompletedProcess(args, 1, "", "socket gone"))
    result = _invoke("supervisor", "down", "--yes")
    assert result.exit_code == 1
    assert "Error: stopping the supervisor failed: socket gone" in result.stderr
