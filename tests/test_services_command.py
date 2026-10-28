"""`aisland services [start|stop|restart|status|info|reset] [NAME…]`: every service from one list.

The list is `aisland/services/registry.py`, in start order. With no name, `status` and `info` cover every service;
`start`, `stop`, `restart` and `reset` cover the services started by default (the container engine, Jenkins,
SwiftBar), stopping in the reverse order so the container engine goes last. A name no
service has is refused before anything happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest
from click.testing import CliRunner, Result

from aisland import cli
from aisland.services import registry


class _Fake:
    """A service that records what it was asked to do; ``launch`` takes ``background`` only if the entry says so."""

    def __init__(self, name: str, calls: List[Tuple[str, ...]], takes_background: bool) -> None:
        self.name = name
        self.calls = calls
        self.takes_background = takes_background

    def launch(self, **kwargs: bool) -> None:
        assert ("background" in kwargs) == self.takes_background
        self.calls.append(("start", self.name, *(["background"] if kwargs.get("background") else [])))

    def restart(self, **kwargs: bool) -> None:
        self.calls.append(("restart", self.name, *(["background"] if kwargs.get("background") else [])))

    def stop(self) -> None:
        self.calls.append(("stop", self.name))

    def status(self) -> None:
        self.calls.append(("status", self.name))

    def info(self) -> None:
        self.calls.append(("info", self.name))

    def reset(self) -> None:
        self.calls.append(("reset", self.name))


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, ...]]:
    """Three services: `engine` (default, no background), `server` (default), `extra` (only when named)."""
    recorded: List[Tuple[str, ...]] = []
    entries = (
        registry.Entry("engine", lambda: _Fake("engine", recorded, False), by_default=True, takes_background=False),
        registry.Entry("server", lambda: _Fake("server", recorded, True), by_default=True, takes_background=True),
        registry.Entry("extra", lambda: _Fake("extra", recorded, True), by_default=False, takes_background=True),
    )
    monkeypatch.setattr(registry, "ENTRIES", entries)
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [])  # no project declares one here
    monkeypatch.setattr("aisland.supervisor.print_status", lambda: recorded.append(("supervisor",)))
    return recorded


def _run(*args: str, input: str = "") -> Result:
    return CliRunner().invoke(cli.main, ["services", *args], input=input)


# --- with no name -------------------------------------------------------------------------------------------------


def test_services_alone_shows_the_supervisor_and_every_service(calls: List[Tuple[str, ...]]) -> None:
    assert _run().exit_code == 0
    assert calls == [("supervisor",), ("status", "engine"), ("status", "server"), ("status", "extra")]


def test_info_alone_covers_every_service(calls: List[Tuple[str, ...]]) -> None:
    assert _run("info").exit_code == 0
    assert calls == [("info", "engine"), ("info", "server"), ("info", "extra")]


def test_start_alone_starts_the_default_services_in_order(calls: List[Tuple[str, ...]]) -> None:
    assert _run("start", "--background").exit_code == 0
    assert calls == [("start", "engine"), ("start", "server", "background")]


def test_stop_alone_stops_the_default_services_last_started_first(calls: List[Tuple[str, ...]]) -> None:
    assert _run("stop").exit_code == 0
    assert calls == [("stop", "server"), ("stop", "engine")]


def test_restart_alone_restarts_the_default_services(calls: List[Tuple[str, ...]]) -> None:
    assert _run("restart", "-b").exit_code == 0
    assert calls == [("restart", "engine"), ("restart", "server", "background")]


def test_reset_asks_first_and_wipes_nothing_on_no(calls: List[Tuple[str, ...]]) -> None:
    assert _run("reset", input="n\n").exit_code == 1
    assert calls == []
    assert _run("reset", input="y\n").exit_code == 0
    assert calls == [("reset", "server"), ("reset", "engine")]


# --- with names ---------------------------------------------------------------------------------------------------


def test_named_services_are_the_ones_acted_on_in_the_order_given(calls: List[Tuple[str, ...]]) -> None:
    assert _run("start", "extra", "engine").exit_code == 0
    assert calls == [("start", "extra"), ("start", "engine")]


def test_a_service_not_started_by_default_starts_when_named(calls: List[Tuple[str, ...]]) -> None:
    assert _run("status", "extra").exit_code == 0
    assert calls == [("status", "extra")]


def test_an_unknown_name_is_refused_before_anything_happens(calls: List[Tuple[str, ...]]) -> None:
    result = _run("start", "server", "sever")
    assert result.exit_code == 1
    assert "Error: no service 'sever' (the services: engine, extra, server)" in result.output
    assert calls == []


# --- the real list ------------------------------------------------------------------------------------------------


def test_the_list_holds_every_service_aisland_ships_once() -> None:
    assert [entry.name for entry in registry.ENTRIES] == [
        "docker",
        "jenkins",
        "swiftbar",
    ]
    assert [entry.name for entry in registry.ENTRIES if entry.by_default] == [
        "docker",
        "jenkins",
        "swiftbar",
    ]


@pytest.mark.parametrize("name", ["labelstudio", "iqengine", "fiftyone", "mlflow", "aim", "clearml"])
def test_a_service_a_project_brings_is_not_aislands_own(name: str) -> None:
    """Label Studio is annotaide's, IQEngine the signal project's, FiftyOne curaitor's, MLflow, Aim and ClearML
    the trainer's: each is offered while its project is installed, and gone with it."""
    assert name not in {entry.name for entry in registry.ENTRIES}
    assert not (Path(registry.__file__).parent / f"{name}.py").exists()


def test_each_entry_builds_the_service_of_its_name() -> None:
    for entry in registry.ENTRIES:
        assert entry.make().name == entry.name


@pytest.mark.parametrize("command", ["start", "stop", "restart", "status", "info", "reset"])
def test_the_top_level_service_commands_are_gone(command: str) -> None:
    result = CliRunner().invoke(cli.main, [command])
    assert result.exit_code == 2
    assert f"No such command '{command}'" in result.output
