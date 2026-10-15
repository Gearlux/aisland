"""The morning report of the local Jenkins' nightly runs (`aisland tests nightly`, the `<workspace>.nightly-check` job).

Root AGENTS.md "Two Test Runs" (user ruling 2026-10-06): the orchestrator's 01:00 run builds every project with its
slow tests; a failure of a night is written to `aisland/data/nightly/<date>.md`, notified, and turns the check job red.
The Jenkins API is replaced by a stand-in answering the same paths, so no test needs a running Jenkins.
"""

import json
import os
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner

from aisland import nightly
from aisland.cli import main
from aisland.services.jenkins import NIGHTLY_CHECK_CRON, TIMED_OUT_MESSAGE, JenkinsService

TIMER = "hudson.triggers.TimerTrigger$TimerTriggerCause"
USER = "hudson.model.Cause$UserIdCause"
NOW = datetime(2026, 10, 7, 9, 30).astimezone()
NIGHT = datetime(2026, 10, 7, 1, 0).astimezone()


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _orchestrator_build(
    number: int, when: datetime, cause: str, result: Optional[str] = "SUCCESS", building: bool = False
) -> Dict[str, Any]:
    return {
        "number": number,
        "result": result,
        "building": building,
        "timestamp": _ms(when),
        "duration": 0 if building else 90 * 60_000,
        "url": f"http://jenkins.test/job/ws.local/{number}/",
        "actions": [{"causes": [{"_class": cause}]}, {}],
    }


def _project_build(job: str, number: int, upstream: int, result: Optional[str] = "SUCCESS") -> Dict[str, Any]:
    return {
        "number": number,
        "result": result,
        "building": result is None,
        "timestamp": _ms(NIGHT + timedelta(minutes=number)),
        "duration": 3 * 60_000,
        "url": f"http://jenkins.test/job/{job}/{number}/",
        "actions": [{"causes": [{"upstreamProject": "ws.local", "upstreamBuild": upstream}]}],
    }


class FakeJenkins:
    """Answers the API paths the report asks for (query strings ignored); a path it does not know is a 404."""

    base_url = "http://jenkins.test/"

    def __init__(self, answers: Dict[str, Any], texts: Optional[Dict[str, str]] = None) -> None:
        self.answers = answers
        self.texts = texts or {}

    def json(self, path: str) -> Optional[Any]:
        return self.answers.get(path.split("?", 1)[0])

    def text(self, path: str) -> str:
        return self.texts.get(path.split("?", 1)[0], "")


class DownJenkins(FakeJenkins):
    def __init__(self) -> None:
        super().__init__({})

    def json(self, path: str) -> Optional[Any]:
        raise nightly.JenkinsUnreachable("http://jenkins.test/: [Errno 61] Connection refused")


def _night(orchestrator_builds: Optional[List[Dict[str, Any]]] = None) -> FakeJenkins:
    """One 01:00 run (#26): alpha passes, beta fails two slow tests, gamma fails black; plus a manual run (#25)."""
    builds = orchestrator_builds or [
        _orchestrator_build(26, NIGHT, TIMER, result="FAILURE"),
        _orchestrator_build(25, NIGHT - timedelta(hours=15), USER, result="FAILURE"),
    ]
    answers: Dict[str, Any] = {
        "api/json": {
            "jobs": [{"name": n} for n in ("ws.local", "ws.nightly-check", "alpha.local", "beta.local", "gamma.local")]
        },
        "job/ws.local/api/json": {"builds": builds},
        "job/alpha.local/api/json": {
            "builds": [_project_build("alpha.local", 10, 26), _project_build("alpha.local", 9, 25, "FAILURE")]
        },
        "job/beta.local/api/json": {"builds": [_project_build("beta.local", 5, 26, "FAILURE")]},
        "job/gamma.local/api/json": {"builds": [_project_build("gamma.local", 7, 26, "FAILURE")]},
        "job/beta.local/5/wfapi/describe": {
            "stages": [{"name": "Unit Tests", "status": "SUCCESS"}, {"name": "Slow Tests", "status": "FAILED"}]
        },
        "job/beta.local/5/testReport/api/json": {
            "suites": [
                {
                    "cases": [
                        {
                            "className": "tests.test_ref",
                            "name": "test_file[a]",
                            "status": "FAILED",
                            "errorDetails": "AssertionError: residual 0.3 %\nassert 0.003 < 0.0015",
                        },
                        {
                            "className": "tests.test_ref",
                            "name": "test_file[b]",
                            "status": "REGRESSION",
                            "errorDetails": "ValueError: no burst",
                        },
                        {
                            "className": "tests.test_ref",
                            "name": "test_file[c]",
                            "status": "PASSED",
                            "errorDetails": None,
                        },
                    ]
                }
            ]
        },
        "job/gamma.local/7/wfapi/describe": {"stages": [{"name": "Black", "status": "FAILED"}]},
    }
    texts = {
        "job/gamma.local/7/consoleText": "+ black --check --diff gamma tests\n"
        "would reformat /ws/gamma/tests/test_a.py\nOh no! 💥 💔 💥\n1 file would be reformatted.\n"
    }
    return FakeJenkins(answers, texts)


def _report(api: FakeJenkins, since: datetime = NOW - timedelta(hours=24)) -> nightly.NightlyReport:
    return nightly.build_report(api, "ws.local", since=since, now=NOW)


# --------------------------------------------------------------------------
# What a night's report holds
# --------------------------------------------------------------------------


def test_the_report_names_each_project_of_the_nightly_run_and_what_failed() -> None:
    report = _report(_night())
    (run,) = report.runs
    assert run.number == 26 and run.result == "FAILURE"
    by_job = {p.job: p for p in run.projects}
    assert sorted(by_job) == ["alpha.local", "beta.local", "gamma.local"]
    assert by_job["alpha.local"].number == 10 and by_job["alpha.local"].result == "SUCCESS"  # #9 was the manual run's
    beta = by_job["beta.local"]
    assert beta.failed_stage == "Slow Tests"
    assert [(t.name, t.error) for t in beta.failed_tests] == [
        ("tests.test_ref::test_file[a]", "AssertionError: residual 0.3 %"),
        ("tests.test_ref::test_file[b]", "ValueError: no burst"),
    ]
    gamma = by_job["gamma.local"]
    assert gamma.failed_stage == "Black" and gamma.gate_lines == ["would reformat /ws/gamma/tests/test_a.py"]
    assert not report.ok


def test_a_manual_run_is_no_nightly_run() -> None:
    report = _report(_night([_orchestrator_build(25, NIGHT, USER)]))
    assert report.runs == [] and not report.ok
    assert report.notification("x.md") == "nightly 10-07: no 01:00 run since 10-06 09:30 — x.md"


def test_every_night_since_the_previous_report_is_reported() -> None:
    """A weekday check: on a Monday that is Saturday's, Sunday's and Monday's night."""
    builds = [_orchestrator_build(n, NIGHT - timedelta(days=26 - n), TIMER) for n in (24, 25, 26)]
    report = _report(_night(builds), since=NOW - timedelta(days=3))
    assert [r.number for r in report.runs] == [24, 25, 26]


def test_a_run_before_the_window_is_left_out() -> None:
    builds = [_orchestrator_build(24, NIGHT - timedelta(days=2), TIMER), _orchestrator_build(26, NIGHT, TIMER)]
    assert [r.number for r in _report(_night(builds)).runs] == [26]


def test_a_run_still_building_is_reported_not_judged() -> None:
    report = _report(_night([_orchestrator_build(26, NIGHT, TIMER, result=None, building=True)]))
    assert not report.ok
    assert (report.notification("x.md") or "").startswith("nightly 10-07: ws.local #26 is still building")


def test_jenkins_not_answering_is_the_finding() -> None:
    report = _report(DownJenkins())
    assert report.jenkins_error == "http://jenkins.test/: [Errno 61] Connection refused" and not report.ok
    assert report.notification("x.md") == "nightly 10-07: Jenkins did not answer at http://jenkins.test/"


def test_a_green_night_is_quiet() -> None:
    api = _night([_orchestrator_build(26, NIGHT, TIMER)])
    for job in ("beta.local", "gamma.local"):
        api.answers[f"job/{job}/api/json"] = {"builds": []}
    report = _report(api)
    assert report.ok and report.notification("x.md") is None


def test_the_notification_leads_with_what_failed_and_stays_short() -> None:
    message = _report(_night()).notification("aisland/data/nightly/2026-10-07.md")
    assert message == (
        "nightly 10-07: 2 of 3 failed — beta (2 tests), gamma (black) — aisland/data/nightly/2026-10-07.md"
    )
    many = _night()
    many.answers["api/json"]["jobs"] += [{"name": f"p{i}.local"} for i in range(30)]
    for i in range(30):
        many.answers[f"job/p{i}.local/api/json"] = {"builds": [_project_build(f"p{i}.local", 1, 26, "FAILURE")]}
    assert len(_report(many).notification("aisland/data/nightly/2026-10-07.md") or "") <= 200


def test_the_markdown_shows_each_failure_verbatim_with_its_build() -> None:
    text = nightly.render_markdown(_report(_night()))
    assert "# Nightly runs — 2026-10-07" in text
    assert "## ws.local #26 — 2026-10-07 01:00, FAILURE" in text
    assert "| alpha.local | SUCCESS | 3 min |" in text
    assert "### beta.local #5 — FAILURE in Slow Tests" in text and "http://jenkins.test/job/beta.local/5/" in text
    assert "- `tests.test_ref::test_file[a]` — AssertionError: residual 0.3 %" in text
    assert "    would reformat /ws/gamma/tests/test_a.py" in text


# --------------------------------------------------------------------------
# The window, the file, the notification
# --------------------------------------------------------------------------


def test_the_window_starts_when_the_previous_report_was_written(tmp_path: Path) -> None:
    previous = tmp_path / "2026-10-06.md"
    previous.write_text("")
    written = datetime(2026, 10, 6, 9, 31).astimezone().timestamp()
    os.utime(previous, (written, written))
    (tmp_path / "2026-10-07.md").write_text("")  # today's, from an earlier check today: not the previous report
    assert nightly.report_window(tmp_path, NOW) == datetime(2026, 10, 6, 9, 31).astimezone()


def test_without_a_previous_report_the_window_is_a_day(tmp_path: Path) -> None:
    assert nightly.report_window(tmp_path / "none", NOW) == NOW - timedelta(hours=24)


def test_a_check_writes_the_dated_report_and_notifies_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: List[str] = []
    monkeypatch.setattr(nightly, "notify_desktop", sent.append)
    report, status = nightly.run_nightly(api=_night(), nightly_dir=tmp_path, now=NOW, orchestrator="ws.local")
    assert status == 1
    assert (tmp_path / "2026-10-07.md").read_text() == nightly.render_markdown(report)
    assert sent == [report.notification(str(tmp_path / "2026-10-07.md"))]
    nightly.run_nightly(api=_night(), nightly_dir=tmp_path, now=NOW, orchestrator="ws.local", notify=False)
    assert len(sent) == 1


def test_a_green_check_exits_zero_and_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: List[str] = []
    monkeypatch.setattr(nightly, "notify_desktop", sent.append)
    api = _night([_orchestrator_build(26, NIGHT, TIMER)])
    for job in ("beta.local", "gamma.local"):
        api.answers[f"job/{job}/api/json"] = {"builds": []}
    _, status = nightly.run_nightly(api=api, nightly_dir=tmp_path, now=NOW, orchestrator="ws.local")
    assert status == 0 and sent == []


def test_on_macos_the_notification_is_a_desktop_banner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[List[str]] = []
    monkeypatch.setattr(nightly, "get_workspace_name", lambda: "acme")  # it reads git: not in a test
    monkeypatch.setattr(nightly.sys, "platform", "darwin")
    monkeypatch.setattr(nightly.subprocess, "run", lambda argv, **_: calls.append(argv))
    nightly.notify_desktop('nightly 10-07: "beta" failed \\ 2 tests')
    assert calls == [
        ["osascript", "-e", 'display notification "nightly 10-07: \\"beta\\" failed \\\\ 2 tests" with title "acme"']
    ]


def test_elsewhere_without_a_notifier_nothing_is_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[List[str]] = []
    monkeypatch.setattr(nightly, "get_workspace_name", lambda: "acme")  # it reads git: not in a test
    monkeypatch.setattr(nightly.sys, "platform", "linux")
    monkeypatch.setattr(nightly.shutil, "which", lambda name: None)
    monkeypatch.setattr(nightly.subprocess, "run", lambda argv, **_: calls.append(argv))
    nightly.notify_desktop("nightly 10-07: 1 of 3 failed")
    assert calls == []


class _ClockAtNow(datetime):
    """The clock `aisland tests nightly` reads, stopped at NOW: on the real one, the fixture's 01:00 run of 2026-10-07
    left the 24-hour window from 2026-10-08 01:00 on and the report came back empty."""

    @classmethod
    def now(cls, tz: Optional[tzinfo] = None) -> "_ClockAtNow":
        return cls.fromtimestamp(NOW.timestamp(), tz)


def test_the_cli_prints_the_report_as_json_and_exits_one_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nightly, "datetime", _ClockAtNow)
    monkeypatch.setattr(nightly, "NIGHTLY_DIR", tmp_path)
    monkeypatch.setattr(nightly, "jenkins_url", lambda: "http://jenkins.test/")
    monkeypatch.setattr(nightly, "JenkinsApi", lambda url: _night())
    monkeypatch.setattr(nightly, "orchestrator_job", lambda: "ws.local")
    monkeypatch.setattr(nightly, "notify_desktop", lambda message: None)
    result = CliRunner().invoke(main, ["tests", "nightly", "--json", "--no-notify"])
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert [r["number"] for r in data["runs"]] == [26] and data["path"] == str(tmp_path / "2026-10-07.md")


# --------------------------------------------------------------------------
# The Jenkins job that runs it
# --------------------------------------------------------------------------


def test_the_check_job_runs_the_report_on_weekday_mornings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from aisland import manager

    monkeypatch.setattr(manager, "get_workspace_name", lambda: "ws")
    svc = JenkinsService()
    svc.service_data_dir = tmp_path / "jenkins"
    svc._create_nightly_check_job()
    config = (svc.service_data_dir / "jobs" / "ws.nightly-check" / "config.xml").read_text()
    assert NIGHTLY_CHECK_CRON == "30 9 * * 1-5"
    assert f"cron('{NIGHTLY_CHECK_CRON}')" in config
    timer = config[config.index("<hudson.triggers.TimerTrigger>") : config.index("</hudson.triggers.TimerTrigger>")]
    assert f"<spec>{NIGHTLY_CHECK_CRON}</spec>" in timer
    assert 'sh "${env.WORKSPACE_ROOT}/.venv/bin/aisland tests nightly"' in config
    assert "label 'built-in'" in config


def test_only_a_local_setup_seeds_the_check_job() -> None:
    import inspect

    source = inspect.getsource(JenkinsService.init_pipelines)
    assert "if local:\n            self._create_nightly_check_job()" in source


# --------------------------------------------------------------------------
# A stage past its time limit (user choice 2026-10-15)
# --------------------------------------------------------------------------
# `Jenkinsfile.local` holds each long stage to a limit (`STAGE_LIMITS`); past it the stage fails with ONE console line
# naming the stage and the limit. pytest wrote no report (its files are removed before the call), so that line is what
# the morning says — never yesterday's tests as passed, and never a bare "unit tests".


def _timed_out_line(stage: str, minutes: int) -> str:
    """The line the pipeline's `limited` helper writes, as Groovy renders it."""
    return TIMED_OUT_MESSAGE.replace("${stage}", stage).replace("${minutes}", str(minutes))


def test_a_timed_out_stage_is_reported_with_its_limit() -> None:
    line = _timed_out_line("Slow Tests", 60)
    api = FakeJenkins(
        {
            "api/json": {"jobs": [{"name": n} for n in ("ws.local", "beta.local")]},
            "job/ws.local/api/json": {"builds": [_orchestrator_build(26, NIGHT, TIMER, result="FAILURE")]},
            "job/beta.local/api/json": {"builds": [_project_build("beta.local", 5, 26, "FAILURE")]},
            "job/beta.local/5/wfapi/describe": {
                "stages": [{"name": "Unit Tests", "status": "SUCCESS"}, {"name": "Slow Tests", "status": "FAILED"}]
            },
        },
        {
            "job/beta.local/5/consoleText": "+ pytest tests -m slow --junitxml=slow-test-report.xml\n"
            "tests/test_reference.py ......\nCancelling nested steps due to timeout\n"
            "Sending interrupt signal to process\nAfter 20s process did not stop\n[Pipeline] error\n"
            f"[Pipeline] End of Pipeline\nERROR: {line}\nFinished: FAILURE\n"
        },
    )
    report = _report(api)
    (beta,) = report.runs[0].projects
    assert beta.failed_stage == "Slow Tests" and beta.failed_tests == [] and beta.gate_lines == []
    assert beta.timed_out == line
    assert report.notification("x.md") == "nightly 10-07: 1 of 1 failed — beta (timed out after 60 min) — x.md"
    text = nightly.render_markdown(report)
    assert "### beta.local #5 — FAILURE in Slow Tests" in text and f"\n{line}\n" in text


def test_a_build_failing_any_other_way_reads_as_before() -> None:
    """The CON pin: failed tests and a quality gate's lines are reported as they were; nothing is called a timeout."""
    report = _report(_night())
    assert all(p.timed_out is None for run in report.runs for p in run.projects)
    assert "timed out" not in nightly.render_markdown(report)


def test_the_line_the_pipeline_writes_is_the_line_the_report_reads() -> None:
    """One wording, two readers: the scaffold's helper and this report's pattern."""
    match = nightly._TIMED_OUT.match("ERROR: " + _timed_out_line("Unit Tests", 15))
    assert match is not None and match.group("stage") == "Unit Tests" and match.group("minutes") == "15"
    assert nightly._TIMED_OUT.match("ERROR: script returned exit code 143") is None
