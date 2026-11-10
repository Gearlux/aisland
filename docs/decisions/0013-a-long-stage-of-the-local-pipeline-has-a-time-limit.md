# A long stage of the local pipeline has a time limit, and past it the stage fails

*2026-10-15*

## Context

The local orchestrator builds every project at 01:00 and waits for each build (`build job:`,
`wait: true`); nothing in the orchestrator or the project pipelines bounded that wait. On
2026-10-14 the Slow Tests stage of one project's `.local` #13 ran 518.6 min (41 392 tests whose
own clocks summed to 11 min; pytest's clock said 8:38:30) and passed; on 2026-10-15 the Unit Tests
stage of `labels.local` #37 waited 108.9 min on a test that waits for a SIGINT Jenkins' background
shell ignores, until a hand `kill` ended pytest (`Terminated: 15`, exit 143). Both held
`acme.local` #36 "building" from 01:00 into the afternoon, and the morning report could only say
"still building". The stage durations of the six 01:00 runs before (read from each build's
pipeline-graph record) were small and steady: Unit Tests at most 6.0 min (canvas), Slow Tests
at most 29.6 min, Verify Examples 0.5 min, Verify Notebooks 1.4 min, Initialize and
the quality gates 0.2 min. The 35-48 min builds of some projects were executor queueing
inside the build, not stage time.

A second finding from #37: pytest killed before it writes `--junitxml` leaves the previous build's
`test-report.xml` in the checkout (the local job's workspace), and the stage's `junit` post step
recorded #36's 2 859 passed cases for #37.

## Decision

- Each stage of `Jenkinsfile.local` that runs the project's own code runs inside a time limit:
  `STAGE_LIMITS` in `ci_scaffold.py` (Unit Tests 15, Slow Tests 60, Verify Examples 15, Verify
  Notebooks 15 min — about twice the longest healthy run measured). One table, every project the
  same; Initialize and the quality gates run tools, not the project's code, and get none. The wait
  for an executor is not counted: the node is held before the first stage.
- Past its limit the stage FAILS, naming itself and the limit (`TIMED_OUT_MESSAGE`), rather than
  ABORTING with Jenkins' own wording. Measured on two probe jobs with a 1 min limit around a shell
  ignoring SIGINT: a per-stage `timeout` caught and turned into `error(...)` ends the stage FAILED
  at 80 s, the build FAILURE, the stage's post block run, the next stages skipped, the console
  ending with the one line; the alternative, one whole-build `options { timeout }`, ends ABORTED
  with `Timeout has been exceeded`, names no stage and no limit, skips the pipeline's `failure`
  block, and would have the morning report parse Jenkins' wording and the orchestrator's
  `catchError` meet a result it was never measured with. FAILURE is the path every failing project
  already takes through the orchestrator (`<job> completed: FAILURE`) and through the morning report.
- The helper `limited(stage, minutes, body)` is defined in the generated file above `pipeline {`
  (the template `templates/ci/_limited.groovy.jinja`; a Python string, `_LIMITED_HELPER`, until the CI templates of
  2026-10-30) and rethrows a `FlowInterruptedException` that arrives before the limit —
  Abort clicked by hand stays an abort. It needs no sandbox approval: `System.currentTimeMillis`
  and `Closure.call` are in script-security's generic whitelist, and a catch clause's class name
  is never checked.
- Each pytest step first removes the files it writes (`rm -f test-report.xml coverage.xml`,
  `rm -f slow-test-report.xml`), in both Jenkins templates: both workspaces persist between builds.
  A timed-out stage then has no report, the `junit` step records nothing, and the morning report's
  console path finds the timed-out line (`nightly._TIMED_OUT`) and quotes it.
- The push pipelines (`Jenkinsfile`, `ci.yml`) get no limit here: a runner's limits are measured
  on the runner (`TASKS.md`), and GitHub bounds a job at six hours anyway.

Rejected: bounding the orchestrator's `build job:` wait instead. The project build would keep
running, holding an executor for the rest of the night, and its own record would say nothing.

## Consequences

- The hung process may outlive the stage: in both probes Jenkins printed
  `After 20s process did not stop` and went on, and the `sleep` was alive minutes later. The
  orchestrator is unblocked; the process is the test's own bug to fix.
- A project whose healthy run grows past its limit fails its nightly build with the line above
  until the number in `STAGE_LIMITS` is raised and every project re-scaffolded — the morning
  report names it, so it is never silent.
- `aisland tests nightly` says `labels (timed out after 15 min)` for such a build, and its
  markdown quotes the line under the project's heading.

## Example

```groovy
// <project>/Jenkinsfile.local — the helper, and one stage inside its limit
def limited(String stage, int minutes, Closure body) {
    long started = System.currentTimeMillis()
    try {
        timeout(time: minutes, unit: 'MINUTES') {
            body()
        }
    } catch (org.jenkinsci.plugins.workflow.steps.FlowInterruptedException interrupted) {
        if (System.currentTimeMillis() - started < minutes * 60000L) {
            throw interrupted
        }
        error("${stage} timed out after ${minutes} min - its limit in Jenkinsfile.local (STAGE_LIMITS in aisland/ci/ci_scaffold.py)")
    }
}
...
        stage('Slow Tests') {
            steps {
                limited('Slow Tests', 60) {
                    sh 'rm -f slow-test-report.xml'
                    script { ... the pytest call ... }
                }
            }
```

```text
Timeout set to expire in 1 hr 0 min
+ /home/you/acme/.venv/bin/pytest tests -m slow --junitxml=slow-test-report.xml
...
Cancelling nested steps due to timeout
Sending interrupt signal to process
After 20s process did not stop
[Pipeline] error
...
ERROR: Slow Tests timed out after 60 min - its limit in Jenkinsfile.local (STAGE_LIMITS in aisland/ci/ci_scaffold.py)
Finished: FAILURE
```

## What you may change

- The numbers in `STAGE_LIMITS`, and which stages carry one, as long as the table stays the one
  source (`tests/test_jenkins_scaffold.py::TestALongStageHasATimeLimit` reads it) and the
  generated files are regenerated with it.
- The wording of `TIMED_OUT_MESSAGE`, together with `nightly._TIMED_OUT`
  (`tests/test_nightly.py::test_the_line_the_pipeline_writes_is_the_line_the_report_reads`).
- What must not change: a timed-out stage FAILS (an abort would leave the orchestrator's and the
  report's paths unmeasured), an abort before the limit is rethrown, and every pytest step removes
  its report files first.

**Amendment (2026-10-28, chosen: three times the table): GitHub's jobs get limits too.** The push pipelines had
none, waiting for a runner's durations to measure; until then a hung job held a GitHub runner for its six-hour
default. `ci.yml`'s jobs that run the project's code now get `RUNNER_LIMIT_FACTOR` × their stage's limit — unit tests
45, slow tests 180, examples 45, notebooks 45 minutes — from the same table, so a change to `STAGE_LIMITS` moves both;
the quality gates still get none. `release.yml` (hand-written) limits its own jobs: tests 45, build 15, publish 10. The
push `Jenkinsfile` stays unlimited: it runs on a Jenkins nobody has measured.

```python
RUNNER_LIMIT_FACTOR = 3
runner_limit("Slow Tests")      # 180 → `timeout-minutes: 180` on the slow-tests job
```

---

[The architecture and every decision](../architecture.md#the-decisions)
