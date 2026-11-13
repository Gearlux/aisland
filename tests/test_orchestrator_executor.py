"""The orchestrator pipeline must not occupy an executor.

It only triggers the project jobs (`build job:`), which needs no node. Declaring
`agent { node { label 'built-in' } }` made it hold one of the built-in node's two
executors for its whole run, so each "parallel" phase ran its projects one at a
time: on 2026-09-13 app waited 875 s for recordstream to finish, and annotaide
and purifai sat at "Waiting for next available executor" behind torpaido.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from aisland import locations
from aisland.ci import jenkins_job_config as job_config
from aisland.ci.jenkins_job_config import NIGHTLY_CRON
from aisland.services import jenkins_jobs
from aisland.workspace.model import Workspace
from tests.records import project_metadata


def _orchestrator_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local: bool, slow: Sequence[str] = ()) -> str:
    """The orchestrator's config.xml for three projects; those named in `slow` register the `slow` marker."""
    for name in slow:
        (tmp_path / name).mkdir()
        (tmp_path / name / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\n\n[tool.pytest.ini_options]\nmarkers = ["slow: the slow run"]\n'
        )

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(jenkins_jobs, "get_workspace_name", lambda: "ws")
    monkeypatch.setattr(
        Workspace,
        "install_order",
        [
            project_metadata("libcore", tmp_path / "libcore"),
            project_metadata("app_a", tmp_path / "app_a", "libcore>=0.1.0"),
            project_metadata("app_b", tmp_path / "app_b", "libcore>=0.1.0"),
        ],
    )
    home = tmp_path / "jenkins"
    jenkins_jobs._create_orchestrator_job(home, local=local, initialized_projects=["libcore", "app_a", "app_b"])
    job = "ws.local" if local else "ws"
    return (home / "jobs" / job / "config.xml").read_text(encoding="utf-8")


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


# --- the nightly run (user ruling 2026-10-06) ----------------------------------------------------
# The LOCAL orchestrator runs every night at 01:00, so every project's slow run happens and the test-run statistics
# fill (`aisland tests report`). Its timer sits where Jenkins reads one at each moment: the job's properties arm it
# when the job is seeded, before any run; the pipeline's `triggers` directive re-applies it at every run (a pipeline
# without one would clear the job's timer on its first run).


def test_the_local_orchestrator_runs_every_night_at_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _orchestrator_script(monkeypatch, tmp_path, local=True)
    assert NIGHTLY_CRON == "0 1 * * *"
    assert f"triggers {{\n        cron('{NIGHTLY_CRON}')\n    }}" in config
    assert "<org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>" in config
    timer = config[config.index("<hudson.triggers.TimerTrigger>") : config.index("</hudson.triggers.TimerTrigger>")]
    assert f"<spec>{NIGHTLY_CRON}</spec>" in timer


def test_the_ci_orchestrator_keeps_no_timer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The CON pin: the GitHub-backed orchestrator builds what is pushed; it gains no schedule."""
    config = _orchestrator_script(monkeypatch, tmp_path, local=False)
    assert "cron(" not in config
    assert "TimerTrigger" not in config


# --- a manual run is the fast one, the nightly run the slow one too (user ruling 2026-10-06) ---------------
# A manual Build Now runs every project's fast tests; the 01:00 timer (or a tick on the orchestrator's SLOW_TESTS) passes
# SLOW_TESTS=true to each project that has slow tests, whose local pipeline runs its Slow Tests stage only then.

_SLOW_TESTS_DECIDED = (
    "def slowTests() {\n"
    "    return params.SLOW_TESTS || !currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause')"
    ".isEmpty()\n}"
)


def test_the_nightly_run_asks_for_the_slow_tests_and_a_manual_one_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _orchestrator_script(monkeypatch, tmp_path, local=True, slow=("app_a",))
    assert _SLOW_TESTS_DECIDED in config
    declared = config[config.index("name: 'SLOW_TESTS'") :]
    assert declared[: declared.index(")")].count("defaultValue: false") == 1
    assert (
        "build job: 'app_a.local', wait: true, propagate: true, "
        "parameters: [booleanParam(name: 'SLOW_TESTS', value: slowTests())]"
    ) in config
    # Defined at seeding too, so Build with Parameters offers the tick before the job's first run.
    defined = config[config.index("<hudson.model.ParametersDefinitionProperty>") :]
    assert "<name>SLOW_TESTS</name>" in defined and "<defaultValue>false</defaultValue>" in defined


def test_a_project_without_slow_tests_is_built_as_before(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The CON pin: a project with no `slow` marker gets no switch it would not read."""
    config = _orchestrator_script(monkeypatch, tmp_path, local=True, slow=("app_a",))
    assert "build job: 'app_b.local', wait: true, propagate: true\n" in config
    assert "build job: 'libcore.local', wait: true, propagate: true\n" in config


def test_the_ci_orchestrator_passes_no_slow_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _orchestrator_script(monkeypatch, tmp_path, local=False, slow=("app_a",))
    assert "SLOW_TESTS" not in config


def test_a_seeded_local_job_accepts_the_slow_switch_before_its_first_run(tmp_path: Path) -> None:
    """Jenkins drops a parameter the job does not define yet; a pipeline's `parameters` are defined by its first run.
    So the seeded job carries SLOW_TESTS from the start, or the first nightly run would run the fast tests only."""
    script = "pipeline {\n    parameters {\n        booleanParam(name: 'SLOW_TESTS', defaultValue: false)\n    }\n}"
    config = job_config.local_job_config("p", script, tmp_path / "p")
    definition = config[config.index("<hudson.model.BooleanParameterDefinition>") :]
    assert "<name>SLOW_TESTS</name>" in definition and "<defaultValue>false</defaultValue>" in definition
    assert f"<customWorkspace>{(tmp_path / 'p').absolute()}</customWorkspace>" in config


def test_a_seeded_job_without_the_switch_defines_no_parameter(tmp_path: Path) -> None:
    config = job_config.local_job_config("p", "pipeline {\n}", tmp_path / "p")
    assert "ParametersDefinitionProperty" not in config
