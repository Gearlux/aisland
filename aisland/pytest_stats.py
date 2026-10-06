"""Every pytest run of a workspace project, recorded — and the report that reads the record back.

Root AGENTS.md "Two Test Runs" (user ruling 2026-10-05): a project's fast run (plain ``pytest``) finishes within five
minutes and holds the unit tests plus only the integration tests that break; every other test carries ``slow``. Which
tests are slow and which ones break is read from statistics, never guessed. CI keeps each build's per-test results
(Jenkins' JUnit record), but code reaches CI only after it passes locally — 11 of the 351 Jenkins builds recorded by
2026-10-05 had a failing test — so the breaks that decide the split happen in local runs. Hence two halves:

* a pytest plugin (entry point ``pytest11`` named ``aisland_test_stats``, so every run in the workspace ``.venv`` loads
  it) that appends each run of a workspace project to ``aisland/data/test-stats/<project>/<date>.jsonl``: one line for
  the run (leg, whole or partial, wall seconds, exit status, commit) and one per test (seconds, outcome, ``slow`` mark);
* the reader behind ``aisland tests report <project>``, which merges those lines with the Jenkins builds' JUnit results
  and answers what the rule asks: the fast run's time against the budget, the slowest fast tests, the tests that
  failed, what to move to the slow run and what back into the fast run.

The plugin must never cost a run its result: it records only a run whose rootdir is ``<workspace>/<project>``, imports
only the standard library (it loads in every pytest start, so no ``aisland.manager`` with its click and yaml), and a
record it cannot write is one printed line. ``-p no:aisland_test_stats`` disables it for one run.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Literal, Optional, Tuple

if TYPE_CHECKING:
    import pytest

# Resolved from this file exactly as `aisland.manager` resolves `DATA_DIR` (pinned by
# `tests/test_pytest_stats.py::test_the_records_live_in_aislands_data_folder`).
_PACKAGE_DIR = Path(__file__).parent.absolute()
#: The workspace root: aisland's checkout sits directly in it.
WORKSPACE_ROOT = _PACKAGE_DIR.parent.parent
#: One folder per project, one JSON-lines file per day.
STATS_DIR = _PACKAGE_DIR.parent / "data" / "test-stats"
#: The local Jenkins' job folders (`<project>.local`, `<project>`), each build's JUnit record in it.
JENKINS_JOBS_DIR = _PACKAGE_DIR.parent / "data" / "jenkins" / "jobs"

#: The fast run's budget (user ruling 2026-10-05: "around 5 min max per project").
FAST_RUN_BUDGET_SECONDS = 300.0
#: Day files older than this are removed when a run is recorded: a full fast run of a large project is a few hundred
#: kilobytes, and the rule reads the last weeks, not the last year.
RETENTION_DAYS = 90
#: How far back the report reads by default.
DEFAULT_WINDOW_DAYS = 30
#: How many of the slowest fast-run tests the report lists.
SLOWEST_SHOWN = 10

#: Which of a project's two runs this was: `fast` (plain `pytest`, i.e. `-m 'not slow'`, or plain `pytest` in a project
#: that marks nothing slow), `slow` (`-m slow`), `all` (`-m ''` where `slow` is registered), `other` (any other `-m`).
Leg = Literal["fast", "slow", "all", "other"]
#: A test's result over its setup, call and teardown: the worst phase wins.
Outcome = Literal["passed", "skipped", "failed", "error"]
_SEVERITY: Dict[str, int] = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
#: Exit statuses of a run that ran to its end (0 all passed, 1 some failed); an interrupted run says nothing of time.
_COMPLETE_EXITS = (0, 1)


# --------------------------------------------------------------------------
# The plugin
# --------------------------------------------------------------------------


def pytest_configure(config: "pytest.Config") -> None:
    """Start recording when this run is a workspace project's own: pytest's rootdir is `<workspace>/<project>`."""
    if hasattr(config, "workerinput"):  # an xdist worker: its controller records the run
        return
    # A Jenkins build: the local Jenkins builds IN the project folder (`customWorkspace`) with the workspace `.venv`,
    # and keeps its own JUnit record, which `build_report` reads; recording the run here would count it twice.
    if os.environ.get("JENKINS_URL"):
        return
    if not _is_workspace_project(Path(config.rootpath)):
        return
    config.pluginmanager.register(_Recorder(config, STATS_DIR), "aisland_test_stats_recorder")


def _is_workspace_project(folder: Path) -> bool:
    """A direct child of the workspace root (a `.gitmodules` there) holding a `pyproject.toml`.

    A test's own tmp project (`pytester`, a subprocess in `tmp_path`) is not one: its inner pytest run is no run of
    the project.
    """
    root = WORKSPACE_ROOT.resolve()
    return (
        folder.resolve().parent == root and (root / ".gitmodules").is_file() and (folder / "pyproject.toml").is_file()
    )


def run_leg(markexpr: str, slow_registered: bool) -> Leg:
    """Which run a `-m` expression makes of a project that does (or does not) register the `slow` marker."""
    expression = " ".join(markexpr.split())
    if expression == "not slow":
        return "fast"
    if expression == "slow":
        return "slow"
    if not expression:
        return "all" if slow_registered else "fast"
    return "other"


def _is_whole_run(config: "pytest.Config") -> bool:
    """True when the run is the project's whole leg — only then does its wall time measure the fast run.

    Partial: a file or a test named, `-k`, `-x`/`--maxfail`, `--lf`, `--sw`, `--deselect`.
    """
    option = config.option
    if option.keyword or option.maxfail or getattr(option, "lf", False) or getattr(option, "stepwise", False):
        return False
    if getattr(option, "deselect", None):
        return False
    here = Path(config.invocation_params.dir)
    return all("::" not in arg and (here / arg).is_dir() for arg in config.args)


def _phase_outcome(report: "pytest.TestReport") -> Outcome:
    if report.passed:
        return "passed"
    if report.skipped:  # a skip, or an expected failure
        return "skipped"
    return "failed" if report.when == "call" else "error"


@dataclass
class _Result:
    slow: bool
    seconds: float = 0.0
    outcome: Outcome = "passed"


class _Recorder:
    """One run's record, held while it runs and written once when the session ends."""

    def __init__(self, config: "pytest.Config", stats_dir: Path) -> None:
        self._config = config
        self._folder = stats_dir / Path(config.rootpath).name
        self._project_dir = Path(config.rootpath)
        markers = config.getini("markers")
        slow_registered = any(str(line).split(":", 1)[0].strip() == "slow" for line in markers)
        self._leg = run_leg(config.option.markexpr or "", slow_registered)
        self._whole = _is_whole_run(config)
        self._started = time.monotonic()
        self._at = datetime.now().astimezone()
        self._tests: Dict[str, _Result] = {}

    def pytest_runtest_logreport(self, report: "pytest.TestReport") -> None:
        result = self._tests.setdefault(report.nodeid, _Result(slow="slow" in report.keywords))
        result.seconds += report.duration
        outcome = _phase_outcome(report)
        if _SEVERITY[outcome] > _SEVERITY[result.outcome]:
            result.outcome = outcome

    def pytest_sessionfinish(self, session: "pytest.Session", exitstatus: int) -> None:
        if not self._tests:  # `--collect-only`, or nothing selected: no run to record
            return
        run_id = f"{self._at:%Y%m%dT%H%M%S}-{os.getpid()}"
        lines: List[Dict[str, object]] = [
            {
                "kind": "run",
                "run": run_id,
                "at": self._at.isoformat(timespec="seconds"),
                "commit": _commit(self._project_dir),
                "leg": self._leg,
                "whole": self._whole,
                "seconds": round(time.monotonic() - self._started, 3),
                "exit": int(exitstatus),
                "tests": len(self._tests),
            }
        ]
        lines += [
            {
                "kind": "test",
                "run": run_id,
                "test": nodeid,
                "seconds": round(result.seconds, 3),
                "outcome": result.outcome,
                "slow": result.slow,
            }
            for nodeid, result in self._tests.items()
        ]
        try:
            _append(self._folder, self._at.date(), lines)
        except OSError as error:
            reporter = self._config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(f"aisland test stats: this run was not recorded ({error})")


def _commit(folder: Path) -> str:
    """The project's HEAD, short; empty outside a git checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _append(folder: Path, day: date, lines: List[Dict[str, object]]) -> None:
    """Append the run to the day's file in ONE write (sessions run tests side by side), then drop expired days."""
    folder.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8")
    descriptor = os.open(folder / f"{day.isoformat()}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    cutoff = day - timedelta(days=RETENTION_DAYS)
    for path in folder.glob("*.jsonl"):
        stamp = _day_of(path)
        if stamp is not None and stamp < cutoff:
            path.unlink(missing_ok=True)


def _day_of(path: Path) -> Optional[date]:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def junit_address(nodeid: str) -> str:
    """The `<classname>::<name>` pytest's JUnit report gives a nodeid — how CI's records name the same test.

    `tests/test_a.py::TestX::test_y[1]` -> `tests.test_a.TestX::test_y[1]` (pytest's `junitxml.mangle_test_address`).
    """
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = names[0].replace("/", ".").removesuffix(".py")
    names[-1] += bracket + params
    return ".".join(names[:-1]) + "::" + names[-1]


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


@dataclass
class Tally:
    """One test over the window's local runs and Jenkins builds."""

    test: str  # the pytest nodeid; the JUnit address when only Jenkins ran it
    seconds: float  # the median over the records that did not skip it
    runs: int
    failures: int  # failed or errored
    last_failure: Optional[str]  # its date, ISO
    slow: Optional[bool]  # its `slow` mark in the latest local run; None when only Jenkins ran it


@dataclass
class StatsReport:
    """What `aisland tests report` prints; `to_dict()` is the JSON an MCP tool would return."""

    project: str
    days: int
    budget_seconds: float
    local_runs: int
    ci_builds: int
    whole_fast_runs: int  # complete runs of the whole fast leg — the ones whose time counts
    fast_run_seconds: Optional[float]  # their median
    last_fast_run_seconds: Optional[float]
    slowest_fast: List[Tally]
    failed: List[Tally]  # every test that failed in the window, most failures first
    to_slow: List[Tally]  # never-failed fast tests, slowest first, whose time covers the overrun; empty within budget
    to_fast: List[Tally]  # slow tests that failed: they catch breaks

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class _Record:
    """One test's result in one run or build."""

    address: str
    nodeid: Optional[str]
    day: date
    at: datetime
    seconds: float
    outcome: str
    slow: Optional[bool]


@dataclass
class _Run:
    """One recorded local run, as its run line states it."""

    run: str
    at: datetime
    leg: str
    whole: bool
    seconds: float
    exit: int


def build_report(
    project: str,
    days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
    stats_dir: Optional[Path] = None,
    jenkins_jobs_dir: Optional[Path] = None,
) -> StatsReport:
    """Read the last `days` of PROJECT's local runs and Jenkins builds and say what the two-run rule needs."""
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=days)
    runs, local = _local_records((stats_dir or STATS_DIR) / project, cutoff)
    builds, ci = _jenkins_records(project, jenkins_jobs_dir or JENKINS_JOBS_DIR, cutoff)
    tallies = _tallies(local + ci)

    fast = sorted(
        (r for r in runs if r.leg == "fast" and r.whole and r.exit in _COMPLETE_EXITS),
        key=lambda r: r.at,
    )
    fast_seconds = statistics.median(r.seconds for r in fast) if fast else None
    by_seconds = sorted(tallies, key=lambda t: (-t.seconds, t.test))
    failed = sorted((t for t in tallies if t.failures), key=lambda t: (-t.failures, str(t.last_failure), t.test))
    return StatsReport(
        project=project,
        days=days,
        budget_seconds=FAST_RUN_BUDGET_SECONDS,
        local_runs=len(runs),
        ci_builds=builds,
        whole_fast_runs=len(fast),
        fast_run_seconds=fast_seconds,
        last_fast_run_seconds=fast[-1].seconds if fast else None,
        slowest_fast=[t for t in by_seconds if t.slow is False][:SLOWEST_SHOWN],
        failed=failed,
        to_slow=_to_slow(by_seconds, fast_seconds),
        to_fast=[t for t in failed if t.slow is True],
    )


def _to_slow(by_seconds: List[Tally], fast_seconds: Optional[float]) -> List[Tally]:
    """The never-failed fast tests, slowest first, until their seconds cover the fast run's overrun."""
    if fast_seconds is None or fast_seconds <= FAST_RUN_BUDGET_SECONDS:
        return []
    overrun, moved = fast_seconds - FAST_RUN_BUDGET_SECONDS, []
    for tally in by_seconds:
        if overrun <= 0:
            break
        if tally.slow is False and not tally.failures:
            moved.append(tally)
            overrun -= tally.seconds
    return moved


def _local_records(folder: Path, cutoff: datetime) -> Tuple[List[_Run], List[_Record]]:
    """The window's recorded runs, and each of their tests' results."""
    runs: Dict[str, _Run] = {}
    tests: List[Tuple[str, str, float, str, bool]] = []
    for line in _lines(folder, cutoff.date()):
        try:
            if line.get("kind") == "run":
                run = _Run(
                    str(line["run"]),
                    datetime.fromisoformat(str(line["at"])),
                    str(line["leg"]),
                    bool(line["whole"]),
                    float(line["seconds"]),
                    int(line["exit"]),
                )
                if run.at >= cutoff:
                    runs[run.run] = run
            elif line.get("kind") == "test":
                tests.append(
                    (
                        str(line["run"]),
                        str(line["test"]),
                        float(line["seconds"]),
                        str(line["outcome"]),
                        bool(line["slow"]),
                    )
                )
        except (KeyError, TypeError, ValueError):
            continue  # a line of another shape: skipped like a damaged one
    records = []
    for run_id, nodeid, seconds, outcome, slow in tests:
        found = runs.get(run_id)
        if found is None:  # outside the window, or its run line was lost
            continue
        records.append(_Record(junit_address(nodeid), nodeid, found.at.date(), found.at, seconds, outcome, slow))
    return list(runs.values()), records


def _lines(folder: Path, first_day: date) -> Iterator[Dict[str, Any]]:
    """The JSON lines of the day files from `first_day` on; a damaged line (a run killed mid-write) is skipped.

    Values are `Any`: a parsed JSON line mixes strings, numbers and booleans, and `_local_records` converts each.
    """
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.jsonl")):
        day = _day_of(path)
        if day is None or day < first_day:
            continue
        for text in path.read_text(encoding="utf-8").splitlines():
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(line, dict):
                yield line


def _jenkins_records(project: str, jobs: Path, cutoff: datetime) -> Tuple[int, List[_Record]]:
    """Each build of `<project>.local` / `<project>` since the cutoff, from Jenkins' own JUnit record."""
    builds, records = 0, []
    for job in (f"{project}.local", project):
        folder = jobs / job / "builds"
        if not folder.is_dir():
            continue
        for build in sorted(folder.iterdir()):
            junit, info = build / "junitResult.xml", build / "build.xml"
            if not (junit.is_file() and info.is_file()):
                continue
            at = _build_time(info)
            if at is None or at < cutoff:
                continue
            try:
                cases = list(ET.parse(junit).getroot().iter("case"))
            except ET.ParseError:
                continue
            builds += 1
            for case in cases:
                failed = case.find("errorStackTrace") is not None or case.find("errorDetails") is not None
                skipped = (case.findtext("skipped") or "").strip() == "true"
                address = f"{case.findtext('className') or ''}::{case.findtext('testName') or ''}"
                outcome = "failed" if failed else "skipped" if skipped else "passed"
                seconds = float(case.findtext("duration") or 0.0)
                records.append(_Record(address, None, at.date(), at, seconds, outcome, None))
    return builds, records


def _build_time(build_xml: Path) -> Optional[datetime]:
    """The build's start, from `build.xml` (read as text: Jenkins writes XML 1.1, which ElementTree refuses)."""
    text = build_xml.read_text(encoding="utf-8", errors="replace")
    start, stop = text.find("<timestamp>"), text.find("</timestamp>")
    if start < 0 or stop < 0:
        return None
    try:
        return datetime.fromtimestamp(int(text[start + len("<timestamp>") : stop]) / 1000).astimezone()
    except ValueError:
        return None


def _tallies(records: List[_Record]) -> List[Tally]:
    grouped: Dict[str, List[_Record]] = {}
    for record in records:
        grouped.setdefault(record.address, []).append(record)
    tallies = []
    for address, group in grouped.items():
        local = sorted((r for r in group if r.nodeid is not None), key=lambda r: r.at)
        failures = [r for r in group if r.outcome in ("failed", "error")]
        timed = [r.seconds for r in group if r.outcome != "skipped"]
        tallies.append(
            Tally(
                test=str(local[-1].nodeid) if local else address,
                seconds=round(statistics.median(timed), 3) if timed else 0.0,
                runs=len(group),
                failures=len(failures),
                last_failure=max(r.day for r in failures).isoformat() if failures else None,
                slow=local[-1].slow if local else None,
            )
        )
    return tallies


def _duration(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}m {total % 60:02d}s"


def render_report(report: StatsReport) -> str:
    """The report as the terminal shows it."""
    if not report.local_runs and not report.ci_builds:
        return (
            f"No recorded runs of {report.project} in the last {report.days} days. "
            f"Run `pytest` in {report.project}/ to record one."
        )
    budget = _duration(report.budget_seconds)
    lines = [
        f"{report.project} - the last {report.days} days: {report.local_runs} local runs, "
        f"{report.ci_builds} Jenkins builds"
    ]
    if report.fast_run_seconds is None:
        lines.append(f"Fast run: no whole fast run recorded (plain `pytest` in {report.project}/ records one)")
    else:
        assert report.last_fast_run_seconds is not None
        runs = "run" if report.whole_fast_runs == 1 else "runs"
        over = report.fast_run_seconds - report.budget_seconds
        verdict = f"{_duration(over)} over" if over > 0 else "within"
        lines.append(
            f"Fast run: {_duration(report.fast_run_seconds)} (median of {report.whole_fast_runs} whole {runs}; "
            f"last {_duration(report.last_fast_run_seconds)}) - budget {budget}, {verdict}"
        )
    if report.slowest_fast:
        lines += ["", "Slowest fast-run tests:"]
        lines += [f"  {t.seconds:8.1f} s  {t.failures} of {t.runs} failed  {t.test}" for t in report.slowest_fast]
    if report.failed:
        lines += ["", "Tests that failed:"]
        for t in report.failed:
            leg = {True: "slow", False: "fast", None: "ci  "}[t.slow]
            lines.append(f"  {t.failures:3d} of {t.runs:<3d}  last {t.last_failure}  {leg}  {t.test}")
    if report.to_slow:
        lines += ["", f"Move to the slow run (never failed, slowest first) to fit {budget}:"]
        lines += [f"  {t.seconds:8.1f} s  {t.test}" for t in report.to_slow]
    if report.to_fast:
        lines += ["", "Slow tests that failed - move into the fast run:"]
        lines += [f"  {t.failures} of {t.runs} failed  {t.test}" for t in report.to_fast]
    return "\n".join(lines)
