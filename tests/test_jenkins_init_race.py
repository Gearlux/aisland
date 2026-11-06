"""Regression tests for the `init_pipelines` write-config-while-running race.

Before the fix, `setup_global_properties` was called while Jenkins was still running, and Jenkins's in-memory
model silently overwrote the new `<globalNodeProperties>` block on its next save — leaving `env.WORKSPACE_ROOT`
unset and breaking every later build with `null/.venv/bin/python3: No such file or directory`. On a clean reset
the opposite failed: config.xml did not exist yet, so the properties were never written at all.

The fix warm-starts a Jenkins that is not running, waits for config.xml, stops Jenkins before any config.xml
change and cold-starts it after. These tests RUN `init_pipelines` with every step that needs a real Jenkins
replaced by a recorder, and read the order of what it did — each start and stop noting whether config.xml was
there at that moment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Tuple

import click
import pytest

from aisland.ci import jenkins_job_config as job_config
from aisland.services import jenkins_jobs, jenkins_settings
from aisland.services.jenkins import JenkinsService

#: ``run(running=..., local=..., config_appears=...)`` → (what init_pipelines did, in order; what it printed).
InitRun = Callable[..., Tuple[List[str], str]]


class _Clock:
    """The jenkins_jobs module's ``time``: ``sleep`` advances the clock and records a wait, and makes config.xml appear
    on the first one when told to — what a warm-starting Jenkins does a moment after its launch."""

    def __init__(self, config: Path, appears: bool, calls: List[str]) -> None:
        self.now, self.config, self.appears, self.calls = 0.0, config, appears, calls

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.appears and not self.config.exists():
            self.calls.append("waited, config.xml appeared")
            self.config.write_text("<hudson/>")


@pytest.fixture
def recorded_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> InitRun:
    """`init_pipelines` over a workspace of one project (`app`, with both Jenkinsfiles), Jenkins recorded."""
    workspace = tmp_path / "ws"
    (workspace / "app").mkdir(parents=True)
    (workspace / "app" / "Jenkinsfile").write_text("pipeline {}\n")
    (workspace / "app" / "Jenkinsfile.local").write_text("pipeline {}\n")
    monkeypatch.setattr(jenkins_jobs, "get_workspace_root", lambda: workspace)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)

    def run(*, running: bool, local: bool = True, config_appears: bool = True) -> Tuple[List[str], str]:
        svc = JenkinsService()
        svc.service_data_dir = tmp_path / "jenkins"
        svc.service_data_dir.mkdir(exist_ok=True)
        config = svc.service_data_dir / "config.xml"
        if running:
            config.write_text("<hudson/>")
        calls: List[str] = []

        def _state() -> str:
            return "config.xml present" if config.exists() else "config.xml absent"

        monkeypatch.setattr(svc, "setup", lambda: None)
        monkeypatch.setattr(svc, "setup_plugins", lambda: False)
        monkeypatch.setattr(svc, "is_running", lambda: running)
        monkeypatch.setattr(svc, "launch", lambda port=9080, background=False: calls.append(f"launch ({_state()})"))
        monkeypatch.setattr(svc, "stop", lambda: calls.append(f"stop ({_state()})"))
        jobs = jenkins_jobs
        monkeypatch.setattr(
            jenkins_settings, "setup_dashboard_view", lambda home, name=None: calls.append("setup_dashboard_view")
        )
        monkeypatch.setattr(
            jenkins_settings, "setup_global_properties", lambda home: calls.append("setup_global_properties")
        )
        monkeypatch.setattr(
            jobs, "_create_orchestrator_job", lambda home, local, names: calls.append("orchestrator job")
        )
        monkeypatch.setattr(jobs, "_create_nightly_check_job", lambda home: calls.append("nightly-check job"))
        monkeypatch.setattr(job_config, "local_job_config", lambda *a: "<flow-definition/>")
        monkeypatch.setattr(jobs, "_get_git_remote", lambda path: "git@github.com:Gearlux/app.git")
        monkeypatch.setattr(jobs, "_find_credentials", lambda home, url: "github")
        monkeypatch.setattr(jobs, "time", _Clock(config, config_appears, calls))
        capsys.readouterr()
        jenkins_jobs.init_pipelines(svc, local=local)
        captured = capsys.readouterr()
        return calls, captured.out + captured.err  # a warning is on stderr

    return run


def test_a_jenkins_not_running_is_warm_started_stopped_before_the_writes_and_cold_started_after(
    recorded_init: InitRun,
) -> None:
    calls, _ = recorded_init(running=False)
    assert calls == [
        "orchestrator job",
        "nightly-check job",
        "launch (config.xml absent)",
        "waited, config.xml appeared",
        "stop (config.xml present)",
        "setup_dashboard_view",
        "setup_global_properties",
        "launch (config.xml present)",
    ]


def test_a_running_jenkins_is_stopped_before_the_writes_and_not_warm_started(recorded_init: InitRun) -> None:
    calls, _ = recorded_init(running=True)
    assert calls == [
        "orchestrator job",
        "nightly-check job",
        "stop (config.xml present)",
        "setup_dashboard_view",
        "setup_global_properties",
        "launch (config.xml present)",
    ]


def test_a_config_xml_that_never_appears_is_waited_for_90_seconds_then_named(recorded_init: InitRun) -> None:
    calls, out = recorded_init(running=False, config_appears=False)
    assert "  - Warning: config.xml did not appear within 90s.\n" in out
    assert calls[calls.index("launch (config.xml absent)") + 1 :] == [
        "stop (config.xml absent)",
        "setup_dashboard_view",
        "setup_global_properties",
        "launch (config.xml absent)",
    ]
    assert jenkins_jobs.time.time() >= 90  # type: ignore[attr-defined]  # the fake clock: it waited the 90 s


def test_only_a_local_init_seeds_the_nightly_check_job(recorded_init: InitRun) -> None:
    local, _ = recorded_init(running=True, local=True)
    remote, _ = recorded_init(running=True, local=False)
    assert "nightly-check job" in local
    assert "nightly-check job" not in remote
    assert "orchestrator job" in remote
