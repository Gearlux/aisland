"""Tests for :mod:`aisland.supervisor` — the process-compose backend.

The supervisor's contract has two halves:

1. The **desired-state document** (``data/process-compose.yaml``): starting a
   service registers/enables its entry, stopping disables it, resetting
   removes it. process-compose applies whatever the document says, so a wrong
   document silently starts/stops the wrong things.
2. The **client calls**: every operation is a subprocess against the
   process-compose binary; the argument shape (config path, socket flags,
   JSON output) is what these tests pin down. subprocess is mocked — the
   live binary is exercised by the end-to-end flow, not unit tests.
"""

from __future__ import annotations

import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import click
import pytest
import yaml

from aisland import supervisor


@pytest.fixture(autouse=True)
def _own_workspace(empty_workspace: Path) -> Path:
    """The commands of this file run in a workspace of the test's own, never the developer's."""
    return empty_workspace


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the supervisor's generated files at a throwaway directory."""
    config = tmp_path / "process-compose.yaml"
    monkeypatch.setattr(supervisor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "CONFIG_FILE", config)
    monkeypatch.setattr(supervisor, "SOCKET_PATH", tmp_path / "process-compose.sock")
    monkeypatch.setattr(supervisor, "SERVER_LOG", tmp_path / "process-compose.log")
    return config


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Desired-state document
# ---------------------------------------------------------------------------


def test_register_process_writes_full_entry(isolated_config: Path) -> None:
    supervisor.register_process(
        "mlflow",
        ["python", "-m", "mlflow", "server", "--port", "5005"],
        env={"B": "2", "A": "1"},
        log_file=Path("/logs/mlflow.log"),
        readiness={"port": 5005, "path": "/health"},
        enabled=True,
    )

    text = isolated_config.read_text()
    assert text.startswith("# ====")  # provenance header (generator mandate)
    entry = yaml.safe_load(text)["processes"]["mlflow"]
    assert entry["command"] == "python -m mlflow server --port 5005"
    assert entry["disabled"] is False
    assert entry["environment"] == ["A=1", "B=2"]
    assert entry["log_location"] == "/logs/mlflow.log"
    assert entry["readiness_probe"]["http_get"] == {"host": "127.0.0.1", "port": 5005, "path": "/health"}
    # Crash recovery on, but manual stops respected.
    assert entry["availability"]["restart"] == "on_failure"


def test_register_process_quotes_command_tokens(isolated_config: Path) -> None:
    """Tokens with spaces must survive the argv → shell-string conversion."""
    supervisor.register_process("svc", ["echo", "hello world"], enabled=False)
    entry = yaml.safe_load(isolated_config.read_text())["processes"]["svc"]
    assert entry["command"] == "echo 'hello world'"
    assert entry["disabled"] is True


def test_register_preserves_other_entries(isolated_config: Path) -> None:
    """Starting one service must not drop or change another's desired state."""
    supervisor.register_process("jenkins", ["java", "-jar", "jenkins.war"], enabled=True)
    supervisor.register_process("mlflow", ["mlflow", "server"], enabled=False)
    processes = yaml.safe_load(isolated_config.read_text())["processes"]
    assert processes["jenkins"]["disabled"] is False
    assert processes["mlflow"]["disabled"] is True


def test_set_enabled_flips_desired_state(isolated_config: Path) -> None:
    supervisor.register_process("svc", ["sleep", "60"], enabled=True)
    assert supervisor.set_enabled("svc", False) is True
    assert yaml.safe_load(isolated_config.read_text())["processes"]["svc"]["disabled"] is True
    assert supervisor.set_enabled("unknown", False) is False


def test_remove_process(isolated_config: Path) -> None:
    supervisor.register_process("svc", ["sleep", "60"])
    assert supervisor.remove_process("svc") is True
    assert yaml.safe_load(isolated_config.read_text())["processes"] == {}
    assert supervisor.remove_process("svc") is False


def test_load_config_recovers_from_corrupt_yaml(isolated_config: Path) -> None:
    isolated_config.write_text("{not: valid: yaml: [")
    config = supervisor.load_config()
    assert config["processes"] == {}


# ---------------------------------------------------------------------------
# Applying the document
# ---------------------------------------------------------------------------


def test_apply_updates_running_server(isolated_config: Path) -> None:
    supervisor.register_process("svc", ["sleep", "60"], enabled=True)
    with (
        patch("aisland.supervisor.is_server_running", return_value=True),
        patch("aisland.supervisor._run_client", return_value=_completed()) as run_mock,
        patch("aisland.supervisor.read_workspace_env", return_value={}),
    ):
        supervisor.apply()
    args, _kwargs = run_mock.call_args
    assert args[0] == ["project", "update", "-f", str(isolated_config)]


def test_apply_expands_refs_from_workspace_env(isolated_config: Path) -> None:
    """${VAR} refs in the document expand CLIENT-side at `project update`,
    so apply() must hand the client the workspace .env values — layered OVER
    the inherited environment (a stale shell export must not win over the
    freshly-written .env)."""
    import os

    supervisor.register_process("svc", ["sleep", "60"], enabled=True)
    with (
        patch("aisland.supervisor.is_server_running", return_value=True),
        patch("aisland.supervisor._run_client", return_value=_completed()) as run_mock,
        patch("aisland.supervisor.read_workspace_env", return_value={"LABELSTUDIO_TOKEN": "tok-from-dotenv"}),
        patch.dict(os.environ, {"LABELSTUDIO_TOKEN": "stale-shell-value"}),
    ):
        supervisor.apply()
    env = run_mock.call_args.kwargs["env"]
    assert env["LABELSTUDIO_TOKEN"] == "tok-from-dotenv"
    assert "PATH" in env  # os.environ still underneath


def test_read_workspace_env_parses_and_skips_noise(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "# comment\n"
        "\n"
        "LABELSTUDIO_TOKEN=abc123\n"
        "SPACED_KEY = spaced value\n"  # bash runs a command named SPACED_KEY: no assignment
        "malformed-line-without-equals\n"
        "DATA_ROOT=/data/gert\n"
    )
    with patch("aisland.supervisor._workspace_root", return_value=tmp_path):
        values = supervisor.read_workspace_env()
    assert values == {"LABELSTUDIO_TOKEN": "abc123", "DATA_ROOT": "/data/gert"}


def test_read_workspace_env_missing_file(tmp_path: Path) -> None:
    with patch("aisland.supervisor._workspace_root", return_value=tmp_path):
        assert supervisor.read_workspace_env() == {}


def test_apply_raises_on_failed_update(isolated_config: Path) -> None:
    supervisor.register_process("svc", ["sleep", "60"], enabled=True)
    with (
        patch("aisland.supervisor.is_server_running", return_value=True),
        patch("aisland.supervisor._run_client", return_value=_completed(returncode=1, stderr="boom")),
    ):
        with pytest.raises(click.ClickException, match="boom"):
            supervisor.apply()


def test_apply_starts_server_when_something_is_enabled(isolated_config: Path) -> None:
    supervisor.register_process("svc", ["sleep", "60"], enabled=True)
    with (
        patch("aisland.supervisor.is_server_running", return_value=False),
        patch("aisland.supervisor._start_server") as start_mock,
    ):
        supervisor.apply()
    start_mock.assert_called_once()


def test_apply_skips_server_start_when_all_disabled(isolated_config: Path) -> None:
    """An all-disabled document needs no supervisor process at all."""
    supervisor.register_process("svc", ["sleep", "60"], enabled=False)
    with (
        patch("aisland.supervisor.is_server_running", return_value=False),
        patch("aisland.supervisor._start_server") as start_mock,
    ):
        supervisor.apply()
    start_mock.assert_not_called()


def test_start_server_command_shape(isolated_config: Path) -> None:
    """The server must run detached, keep-project, against our config/socket."""
    with (
        patch("aisland.supervisor.ensure_binary", return_value="/usr/bin/process-compose"),
        patch("aisland.supervisor._preflight_full_disk_access") as preflight_mock,
        patch("aisland.supervisor.subprocess.run", return_value=_completed()) as run_mock,
    ):
        supervisor._start_server()
    preflight_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd[0] == "/usr/bin/process-compose"
    assert cmd[1] == "up"
    assert "--detached" in cmd
    assert "--keep-project" in cmd
    assert str(isolated_config) in cmd


# ---------------------------------------------------------------------------
# Querying the server
# ---------------------------------------------------------------------------


def test_list_processes_parses_json(isolated_config: Path) -> None:
    payload = '[{"name": "mlflow", "status": "Running", "is_running": true, "pid": 42}]'
    with (
        patch("aisland.supervisor._client_reachable", return_value=True),
        patch("aisland.supervisor._run_client", return_value=_completed(stdout=payload)),
    ):
        processes = supervisor.list_processes()
    assert processes["mlflow"]["pid"] == 42
    assert processes["mlflow"]["is_running"] is True


def test_get_process_returns_none_without_server(isolated_config: Path) -> None:
    with patch("aisland.supervisor._client_reachable", return_value=False):
        assert supervisor.get_process("mlflow") is None
        assert supervisor.list_processes() == {}
        assert supervisor.get_ports("mlflow") == []


def test_get_ports_parses_cli_output(isolated_config: Path) -> None:
    # Output shape empirically verified against process-compose v1.116.0.
    # Repeated entries (one per worker socket) must dedupe.
    out = "Process mlflow TCP ports: [5005 9080 5005]\n"
    with (
        patch("aisland.supervisor._client_reachable", return_value=True),
        patch("aisland.supervisor._run_client", return_value=_completed(stdout=out)),
    ):
        assert supervisor.get_ports("mlflow") == [5005, 9080]


def test_get_ports_handles_unexpected_output(isolated_config: Path) -> None:
    with (
        patch("aisland.supervisor._client_reachable", return_value=True),
        patch("aisland.supervisor._run_client", return_value=_completed(stdout="no ports here")),
    ):
        assert supervisor.get_ports("mlflow") == []


def test_wait_for_returns_when_state_matches(isolated_config: Path) -> None:
    states: Dict[str, Any] = {"name": "svc", "is_running": True}
    with patch("aisland.supervisor.get_process", return_value=states):
        result = supervisor.wait_for("svc", running=True, timeout=2)
    assert result is states


def test_wait_for_outcome_ready(isolated_config: Path) -> None:
    state = {"is_ready": "Ready", "is_running": True, "has_ready_probe": True}
    with patch("aisland.supervisor.get_process", return_value=state):
        outcome, result = supervisor.wait_for_outcome("svc", timeout=2)
    assert outcome == "ready"
    assert result is state


def test_wait_for_outcome_failed_when_supervisor_gave_up(isolated_config: Path) -> None:
    """A crash-looping process (Completed, exit != 0) must NOT read as started.

    Regression guard for the live Jenkins port-conflict incident: the process
    flips is_running on/off through its restart budget, so a bare
    is_running poll reported success while the service was dying.
    """
    state = {"is_ready": "-", "is_running": False, "status": "Completed", "exit_code": 1, "restarts": 3}
    with patch("aisland.supervisor.get_process", return_value=state):
        outcome, result = supervisor.wait_for_outcome("svc", timeout=2)
    assert outcome == "failed"
    assert result is state


def test_wait_for_outcome_running_without_probe(isolated_config: Path) -> None:
    state = {"is_ready": "-", "is_running": True, "has_ready_probe": False, "status": "Running"}
    with patch("aisland.supervisor.get_process", return_value=state):
        outcome, _result = supervisor.wait_for_outcome("svc", timeout=5)
    assert outcome == "running"


def test_wait_for_outcome_pending_on_slow_readiness(isolated_config: Path) -> None:
    state = {"is_ready": "Not Ready", "is_running": True, "has_ready_probe": True, "status": "Running"}
    with patch("aisland.supervisor.get_process", return_value=state):
        outcome, _result = supervisor.wait_for_outcome("svc", timeout=1)
    assert outcome == "pending"


def test_port_in_use_free_port(isolated_config: Path) -> None:
    import socket

    # Reserve a port, close it, and probe: a just-released port must be free.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert supervisor.port_in_use(port) is None


def test_port_in_use_detects_listener(isolated_config: Path) -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        owner = supervisor.port_in_use(port)
    # Own PID when lsof resolves it, -1 when the diagnostic lookup fails.
    import os

    assert owner in (os.getpid(), -1)


def test_get_registered_port(isolated_config: Path) -> None:
    supervisor.register_process("svc", ["sleep", "60"], readiness={"port": 5005, "path": "/health"})
    assert supervisor.get_registered_port("svc") == 5005
    supervisor.register_process("bare", ["sleep", "60"])
    assert supervisor.get_registered_port("bare") is None
    assert supervisor.get_registered_port("unknown") is None


def test_start_process_command_shape(isolated_config: Path) -> None:
    with patch("aisland.supervisor._run_client", return_value=_completed()) as run_mock:
        assert supervisor.start_process("svc") is True
    run_mock.assert_called_once_with(["process", "start", "svc"])


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


def test_find_binary_prefers_path_then_vendored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "BIN_DIR", tmp_path)
    with patch("aisland.supervisor.shutil.which", return_value="/on/path/process-compose"):
        assert supervisor.find_binary() == "/on/path/process-compose"
    with patch("aisland.supervisor.shutil.which", return_value=None):
        assert supervisor.find_binary() is None
        vendored = tmp_path / "process-compose"
        vendored.touch()
        assert supervisor.find_binary() == str(vendored)


def test_ensure_binary_raises_when_uninstallable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user agreed to the install, and process-compose is still not there afterwards."""
    from aisland import provision

    monkeypatch.setattr(provision, "agree", lambda question: True)
    with (
        patch("aisland.supervisor.find_binary", return_value=None),
        patch("aisland.installers.install_brew_packages"),
        patch("aisland.installers.install_userspace_tool"),
    ):
        with pytest.raises(click.ClickException, match="process-compose is still not found after installing it"):
            supervisor.ensure_binary()


def test_connection_args_use_unix_socket_on_posix(isolated_config: Path) -> None:
    if supervisor.os.name == "nt":
        pytest.skip("posix-only")
    args = supervisor._connection_args()
    assert args[0] == "--use-uds"
    assert args[2].endswith("process-compose.sock")


# ---------------------------------------------------------------------------
# macOS Full Disk Access pre-flight
# ---------------------------------------------------------------------------
#
# TCC attributes supervised services' file access to the process-compose
# binary (the daemon-tree root), so external-volume data requires an FDA
# grant on THAT binary. These tests pin the probe (fake TCC db) and the
# fire/skip decision tree; the message content is asserted loosely (the
# resolved path must appear — it is the actionable part).


def _fake_tcc_db(tmp_path: Path, rows: list) -> Path:
    """A minimal stand-in for the system TCC database (access table subset)."""
    db_path = tmp_path / "TCC.db"
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("CREATE TABLE access (service TEXT, client TEXT, auth_value INTEGER)")
        db.executemany("INSERT INTO access VALUES (?, ?, ?)", rows)
        db.commit()
    return db_path


def test_tcc_probe_grant_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "process-compose"
    binary.touch()
    db = _fake_tcc_db(tmp_path, [("kTCCServiceSystemPolicyAllFiles", str(binary), 2)])
    monkeypatch.setattr(supervisor, "_TCC_SYSTEM_DB", db)
    assert supervisor._tcc_full_disk_access_granted(binary) is True


def test_tcc_probe_grant_matches_resolved_symlink_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """System Settings records the resolved Cellar path; PATH holds a symlink."""
    real = tmp_path / "cellar" / "process-compose"
    real.parent.mkdir()
    real.touch()
    link = tmp_path / "process-compose"
    link.symlink_to(real)
    db = _fake_tcc_db(tmp_path, [("kTCCServiceSystemPolicyAllFiles", str(real), 2)])
    monkeypatch.setattr(supervisor, "_TCC_SYSTEM_DB", db)
    assert supervisor._tcc_full_disk_access_granted(link) is True


def test_tcc_probe_grant_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "process-compose"
    binary.touch()
    db = _fake_tcc_db(tmp_path, [("kTCCServiceSystemPolicyAllFiles", "/some/other/binary", 2)])
    monkeypatch.setattr(supervisor, "_TCC_SYSTEM_DB", db)
    assert supervisor._tcc_full_disk_access_granted(binary) is False


def test_tcc_probe_denied_grant_is_not_granted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "process-compose"
    binary.touch()
    db = _fake_tcc_db(tmp_path, [("kTCCServiceSystemPolicyAllFiles", str(binary), 0)])
    monkeypatch.setattr(supervisor, "_TCC_SYSTEM_DB", db)
    assert supervisor._tcc_full_disk_access_granted(binary) is False


def test_tcc_probe_unreadable_db_is_inconclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "_TCC_SYSTEM_DB", tmp_path / "missing" / "TCC.db")
    assert supervisor._tcc_full_disk_access_granted(tmp_path / "process-compose") is None


def _clear_protected_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in supervisor._TCC_PROTECTED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_preflight_skips_on_non_darwin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    with (
        patch("aisland.supervisor.platform.system", return_value="Linux"),
        patch("aisland.supervisor.ensure_binary") as binary_mock,
    ):
        supervisor._preflight_full_disk_access()
    binary_mock.assert_not_called()
    assert capsys.readouterr().err == ""


def test_preflight_skips_without_external_volume_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clear_protected_env(monkeypatch)
    monkeypatch.setenv("DATA_ROOT", "/data/local")  # not under /Volumes/
    with (
        patch("aisland.supervisor.platform.system", return_value="Darwin"),
        patch("aisland.supervisor.read_workspace_env", return_value={}),
        patch("aisland.supervisor.ensure_binary") as binary_mock,
    ):
        supervisor._preflight_full_disk_access()
    binary_mock.assert_not_called()
    assert capsys.readouterr().err == ""


def test_preflight_warns_with_grant_path_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clear_protected_env(monkeypatch)
    binary = tmp_path / "process-compose"
    binary.touch()
    with (
        patch("aisland.supervisor.platform.system", return_value="Darwin"),
        patch("aisland.supervisor.read_workspace_env", return_value={"DATA_ROOT": "/Volumes/Store"}),
        patch("aisland.supervisor.ensure_binary", return_value=str(binary)),
        patch("aisland.supervisor._tcc_full_disk_access_granted", return_value=False),
    ):
        supervisor._preflight_full_disk_access()
    err = capsys.readouterr().err
    assert "Full Disk Access" in err
    assert str(binary.resolve()) in err
    assert "DATA_ROOT=/Volumes/Store" in err
    assert "aisland supervisor down" in err


def test_preflight_silent_when_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clear_protected_env(monkeypatch)
    with (
        patch("aisland.supervisor.platform.system", return_value="Darwin"),
        patch("aisland.supervisor.read_workspace_env", return_value={"HF_HOME": "/Volumes/Store/huggingface"}),
        patch("aisland.supervisor.ensure_binary", return_value=str(tmp_path / "process-compose")),
        patch("aisland.supervisor._tcc_full_disk_access_granted", return_value=True),
    ):
        supervisor._preflight_full_disk_access()
    assert capsys.readouterr().err == ""


def test_preflight_silent_when_probe_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clear_protected_env(monkeypatch)
    with (
        patch("aisland.supervisor.platform.system", return_value="Darwin"),
        patch("aisland.supervisor.read_workspace_env", return_value={"DATA_ROOT": "/Volumes/Store"}),
        patch("aisland.supervisor.ensure_binary", return_value=str(tmp_path / "process-compose")),
        patch("aisland.supervisor._tcc_full_disk_access_granted", return_value=None),
    ):
        supervisor._preflight_full_disk_access()
    assert capsys.readouterr().err == ""
