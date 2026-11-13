# The local pipeline tests the venv, it does not install

*2026-09-13*

## Context

`aisland jenkins scaffold` emits a `Jenkinsfile.local` per project, seeded into the local Jenkins
as `<project>.local` and run inside the workspace checkout against the shared `.venv`. Its
`Initialize` stage used to install into that venv: every internal dep editable with `--no-deps`,
then the project's `dev` extra (plus selected framework/tracker extras), then its `notebook`
extra, then `jupyter nbconvert`. The venv those installs targeted is the one `aisland setup`
provisions, where every workspace project is already editable-installed from its own checkout —
so the installs changed nothing, but each one re-resolved the full dependency set against the
configured indexes. Measured on 2026-09-13 on a slow VPN link: compiler's `Initialize` took
1430 s (`Resolved 82 packages in 11m 50s`, then 696 s more for the notebook extra) to reinstall
only compiler itself, datasets's 830 s and app's 574 s, while the quality gates and tests
that followed took about 12 s. The installs also serialised builds on the venv's exclusive lock
(`Waiting to acquire exclusive lock for …/.venv`).

A second queue sat in front of that one. The `<workspace>.local` orchestrator declared
`agent { node { label 'built-in' } }`, holding one of the built-in node's two executors for its
whole run although it only triggers jobs. Each "parallel" phase therefore ran its projects one
at a time: app waited 875 s for datasets, and labels and cleaner showed
`Waiting for next available executor` behind compiler.

## Decision

- `Jenkinsfile.local` installs nothing. Installing is `aisland setup`'s job; the local pipeline
  runs the gates of a checkout against the venv exactly as setup left it.
- Its `Initialize` stage keeps one check: the project's distribution in the venv must record THIS
  checkout as its source (`direct_url.json`, written for every editable install), else the build
  stops naming `aisland setup`. It reads the distribution rather than looking up the package, for
  two reasons: nothing is imported, and a project may ship its code inside another project's
  package namespace (`detector-yolo` installs `detector/yolo/`), where a package lookup
  would resolve a sibling's checkout. It runs `python3 -P` — Jenkins runs inside the checkout,
  and without `-P` the working directory is first on `sys.path`, so a stale `<name>.egg-info`
  left there shadows the venv's record and a correctly-installed project fails (measured on
  such a child project: exit 1 without `-P`, exit 0 with it).
- The orchestrator declares `agent none`; `build job:` needs no node.
- The runner files (`Jenkinsfile`, `ci.yml`) are unchanged: a runner has no workspace venv.

Rejected: speeding the installs up (resolving only a private package from the corporate index,
or caching harder). It keeps two owners of the venv and still pays a resolve per build for
nothing.

## Consequences

- A project that gains a dependency fails its local build with `ModuleNotFoundError` until
  `aisland setup` has run. That is the intended signal; the pipeline never installs it.
- `Jenkinsfile.local` carries no dependency list and reads no source or selection configuration,
  so no `aisland workspace configure` change can rewrite it.
- With installs gone nothing holds the venv lock, and with the orchestrator off the executors a
  phase's builds run in parallel, bounded by the node's executor count.

## Example

```groovy
// <project>/Jenkinsfile.local — the whole Initialize stage
stage('Initialize') {
    steps {
        echo 'Validating Environment...'
        sh "${VENV_BIN}/python3 --version"
        sh """${VENV_BIN}/python3 -P - 'compiler' "${env.WORKSPACE_ROOT}/compiler" <<'PY'
import importlib.metadata, json, pathlib, sys
dist, checkout = sys.argv[1], pathlib.Path(sys.argv[2]).resolve()
try:
    raw = importlib.metadata.distribution(dist).read_text("direct_url.json")
    found = json.loads(raw)["url"] if raw else "an install with no source URL"
except importlib.metadata.PackageNotFoundError:
    found = "not installed"
if found != checkout.as_uri():
    sys.exit(f"{dist} is not installed from {checkout} (found: {found}) - run `aisland setup`")
print(f"{dist} -> {checkout}")
PY"""
    }
}
```

```text
compiler -> /home/you/acme/compiler                                                                              # exit 0
compiler is not installed from /home/you/acme/compiler (found: file:///Users/you/other/compiler) - run `aisland setup`   # exit 1
```

## What you may change

- The check itself (what counts as "this checkout"), as long as it imports nothing, keys on the
  distribution rather than a package name, and does not let the working directory decide the
  answer — the pins in `tests/test_jenkins_scaffold.py::TestTheLocalPipelineInstallsNothing` run
  the emitted shell from inside a checkout carrying a stale `egg-info` for exactly that reason.
- The orchestrator's phase layout. What must not change: it holds no executor
  (`tests/test_orchestrator_executor.py`), and `Jenkinsfile.local` gains no install step.

---

[The architecture and every decision](../architecture.md#the-decisions)
