"""How often `aisland services` asks process-compose.

Every question is a process-compose client process (about 0.2-0.4 s each on a loaded machine), and `aisland
services` asked 17 of them: `project state` before each `process list` and `process ports`, and one `process list`
per service. A `process list` or `process ports` that fails already says there is no server, so it is asked alone;
and while one command lists or describes the services, one `process list` answers for all of them
(`supervisor.snapshot`). What each command prints is unchanged.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, List

import pytest

from aisland.services import registry, supervisor

PROCESSES = [{"name": "jenkins", "status": "Running", "is_running": True, "pid": 7}, {"name": "mlflow"}]


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """A supervisor whose socket exists; every client call is recorded and answered."""
    socket = tmp_path / "process-compose.sock"
    socket.touch()
    monkeypatch.setattr(supervisor, "socket_path", lambda: socket)
    monkeypatch.setattr(supervisor, "find_binary", lambda: "process-compose")
    asked: List[List[str]] = []

    def client(args: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        asked.append(args)
        if args[:2] == ["process", "list"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(PROCESSES), "")
        if args[:2] == ["process", "ports"]:
            return subprocess.CompletedProcess(args, 0, f"Process {args[2]} TCP ports: [9080]", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(supervisor, "_run_client", client)
    return asked


def test_listing_the_processes_is_one_question(server: List[List[str]]) -> None:
    assert set(supervisor.list_processes()) == {"jenkins", "mlflow"}
    assert server == [["process", "list", "--output", "json"]]


def test_a_failed_list_is_no_server(server: List[List[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supervisor, "_run_client", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "connection refused")
    )
    assert supervisor.list_processes() == {}
    assert supervisor.get_ports("jenkins") == []


def test_without_a_socket_nothing_is_asked(server: List[List[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "socket_path", lambda: Path("/nonexistent/process-compose.sock"))
    assert supervisor.list_processes() == {} and supervisor.get_ports("jenkins") == []
    assert server == []


def test_a_ports_question_is_asked_alone(server: List[List[str]]) -> None:
    assert supervisor.get_ports("jenkins") == [9080]
    assert server == [["process", "ports", "jenkins"]]


def test_inside_a_snapshot_one_list_answers_every_process(server: List[List[str]]) -> None:
    with supervisor.snapshot():
        assert supervisor.get_process("jenkins") == PROCESSES[0]
        assert supervisor.get_process("mlflow") == PROCESSES[1]
        assert supervisor.get_process("aim") is None
    assert server.count(["process", "list", "--output", "json"]) == 1


def test_outside_a_snapshot_every_question_is_asked_again(server: List[List[str]]) -> None:
    """A wait for a process to start must see each new answer: nothing is kept after the snapshot."""
    with supervisor.snapshot():
        supervisor.list_processes()
    supervisor.list_processes()
    supervisor.list_processes()
    assert server.count(["process", "list", "--output", "json"]) == 3


def test_services_status_asks_for_the_process_list_once(server: List[List[str]]) -> None:
    calls: List[str] = []

    class Service:
        def __init__(self, name: str) -> None:
            self.name = name

        def status(self) -> None:
            calls.append(str(supervisor.get_process(self.name)))

        info = status

    def make(name: str) -> Callable[[], Any]:  # Any: a stand-in with only the methods the list calls here
        return lambda: Service(name)

    entries = [registry.Entry(n, make(n), by_default=True, takes_background=True) for n in ("a", "b", "c")]
    registry.status(entries, [])
    registry.info(entries, [])
    assert len(calls) == 6
    assert server.count(["process", "list", "--output", "json"]) == 2  # one for status, one for info


def test_one_threads_snapshot_never_answers_another_threads_question(server: List[List[str]]) -> None:
    """The MCP server serves tool calls side by side: a `services_status` call's snapshot must not hand its one answer
    to a wait for a process to start (`wait_for`) running in another thread."""
    opened, asked = threading.Event(), threading.Event()
    elsewhere: List[int] = []

    def inside() -> None:
        with supervisor.snapshot():
            supervisor.list_processes()
            opened.set()
            asked.wait(timeout=10)

    def outside() -> None:
        opened.wait(timeout=10)
        supervisor.list_processes()
        supervisor.list_processes()
        elsewhere.append(1)
        asked.set()

    threads = [threading.Thread(target=inside), threading.Thread(target=outside)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert elsewhere == [1]
    assert server.count(["process", "list", "--output", "json"]) == 3
