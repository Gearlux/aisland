"""`aisland tests report <project>`: the recorded test runs read back beside the Jenkins builds' JUnit results.

The pytest plugin (:mod:`aisland.pytest_stats`) appends each run of a workspace project to the project's day files;
this report merges those lines with the local Jenkins builds' JUnit results and answers what the workspace's two-run
rule asks: the fast run's time against the budget, the slowest fast tests, the tests that failed, what to move to the
slow run and what back into the fast run. It loads only when the report is asked for, never in a pytest start, so it
is free to import the rest of aisland; it shares the plugin's data folder, record switch and day files.
"""

from __future__ import annotations

import ast
import json
import statistics
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

from aisland import locations, paths, pytest_stats

#: The fast run's budget: about five minutes per project.
FAST_RUN_BUDGET_SECONDS = 300.0
#: How far back the report reads by default.
DEFAULT_WINDOW_DAYS = 30
#: How many of the slowest fast-run tests the report lists.
SLOWEST_SHOWN = 10
#: How many of the latest whole fast runs say how many tests the fast run runs at once (`_parallel`).
PARALLEL_RUNS = 5
#: How many of the latest whole fast runs the fast run's time is the median of: older runs ran a fast run that was
#: another set of tests (before the moves the report itself asked for).
BASELINE_RUNS = 5
#: Exit statuses of a run that ran to its end (0 all passed, 1 some failed); an interrupted run says nothing of time.
_COMPLETE_EXITS = (0, 1)


def jenkins_jobs_folder() -> Path:
    """The local Jenkins' job folders (`<project>.local`, `<project>`), each build's JUnit record in it."""
    return paths.data_dir() / "jenkins" / "jobs"


def junit_address(nodeid: str) -> str:
    """The `<classname>::<name>` pytest's JUnit report gives a nodeid — how CI's records name the same test.

    `tests/test_a.py::TestX::test_y[1]` -> `tests.test_a.TestX::test_y[1]` (pytest's `junitxml.mangle_test_address`).
    """
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = names[0].replace("/", ".").removesuffix(".py")
    names[-1] += bracket + params
    return ".".join(names[:-1]) + "::" + names[-1]


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
    recording: bool = True  # whether local runs are recorded (`aisland workspace test-stats on|off`)
    recorded_by: pytest_stats.RecordedBy = "default"  # who decided it
    switch_name: str = ""  # the project's name in workspace.toml, which `aisland workspace test-stats` takes
    parallel: float = 1.0  # how many tests the whole fast runs ran at once: their tests' seconds over their wall time
    uncovered_seconds: float = 0.0  # wall seconds of the overrun the never-failed tests cannot cover

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
    test_seconds: float = 0.0  # its tests' seconds added up


def build_report(
    project: str,
    days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
    stats_dir: Optional[Path] = None,
    jenkins_jobs_dir: Optional[Path] = None,
    project_dir: Optional[Path] = None,
) -> StatsReport:
    """Read the last `days` of PROJECT's local runs and Jenkins builds and say what the two-run rule needs.

    The slowest tests and the moves name only tests the project still has (:class:`_Present`, in ``project_dir``,
    default ``<workspace>/PROJECT``): a test deleted or renamed keeps its records for the window, and a move proposed
    for it would be one nobody can make. The failures keep every test, gone or not — they are history."""
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=days)
    runs, local = _local_records((stats_dir or pytest_stats.stats_folder()) / project, cutoff)
    builds, ci = _jenkins_records(project, jenkins_jobs_dir or jenkins_jobs_folder(), cutoff)
    tallies = _tallies(local + ci)
    switch = pytest_stats.recording(locations.workspace_root(), project)

    fast = sorted(
        (r for r in runs if r.leg == "fast" and r.whole and r.exit in _COMPLETE_EXITS),
        key=lambda r: r.at,
    )
    fast_seconds = statistics.median(r.seconds for r in fast[-BASELINE_RUNS:]) if fast else None
    folder = project_dir or locations.workspace_root() / project
    present = _Present(folder)
    ran_last = {r.nodeid for r in local if fast and r.at == fast[-1].at}
    tallies = [
        (
            replace(t, slow=True)
            if t.slow is False and (present.marked_slow(t.test) or _left_the_fast_run(t.test, ran_last, present))
            else t
        )
        for t in tallies
    ]
    by_seconds = sorted((t for t in tallies if present(t.test)), key=lambda t: (-t.seconds, t.test))
    parallel = _parallel(fast)
    to_slow, uncovered = _to_slow(by_seconds, fast_seconds, parallel)
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
        to_slow=to_slow,
        to_fast=[t for t in failed if t.slow is True and present(t.test)],
        recording=switch.on,
        recorded_by=switch.by,
        switch_name=switch.project,
        parallel=parallel,
        uncovered_seconds=uncovered,
    )


class _Present:
    """Whether a recorded test is still in the project, read from its file without importing it.

    A test is gone when its file is, or when the file parses and no longer defines it: its function (a parametrized
    id's name before ``[``), within the classes its nodeid names, a method also found on a base class defined in the
    same file. Whatever cannot be told counts as present — a JUnit address (no ``.py`` file), a project folder that is
    not here, a file that does not parse, a class whose base is defined elsewhere, a test assigned by name
    (``test_x = make()``) — so the report never hides a test it cannot read."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.here = project_dir.is_dir()
        self.trees: Dict[Path, Optional[ast.Module]] = {}

    def __call__(self, test: str) -> bool:
        path, _, rest = test.partition("::")
        if not self.here or not rest or not path.endswith(".py"):
            return True
        file = self.project_dir / path
        if not file.is_file():
            return False
        tree = self._tree(file)
        if tree is None:
            return True
        *classes, name = rest.split("::")
        module = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        scope: Union[ast.Module, ast.ClassDef] = tree
        for class_name in classes:
            found = next((n for n in scope.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
            if found is None:
                return False
            scope = found
        return _defines(scope, name.split("[", 1)[0], module)

    def marked_slow(self, test: str) -> bool:
        """Whether the test's file marks it ``slow`` now — on the function, on a class it is in, or in a ``pytestmark``
        of the module or of such a class. Only a mark found counts: a test whose file cannot be read, or that a hook
        or a parametrized case marks, keeps the mark its last run recorded."""
        path, _, rest = test.partition("::")
        if not self.here or not rest or not path.endswith(".py"):
            return False
        file = self.project_dir / path
        tree = self._tree(file) if file.is_file() else None
        if tree is None:
            return False
        *classes, name = rest.split("::")
        if _carries_slow(tree.body, []):
            return True
        scope: Union[ast.Module, ast.ClassDef] = tree
        for class_name in classes:
            found = next((n for n in scope.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
            if found is None:
                return False
            if _carries_slow(found.body, found.decorator_list):
                return True
            scope = found
        function = next(
            (
                n
                for n in scope.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name.split("[", 1)[0]
            ),
            None,
        )
        return function is not None and any(_is_slow(d) for d in function.decorator_list)

    def _tree(self, file: Path) -> Optional[ast.Module]:
        if file not in self.trees:
            try:
                self.trees[file] = ast.parse(file.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, ValueError):
                self.trees[file] = None
        return self.trees[file]


def _left_the_fast_run(test: str, ran_last: Set[Optional[str]], present: "_Present") -> bool:
    """Whether a test the project still has, and an earlier fast run ran, is absent from the latest whole fast run:
    something now keeps it out — a mark on one parametrized case, a hook, a deselect — whatever its file shows by
    name. Nothing is claimed while the latest run recorded no test."""
    return bool(ran_last) and test not in ran_last and present(test)


def _is_slow(node: ast.expr) -> bool:
    """Whether a decorator or a mark in a list is ``pytest.mark.slow`` (or ``mark.slow``, imported from pytest)."""
    return ast.unparse(node) in ("pytest.mark.slow", "mark.slow")


def _carries_slow(body: List[ast.stmt], decorators: List[ast.expr]) -> bool:
    """Whether a class's decorators, or a ``pytestmark`` in a module's or a class's body, mark ``slow``."""
    if any(_is_slow(d) for d in decorators):
        return True
    for node in body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            marks = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            if any(_is_slow(m) for m in marks):
                return True
    return False


def _defines(scope: Union[ast.Module, ast.ClassDef], name: str, module: Dict[str, ast.ClassDef]) -> bool:
    """Whether ``scope`` defines ``name`` — or may: through a base class defined elsewhere."""
    for node in scope.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                return True
    if isinstance(scope, ast.ClassDef):
        for base in scope.bases:
            if not isinstance(base, ast.Name) or base.id not in module:
                return True
            if module[base.id] is not scope and _defines(module[base.id], name, module):
                return True
    return False


def _parallel(fast: List[_Run]) -> float:
    """How many tests the fast run runs at once now: over the latest :data:`PARALLEL_RUNS` whole fast runs, the median
    of each run's tests' seconds over its wall time, never below 1 (a run in one process spends some time outside its
    tests). Only the latest: a project that went parallel mid-window ran in one process before. ``fast`` is oldest
    first; a run that recorded no test counts not."""
    timed = [r for r in fast if r.seconds > 0 and r.test_seconds > 0][-PARALLEL_RUNS:]
    ratios = [r.test_seconds / r.seconds for r in timed]
    return round(max(1.0, statistics.median(ratios)), 2) if ratios else 1.0


def _to_slow(
    by_seconds: List[Tally], fast_seconds: Optional[float], parallel: float = 1.0
) -> Tuple[List[Tally], float]:
    """The never-failed fast tests, slowest first, until their seconds cover the fast run's overrun — and the wall
    seconds of it they cannot cover. A run of ``parallel`` tests at once loses a wall second for every ``parallel``
    seconds of tests moved, so the overrun is weighed in test time."""
    if fast_seconds is None or fast_seconds <= FAST_RUN_BUDGET_SECONDS:
        return [], 0.0
    overrun, moved = (fast_seconds - FAST_RUN_BUDGET_SECONDS) * parallel, []
    for tally in by_seconds:
        if overrun <= 0:
            break
        if tally.slow is False and not tally.failures:
            moved.append(tally)
            overrun -= tally.seconds
    return moved, round(max(0.0, overrun) / parallel, 3)


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
        found.test_seconds += seconds
        records.append(_Record(junit_address(nodeid), nodeid, found.at.date(), found.at, seconds, outcome, slow))
    return list(runs.values()), records


def _lines(folder: Path, first_day: date) -> Iterator[Dict[str, Any]]:  # Any: see the docstring
    """The JSON lines of the day files from `first_day` on; a damaged line (a run killed mid-write) is skipped.

    Values are `Any`: a parsed JSON line mixes strings, numbers and booleans, and `_local_records` converts each.
    """
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.jsonl")):
        day = pytest_stats.day_of(path)
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


def _not_recorded(report: StatsReport) -> str:
    """The line saying ``report.project``'s runs are not recorded, and the command that records them again."""
    here = " --machine-only" if report.recorded_by == "this machine" else ""
    return (
        f"Test runs of {report.project} are not recorded: "
        f"`aisland workspace test-stats on {report.switch_name or report.project}{here}` records them again."
    )


def render_report(report: StatsReport) -> str:
    """The report as the terminal shows it."""
    if not report.local_runs and not report.ci_builds:
        if not report.recording:
            return f"No recorded runs of {report.project} in the last {report.days} days. {_not_recorded(report)}"
        return (
            f"No recorded runs of {report.project} in the last {report.days} days. "
            f"Run `pytest` in {report.project}/ to record one."
        )
    budget = _duration(report.budget_seconds)
    lines = [
        f"{report.project} - the last {report.days} days: {report.local_runs} local runs, "
        f"{report.ci_builds} Jenkins builds"
    ]
    if not report.recording:
        lines.append(_not_recorded(report))
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
        if report.parallel > 1:
            lines.append(
                f"  (the fast run runs about {report.parallel:.1f} tests at once: each second over is "
                f"{report.parallel:.1f} s of tests)"
            )
    if report.uncovered_seconds > 0:
        lines.append(
            f"  Even moving all of them leaves the fast run {_duration(report.uncovered_seconds)} over: every other "
            "fast test has failed at least once."
        )
    if report.to_fast:
        lines += ["", "Slow tests that failed - move into the fast run:"]
        lines += [f"  {t.failures} of {t.runs} failed  {t.test}" for t in report.to_fast]
    return "\n".join(lines)
