"""The orchestrator pipeline must not occupy an executor.

It only triggers the project jobs (`build job:`), which needs no node. Declaring
`agent { node { label 'built-in' } }` made it hold one of the built-in node's two
executors for its whole run, so each "parallel" phase ran its projects one at a
time: on 2026-09-13 vendorcli waited 875 s for recordstream to finish, and annotaide
and purifai sat at "Waiting for next available executor" behind torpaido.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisland.services.jenkins import JenkinsService


def _orchestrator_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local: bool) -> str:
    from aisland import manager

    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "get_workspace_name", lambda: "ws")
    monkeypatch.setattr(
        manager,
        "get_install_order",
        lambda root: [
            {"name": "libcore", "dependencies": []},
            {"name": "app_a", "dependencies": ["libcore>=0.1.0"]},
            {"name": "app_b", "dependencies": ["libcore>=0.1.0"]},
        ],
    )
    svc = JenkinsService()
    svc.service_data_dir = tmp_path / "jenkins"
    svc._create_orchestrator_job(local=local, initialized_projects=["libcore", "app_a", "app_b"])
    job = "ws.local" if local else "ws"
    return (svc.service_data_dir / "jobs" / job / "config.xml").read_text(encoding="utf-8")


@pytest.mark.parametrize("local", [True, False])
def test_the_orchestrator_holds_no_executor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local: bool) -> None:
    config = _orchestrator_script(monkeypatch, tmp_path, local)
    assert "agent none" in config
    assert "node {" not in config and "label 'built-in'" not in config


def test_a_phase_still_triggers_its_projects_in_parallel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The CON pin: dropping the node must leave the phases and their parallel triggers intact."""
    config = _orchestrator_script(monkeypatch, tmp_path, local=True)
    phase_1 = config[config.index("stage('Phase 1')") :]
    assert "parallel {" in phase_1
    assert "build job: 'app_a.local'" in phase_1
    assert "build job: 'app_b.local'" in phase_1
