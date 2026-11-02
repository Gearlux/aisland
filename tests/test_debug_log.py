"""aisland's debug log: `aisland --debug` shows it on stderr, a plain run shows nothing of it.

A library module logs a diagnostic with ``logging.getLogger(__name__).debug(...)`` — aisland's own mechanics no user
acts on (a cached download reused, a digest that matched, a step with nothing to do, which file was read, which
command ran) — and keeps ``click.echo`` for the command's result, the progress a person watches and what they must act
on. ``--debug`` puts one handler on the ``aisland`` logger for the run: one ``aisland: <module>: <message>`` line a
record, on stderr, so stdout still holds the result alone.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, List

import click
import pytest
from click.testing import CliRunner

from aisland import cli, gitcmd, gitmodules, locations
from aisland import pyproject as pyproject_toml
from aisland.cli import _base as cli_base
from aisland.services import docker, jenkins_jobs
from aisland.services.jenkins import JenkinsService


def _with_command(monkeypatch: pytest.MonkeyPatch, name: str, body: Callable[[], None]) -> None:
    @click.command(name)
    def command() -> None:
        body()

    monkeypatch.setitem(cli.main.commands, name, command)


def _runs_git(repo: Path) -> Callable[[], None]:
    def body() -> None:
        gitcmd.git(repo, "--version")
        click.echo("the result")

    return body


# --- the switch ------------------------------------------------------------------------------------------------------


def test_debug_shows_a_debug_line_on_stderr_and_leaves_stdout_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _with_command(monkeypatch, "probe", _runs_git(tmp_path))
    debug = CliRunner().invoke(cli.main, ["--debug", "probe"])
    assert debug.exit_code == 0
    assert debug.stdout == "the result\n"
    assert f"aisland: gitcmd: git --version (in {tmp_path}): exit 0\n" in debug.stderr


def test_a_plain_run_shows_no_debug_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _with_command(monkeypatch, "probe", _runs_git(tmp_path))
    plain = CliRunner().invoke(cli.main, ["probe"])
    assert plain.exit_code == 0
    assert plain.stdout == "the result\n"
    assert plain.stderr == ""


def test_the_line_names_the_module_below_aisland(monkeypatch: pytest.MonkeyPatch) -> None:
    def body() -> None:
        logging.getLogger("aisland.services.jenkins").debug("%d plugin(s) to fetch", 3)

    _with_command(monkeypatch, "probe", body)
    assert (
        CliRunner().invoke(cli.main, ["--debug", "probe"]).stderr == "aisland: services.jenkins: 3 plugin(s) to fetch\n"
    )


def test_the_run_takes_its_handler_off_and_puts_the_level_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logger = logging.getLogger("aisland")
    monkeypatch.setattr(logger, "level", logging.INFO)
    _with_command(monkeypatch, "probe", _runs_git(tmp_path))
    for _ in range(2):  # a second run in one process adds no second handler
        result = CliRunner().invoke(cli.main, ["--debug", "probe"])
        assert result.stderr.count("aisland: gitcmd: ") == 1
    assert cli_base._debug_log not in logger.handlers
    assert logger.level == logging.INFO


def test_a_plain_run_leaves_a_level_set_from_outside_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A host that asked for aisland's debug records (here pytest's own capture) still gets them from a plain run."""
    _with_command(monkeypatch, "probe", _runs_git(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="aisland"):
        plain = CliRunner().invoke(cli.main, ["probe"])
    assert plain.stderr == ""
    assert f"git --version (in {tmp_path}): exit 0" in caplog.messages


def test_a_record_that_cannot_be_written_is_handed_to_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    handled: List[logging.LogRecord] = []
    monkeypatch.setattr(cli_base._debug_log, "handleError", handled.append)
    record = logging.LogRecord("aisland.x", logging.DEBUG, __file__, 1, "%d needs a number", ("text",), None)
    cli_base._debug_log.emit(record)
    assert handled == [record]


# --- the decisions it shows ------------------------------------------------------------------------------------------


@pytest.fixture
def debug_log(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> pytest.LogCaptureFixture:
    monkeypatch.setattr(locations, "_last_said", None)
    caplog.set_level(logging.DEBUG, logger="aisland")
    return caplog


def _package_file(root: Path) -> Path:
    module = root / "aisland" / "aisland" / "manager.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    return module


def test_the_checkout_root_is_logged_with_the_marker_that_found_it(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture
) -> None:
    (tmp_path / ".gitmodules").write_text("")
    (tmp_path / "TASKS.md").write_text("")
    locations.checkout_workspace_root(_package_file(tmp_path))
    locations.checkout_workspace_root(_package_file(tmp_path / "again"))
    assert debug_log.messages == [
        f"workspace root {tmp_path} (from the checkout: it holds .gitmodules and TASKS.md)",
    ]


@pytest.mark.parametrize(
    "marker, why",
    [
        ("TASKS.md", "from the checkout: no .gitmodules above it, the outermost TASKS.md"),
        (".git", "from the checkout: no .gitmodules or TASKS.md above it, the outermost .git"),
    ],
)
def test_each_fallback_of_the_checkout_rule_says_which_it_was(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture, marker: str, why: str
) -> None:
    (tmp_path / marker).mkdir()
    root = locations.checkout_workspace_root(_package_file(tmp_path))
    assert debug_log.messages == [f"workspace root {root} ({why})"]


def test_the_last_resort_of_the_checkout_rule_says_so(tmp_path: Path, debug_log: pytest.LogCaptureFixture) -> None:
    module = _package_file(tmp_path / "a" / "b" / "c" / "d")
    root = locations.checkout_workspace_root(module)
    if root != module.parents[2]:
        pytest.skip(f"a marker above {tmp_path} answers first")
    assert debug_log.messages == [
        f"workspace root {root} (from the checkout: no marker above it, two folders above the package)"
    ]


def test_from_a_wheel_the_variable_or_the_rule_that_found_the_root_is_logged(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture
) -> None:
    project = tmp_path / "app"
    project.mkdir()
    (project / "pyproject.toml").write_text("")
    assert locations.wheel_workspace_root(project, {}) == tmp_path
    assert locations.wheel_workspace_root(project, {locations.WORKSPACE_ENV: str(project)}) == project
    assert debug_log.messages == [
        f"workspace root {tmp_path} (from a wheel: {project} is a project, the workspace its parent)",
        f"workspace root {project} (from a wheel: {locations.WORKSPACE_ENV} names it)",
    ]


@pytest.mark.parametrize(
    "layout, why",
    [
        (".gitmodules", "{start} holds .gitmodules"),
        ("child/pyproject.toml", "{start}, the folder aisland runs in, holds projects"),
        (".git/", "{start} is a repository"),
    ],
)
def test_each_rule_of_the_wheel_search_says_which_it_was(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture, layout: str, why: str
) -> None:
    start = tmp_path / "ws"
    target = start / layout
    if layout.endswith("/"):
        target.mkdir(parents=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("")
    assert locations.wheel_workspace_root(start, {}) == start
    assert debug_log.messages == [f"workspace root {start} (from a wheel: {why.format(start=start)})"]


def test_each_git_command_is_logged_with_its_exit_code(tmp_path: Path, debug_log: pytest.LogCaptureFixture) -> None:
    gitcmd.git(tmp_path, "rev-parse", "--is-inside-work-tree")
    assert debug_log.messages == [f"git rev-parse --is-inside-work-tree (in {tmp_path}): exit 128"]


def test_reading_gitmodules_says_which_file_and_how_many(tmp_path: Path, debug_log: pytest.LogCaptureFixture) -> None:
    assert gitmodules.read(tmp_path) == []
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitmodules").write_text('[submodule "lib"]\n\tpath = lib\n\turl = https://example.invalid/lib\n')
    assert [found.name for found in gitmodules.read(tmp_path)] == ["lib"]
    assert f"no .gitmodules in {tmp_path}: no submodules" in debug_log.messages
    assert f"read {tmp_path / '.gitmodules'}: 1 submodule(s)" in debug_log.messages


def test_reading_a_pyproject_says_which_file_and_why_one_declares_nothing(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text("[project\n")
    assert pyproject_toml.read(path) == {}
    with pytest.raises(pyproject_toml.PyprojectError):
        pyproject_toml.parse(path.read_text(), path)
    assert debug_log.messages[0] == f"reading {path}"
    assert debug_log.messages[1].startswith(f"{path} declares nothing: ")
    assert debug_log.messages[2] == f"parsing {path}"


def test_the_template_set_chosen_and_its_variables_are_logged(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture
) -> None:
    from aisland import ci_templates

    chosen = ci_templates.choice(tmp_path, "app")
    assert [line for line in debug_log.messages if "template set" in line] == [
        f"app: template set 'aisland' ({ci_templates.BUILTIN_DIR}, aisland's, nobody names one), "
        f"variables {chosen.variables}"
    ]


@pytest.mark.parametrize(
    "text, project, chooser",
    [
        ('[ci]\ntemplates = "aisland"\n', "app", "named by [ci] templates"),
        (
            '[project."app"]\nurl = "https://example.invalid/app.git"\nci = { templates = "aisland" }\n',
            "app",
            "named by the project's ci",
        ),
    ],
)
def test_the_log_says_who_named_the_template_set(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture, text: str, project: str, chooser: str
) -> None:
    from aisland import ci_templates

    (tmp_path / "workspace.toml").write_text(text)
    ci_templates.choice(tmp_path, project)
    assert any(f"template set 'aisland' ({ci_templates.BUILTIN_DIR}, {chooser})" in line for line in debug_log.messages)


# --- the lines moved from the output to the debug log ----------------------------------------------------------------


def test_installed_jenkins_plugins_are_said_in_the_debug_log_only(
    tmp_path: Path, debug_log: pytest.LogCaptureFixture, capsys: Any
) -> None:
    service = JenkinsService()
    service.service_data_dir = tmp_path
    (tmp_path / "plugins").mkdir()
    for plugin in service.PLUGINS:
        (tmp_path / "plugins" / f"{plugin}.jpi").write_text("")
    assert service.setup_plugins() is False
    assert capsys.readouterr().out == ""
    assert f"every required Jenkins plugin is already in {tmp_path / 'plugins'}" in debug_log.messages


def test_the_env_keys_jenkins_reads_are_said_in_the_debug_log_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug_log: pytest.LogCaptureFixture, capsys: Any
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("DATA_ROOT=/tmp/data\nHF_HOME=/tmp/hf\n")
    monkeypatch.setattr(locations, "workspace_root", lambda: workspace)
    service = JenkinsService()
    service.service_data_dir = tmp_path / "jenkins"
    service.service_data_dir.mkdir()
    (service.service_data_dir / "config.xml").write_text("<?xml version='1.1' encoding='UTF-8'?>\n<hudson/>\n")
    jenkins_jobs.setup_global_properties(service.service_data_dir)
    assert "key(s) from" not in capsys.readouterr().out
    assert f"read 2 key(s) from {workspace / '.env'}" in debug_log.messages


def test_a_docker_socket_already_linked_is_said_in_the_debug_log_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, debug_log: pytest.LogCaptureFixture, capsys: Any
) -> None:
    colima_socket = tmp_path / docker.COLIMA_PROFILE_DIR / "docker.sock"
    colima_socket.parent.mkdir(parents=True)
    colima_socket.write_text("")
    standard_socket = tmp_path / "docker.sock"
    os.symlink(colima_socket, standard_socket)

    class _Paths:
        """`Path` as the module calls it: `/var/run/docker.sock` and the home folder both in the test's folder."""

        def __call__(self, path: str) -> Path:
            return standard_socket if path == "/var/run/docker.sock" else Path(path)

        def home(self) -> Path:
            return tmp_path

    def no_sudo(*args: object, **kwargs: object) -> None:
        raise AssertionError("a socket already linked runs no command")

    monkeypatch.setattr(docker, "Path", _Paths())
    monkeypatch.setattr(docker.subprocess, "run", no_sudo)
    docker.DockerService().configure_socket()
    assert capsys.readouterr().out == "Configuring Docker socket symlink...\n"
    assert f"{standard_socket} already links to {colima_socket}" in debug_log.messages
