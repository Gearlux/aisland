"""A service that does not stop in time is left as it is.

`stop` reports it (the command exits 1) and returns; before this, `reset` went on to wipe the data folder of the
service still writing into it, and `restart` launched a second copy beside the first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest

from aisland import failures, paths
from aisland.services import base, supervisor


class _Stuck(base.BaseService):
    """A service the supervisor reports running whatever is asked of it."""

    def __init__(self) -> None:
        super().__init__("stuck")
        self.launched: List[str] = []

    def is_running(self) -> bool:
        return True

    def launch(self) -> None:
        self.launched.append("launch")


@pytest.fixture
def stuck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Stuck]:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "get_process", lambda name: {"is_running": True, "pid": 41})
    monkeypatch.setattr(supervisor, "set_enabled", lambda name, enabled: True)
    monkeypatch.setattr(supervisor, "apply", lambda: None)
    monkeypatch.setattr(supervisor, "wait_for", lambda name, running, timeout: {"is_running": True})
    failures.reset()
    yield _Stuck()
    failures.reset()


def test_a_reset_wipes_nothing_while_the_service_still_runs(stuck: _Stuck, capsys: pytest.CaptureFixture[str]) -> None:
    (stuck.service_data_dir / "config.xml").write_text("<hudson/>")
    stuck.log_file.write_text("still logging\n")

    stuck.reset()

    assert (stuck.service_data_dir / "config.xml").read_text() == "<hudson/>"
    assert stuck.log_file.is_file()
    assert "Error: Stuck is still running, so its data was not wiped." in capsys.readouterr().err
    assert failures.reported() >= 1


def test_a_restart_starts_no_second_copy_beside_one_that_did_not_stop(stuck: _Stuck) -> None:
    stuck.restart()
    assert stuck.launched == []
    assert failures.reported() == 1
