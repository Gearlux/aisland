"""The morning report of the local Jenkins' nightly runs — what failed, written down, and a notification.

Root AGENTS.md "Two Test Runs" (user ruling 2026-10-06): the local orchestrator (`<workspace>.local`) builds every
project at 01:00 with its slow tests; a manual build runs the fast tests only. A night's failure must not wait for
somebody to open Jenkins, so the `<workspace>.nightly-check` job runs `aisland tests nightly` each weekday at 09:30
(`NIGHTLY_CHECK_CRON`): it reads every timer run of the orchestrator since the previous report from the Jenkins API,
writes `aisland/data/nightly/<date>.md` — each project's result, and for a failed one its failing stage, its failing
tests with their first error line, a quality gate's offending lines, the line a stage past its time limit wrote — shows
a desktop notification when a night failed,
had no run or is still building, and exits 1 then, which turns the check job red. Moving tests between the fast and the
slow run is not done here: it needs judgment, and the Claude app's scheduled task does it from this report.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from aisland.errors import AislandError
from aisland.paths import DATA_DIR
from aisland.projects import get_workspace_name
from aisland.services.jenkins import JenkinsService
from aisland.services.jenkins_jobs import NIGHTLY_CRON

#: One report per check, `<date>.md`; git-ignored like all of `aisland/data/`.
NIGHTLY_DIR = DATA_DIR / "nightly"
#: The cause Jenkins gives a build its timer started — the orchestrator's 01:00 run (`NIGHTLY_CRON`).
TIMER_CAUSE = "hudson.triggers.TimerTrigger$TimerTriggerCause"
#: A notification's length limit: mobile and desktop banners cut what is longer.
NOTIFICATION_LIMIT = 200
#: How many lines of a failed quality gate's console the report quotes.
GATE_LINES = 5
#: A quality gate's offending lines in a console: black, isort, flake8 (`path:line:col: CODE`), mypy.
_GATE_LINE = re.compile(r"^(would reformat |ERROR: .*(sorted|formatted)|\S+:\d+:\d+: [A-Z]+\d+ |\S+:\d+: error: )")
#: The line a stage past its time limit writes (`Jenkinsfile.local`'s `limited` helper: `ci_scaffold.TIMED_OUT_MESSAGE`,
#: the limits in `ci_scaffold.STAGE_LIMITS`), as the `error` step prints it at the end of the console. pytest wrote no
#: report then — its files are removed before the call — so this line is what the morning says.
_TIMED_OUT = re.compile(r"^ERROR: (?P<stage>.+?) timed out after (?P<minutes>\d+) min\b")


class JenkinsUnreachable(AislandError, ConnectionError):
    """Jenkins did not answer: not running, or the address is wrong."""


class Api(Protocol):
    """The two reads the report needs; a 404 is None."""

    base_url: str

    def json(self, path: str) -> Optional[Any]:  # Any: Jenkins' JSON, read by key
        """The parsed JSON at `path`, None for a 404."""

    def text(self, path: str) -> str:
        """The text at `path`, empty for a 404."""


class JenkinsApi:
    """The Jenkins REST API at `base_url` (the local Jenkins needs no login for reading)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"

    def _read(self, path: str) -> Optional[bytes]:
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=30) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise JenkinsUnreachable(f"{self.base_url}: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise JenkinsUnreachable(f"{self.base_url}: {getattr(error, 'reason', error)}") from error

    def json(self, path: str) -> Optional[Any]:  # Any: Jenkins' JSON, read by key
        body = self._read(path)
        return None if body is None else json.loads(body)

    def text(self, path: str) -> str:
        body = self._read(path)
        return "" if body is None else body.decode("utf-8", errors="replace")


def jenkins_url() -> str:
    """Inside a Jenkins build Jenkins' own address (`JENKINS_URL`); elsewhere the running Jenkins', else its default
    port's (:meth:`JenkinsService.url`)."""
    if os.environ.get("JENKINS_URL"):
        return os.environ["JENKINS_URL"]
    return f"{JenkinsService().url()}/"


def orchestrator_job() -> str:
    """The local orchestrator's job name, `<workspace>.local`."""
    return f"{get_workspace_name()}.local"


@dataclass
class FailedTest:
    name: str  # `<className>::<name>`, as Jenkins' test report names it
    error: str  # the first line of its error


@dataclass
class ProjectBuild:
    job: str
    number: int
    result: Optional[str]  # None while building
    minutes: float
    url: str
    failed_stage: Optional[str] = None
    failed_tests: List[FailedTest] = field(default_factory=list)
    gate_lines: List[str] = field(default_factory=list)
    timed_out: Optional[str] = None  # the line a stage past its time limit wrote, without its `ERROR: `

    @property
    def passed(self) -> bool:
        return self.result == "SUCCESS"


@dataclass
class NightlyRun:
    number: int
    started: str  # local time, ISO
    result: Optional[str]
    building: bool
    url: str
    projects: List[ProjectBuild]


@dataclass
class NightlyReport:
    orchestrator: str
    jenkins: str
    since: str  # local time, ISO: the window's start
    until: str
    runs: List[NightlyRun]
    jenkins_error: Optional[str] = None
    path: Optional[str] = None

    @property
    def failed(self) -> List[ProjectBuild]:
        return [p for run in self.runs for p in run.projects if not p.passed and p.result is not None]

    @property
    def ok(self) -> bool:
        """Jenkins answered, a nightly run happened, it finished, and every project passed."""
        if self.jenkins_error is not None or not self.runs:
            return False
        return not any(run.building for run in self.runs) and not self.failed

    def notification(self, where: str) -> Optional[str]:
        """One line, leading with what failed; None for a green night."""
        day = f"{datetime.fromisoformat(self.until):%m-%d}"
        if self.jenkins_error is not None:
            return f"nightly {day}: Jenkins did not answer at {self.jenkins}"
        if not self.runs:
            since = f"{datetime.fromisoformat(self.since):%m-%d %H:%M}"
            return f"nightly {day}: no {_nightly_time()} run since {since} — {where}"
        building = [run for run in self.runs if run.building]
        if building:
            running = sum(1 for p in building[-1].projects if p.result is None)
            return f"nightly {day}: {self.orchestrator} #{building[-1].number} is still building ({running} running) — {where}"
        if self.ok:
            return None
        total = sum(len(run.projects) for run in self.runs)
        head = f"nightly {day}: {len(self.failed)} of {total} failed — "
        tail = f" — {where}"
        items: List[str] = []
        room = NOTIFICATION_LIMIT - len(head) - len(tail)
        for index, build in enumerate(self.failed):
            item = f"{build.job.removesuffix('.local')} ({_what_failed(build)})"
            rest = len(self.failed) - index
            more = f", +{rest - 1} more" if rest > 1 else ""
            if len(", ".join(items + [item])) + len(more) > room:
                items.append(f"+{rest} more")
                break
            items.append(item)
        return (head + ", ".join(items) + tail)[:NOTIFICATION_LIMIT]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _nightly_time() -> str:
    """`NIGHTLY_CRON`'s hour and minute, `01:00`."""
    minute, hour = NIGHTLY_CRON.split()[:2]
    return f"{int(hour):02d}:{int(minute):02d}"


def _what_failed(build: ProjectBuild) -> str:
    if build.failed_tests:
        return f"{len(build.failed_tests)} test{'s' if len(build.failed_tests) != 1 else ''}"
    if build.timed_out:
        match = _TIMED_OUT.match("ERROR: " + build.timed_out)
        return f"timed out after {match.group('minutes')} min" if match else "timed out"
    return (build.failed_stage or build.result or "failed").lower()


def _causes(build: Dict[str, Any]) -> List[Dict[str, Any]]:  # Any: Jenkins' JSON, read by key
    return [cause for action in build.get("actions") or [] if action for cause in action.get("causes") or []]


def _when(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000).astimezone()


_BUILDS = "builds[number,result,building,timestamp,duration,url,actions[causes[_class,upstreamProject,upstreamBuild]]]"


def build_report(api: Api, orchestrator: str, since: datetime, now: datetime) -> NightlyReport:
    """Every timer run of `orchestrator` started after `since`, each project build it started, and what failed."""
    report = NightlyReport(orchestrator, api.base_url, since.isoformat(timespec="minutes"), now.isoformat(), [])
    try:
        builds = (api.json(f"job/{orchestrator}/api/json?tree={_BUILDS}") or {}).get("builds", [])
        nights = sorted(
            (
                b
                for b in builds
                if any(c.get("_class") == TIMER_CAUSE for c in _causes(b)) and _when(b["timestamp"]) > since
            ),
            key=lambda b: b["number"],
        )
        if not nights:
            return report
        jobs = [j["name"] for j in (api.json("api/json?tree=jobs[name]") or {}).get("jobs", [])]
        projects = sorted(j for j in jobs if j.endswith(".local") and j != orchestrator)
        started = {job: (api.json(f"job/{job}/api/json?tree={_BUILDS}") or {}).get("builds", []) for job in projects}
        for night in nights:
            run = NightlyRun(
                night["number"],
                _when(night["timestamp"]).isoformat(timespec="minutes"),
                night.get("result"),
                bool(night.get("building")),
                night.get("url", ""),
                [],
            )
            for job in projects:
                for build in started[job]:
                    upstream = [c for c in _causes(build) if c.get("upstreamProject") == orchestrator]
                    if any(c.get("upstreamBuild") == night["number"] for c in upstream):
                        run.projects.append(_project(api, job, build))
                        break
            report.runs.append(run)
    except JenkinsUnreachable as error:
        report.jenkins_error = str(error)
    return report


def _project(api: Api, job: str, build: Dict[str, Any]) -> ProjectBuild:  # Any: Jenkins' JSON
    result = None if build.get("building") else build.get("result")
    project = ProjectBuild(
        job, build["number"], result, round(build.get("duration", 0) / 60_000, 1), build.get("url", "")
    )
    if result in (None, "SUCCESS"):
        return project
    number = build["number"]
    stages = (api.json(f"job/{job}/{number}/wfapi/describe") or {}).get("stages", [])
    project.failed_stage = next((s["name"] for s in stages if s.get("status") == "FAILED"), None)
    suites = (
        api.json(f"job/{job}/{number}/testReport/api/json?tree=suites[cases[className,name,status,errorDetails]]") or {}
    ).get("suites", [])
    for suite in suites:
        for case in suite.get("cases", []):
            if case.get("status") in ("FAILED", "REGRESSION"):
                first = (case.get("errorDetails") or "").strip().splitlines()
                project.failed_tests.append(
                    FailedTest(f"{case['className']}::{case['name']}", first[0] if first else "")
                )
    if not project.failed_tests:
        console = api.text(f"job/{job}/{number}/consoleText").splitlines()
        project.timed_out = next((line[len("ERROR: ") :] for line in console if _TIMED_OUT.match(line)), None)
        project.gate_lines = [line for line in console if _GATE_LINE.match(line)][:GATE_LINES]
    return project


def report_window(nightly_dir: Path, now: datetime) -> datetime:
    """When the previous report was written (the newest one dated before today); without one, a day ago."""
    today = now.date()
    previous: List[Tuple[date, Path]] = []
    if nightly_dir.is_dir():
        for path in nightly_dir.glob("*.md"):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if day < today:
                previous.append((day, path))
    if not previous:
        return now - timedelta(hours=24)
    _, newest = max(previous)
    return datetime.fromtimestamp(newest.stat().st_mtime).astimezone()


def render_markdown(report: NightlyReport) -> str:
    """The report file: one section per run, one per failed project, failures verbatim."""
    lines = [f"# Nightly runs — {datetime.fromisoformat(report.until):%Y-%m-%d}", ""]
    lines.append(f"Jenkins {report.jenkins} · {report.orchestrator} timer runs since {report.since}")
    if report.jenkins_error is not None:
        lines += ["", f"**Jenkins did not answer:** {report.jenkins_error}"]
    elif not report.runs:
        lines += [
            "",
            f"**No {_nightly_time()} run** of {report.orchestrator} started in that window.",
        ]
    for run in report.runs:
        state = "still building" if run.building else (run.result or "")
        lines += [
            "",
            f"## {report.orchestrator} #{run.number} — {datetime.fromisoformat(run.started):%Y-%m-%d %H:%M}, {state}",
            "",
        ]
        lines += [run.url, "", "| project | result | duration |", "|---|---|---|"]
        lines += [f"| {p.job} | {p.result or 'building'} | {p.minutes:g} min |" for p in run.projects]
        for project in run.projects:
            if project.passed or project.result is None:
                continue
            where = f" in {project.failed_stage}" if project.failed_stage else ""
            lines += ["", f"### {project.job} #{project.number} — {project.result}{where}", "", project.url]
            if project.timed_out:
                lines += ["", project.timed_out]
            if project.failed_tests:
                lines += [""] + [f"- `{t.name}` — {t.error}" for t in project.failed_tests]
            if project.gate_lines:
                lines += ["", "```"] + [f"    {line}" for line in project.gate_lines] + ["```"]
    return "\n".join(lines) + "\n"


def notify_desktop(message: str) -> None:
    """A desktop banner: `osascript` on macOS, `notify-send` where it exists; never fails the check."""
    title = get_workspace_name()
    if sys.platform == "darwin":
        quoted = message.replace("\\", "\\\\").replace('"', '\\"')
        argv = ["osascript", "-e", f'display notification "{quoted}" with title "{title}"']
    elif shutil.which("notify-send"):
        argv = ["notify-send", title, message]
    else:
        return
    try:
        subprocess.run(argv, check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        return


def run_nightly(
    api: Optional[Api] = None,
    nightly_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    orchestrator: Optional[str] = None,
    notify: bool = True,
) -> Tuple[NightlyReport, int]:
    """Report the nightly runs since the previous report: write the file, notify when needed; 0 when all is well."""
    now = now or datetime.now().astimezone()
    folder = nightly_dir or NIGHTLY_DIR
    since = report_window(folder, now)
    report = build_report(api or JenkinsApi(jenkins_url()), orchestrator or orchestrator_job(), since, now)
    path = folder / f"{now:%Y-%m-%d}.md"
    report.path = str(path)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")
    message = report.notification(str(path))
    if notify and message is not None:
        notify_desktop(message)
    return report, 0 if report.ok else 1
