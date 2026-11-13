# A recurring environment skew becomes a doctor check, not a note

*2026-07-27; widened to the Docker layer 2026-08-31 and to Jenkins' Java 2026-10-07*

## Context

The workspace `.venv` is shared and built incrementally, so one install can leave a package
pair inconsistent: both OpenCV wheels installed (they own the same `cv2/` files, so their native
libraries mix), `huggingface_hub` left below what `transformers` imports, an MCP SDK whose server
module was renamed between majors. The Docker layer drifts the same way: a `credsStore` entry
naming a credential helper that was uninstalled, and a Colima VM whose trust store lacks the CA
of a TLS-intercepting proxy the host already trusts. Each failure surfaces far from its cause — a
`dlopen` error during pytest collection, an `ImportError` three libraries deep, every
`docker pull` dying on credentials it does not need — and each had been written up as a note in
an `AGENTS.md`. A note does not run, so every recurrence was diagnosed again from the start.

## Decision

`aisland doctor` runs the tuple `CHECKS` of `check_*` functions. Each returns
`CheckResult(name, ok, detail, remedy)`; the command exits 1 when any check failed, so a CI step
can gate on it.

- **A check is specific.** It names one known skew and carries the copy-pasteable command that
  fixes it; the test suite asserts every failing result has a remedy.
- **Imports run in a subprocess** (`_import_probe`). A broken native bundle can abort the
  interpreter, and a doctor that dies while diagnosing is worse than none.
- **A check mirrors an invariant enforced elsewhere, and names it.** The OpenCV check mirrors the
  `opencv-python ; sys_platform == 'never'` override in `uv_config._ROOT_UV_BASE`, so the doctor
  cannot grow a second policy that drifts from the first.
- **A probe that cannot run passes with a note**: no Colima installed, a stopped VM (the doctor
  never boots one), an unparseable config, a network error. This is the rule
  `supervisor._preflight_full_disk_access` already follows: an inconclusive probe must never cry
  wolf, or the operator learns to ignore the tool.
- **VM state is `DockerService.is_running()`** (the exit code of `colima status`), never a second
  parse of colima's output.

Rejected: a generic venv linter. `pip check` already lists lines that are expected here — a
library pinning one exact OpenCV build, libraries asking for the GUI wheel the workspace drops on
purpose — and a doctor that reported them would fail on every healthy machine.

## Consequences

- A recurrence costs one command and one fix line instead of a forensic session.
- A laptop that does not run Docker passes the preflight.
- The `colima-tls` remedy stays manual (decided 2026-08-31). Installing the network's CA
  automatically at `aisland docker start` would silently trust whatever intercepts TLS on any
  network the machine joins, to save a rare one-command fix. The CA install does not survive
  `colima delete`, which is why the check exists at all.
- Each check costs a subprocess or a file read, so the whole run stays a preflight rather than a
  build step.

## Example

```python
# aisland/doctor.py — a new check is one function plus one tuple entry
CHECKS: Tuple[Callable[[], CheckResult], ...] = (
    check_opencv_single_variant,
    check_huggingface_hub_floor,
    check_mcp_server_api,
    check_docker_credsstore,
    check_colima_vm_trust,
    check_jenkins_java,      # see "Jenkins runs on the Java its WAR accepts, by full path"
    check_torch_extension_lock,
    check_tunnel_relay,
    check_tunnel_connect,
    check_pylance_memory,
)
```

```text
# the opencv check's report when both OpenCV wheels are installed (exit 1)
  ✗ opencv           opencv-python=4.12.0.88 is installed; the workspace override drops it and keeps opencv-python-headless=4.13.0.92 — both own cv2/, so .dylibs will mix
      fix: uv pip uninstall opencv-python && uv pip install --force-reinstall opencv-python-headless   # use `uv pip`, not `pip` — pip does not read uv.toml's overrides
```

## What you may change

- Add a check: a `check_*` returning `CheckResult`, appended to `CHECKS`, with tests for its
  failing, passing and inconclusive branches in `tests/test_doctor.py`.
- The report's wording and order.
- What must not change: a failing result carries a remedy; imports stay out of process; an
  inconclusive probe passes; the doctor only reads — it never changes the environment and never
  boots a VM.

---

## `torch-ext-lock` judges this venv's torch (2026-10-25)

**Context.** The check failed on any `lock` older than two minutes in any Python's folder of torch's extension
cache. torch up to 2.13 serialises a JIT build with `FileBaton`, a lock file whose existence blocks; torch 2.14
holds `filelock`'s OS lock instead and leaves the file behind after every build. With the workspace's venv on
2.14.1 the check failed on every healthy build — first on a `py312` lock another venv (also 2.14.1) had left.

**Decision.** Read this venv's torch from its metadata. From 2.14.1 (the first version measured: a build killed
mid-compile left the next import waiting over 240 s on 2.13.0 and loading in 43 s on 2.14.1) the check passes
saying why; below it, it keeps its old rule, in this Python's own folder only.

**Consequences.** No false alarm with a current torch; an old torch is still caught. A venv of another Python is
not judged by this one's torch.

**Example.**

```
✓ torch-ext-lock   torch 2.14.1 holds an OS lock while it builds — a killed build leaves no lock that blocks
```

**What you may change.** Lower `_TORCH_OS_LOCK_SINCE` to 2.14.0 once a 2.14.0 build is measured the same way.

---

## Pylance runs out of memory: the root excludes its projects, the doctor reads the log (2026-10-30)

**Context.** In a workspace of 28 projects the generated workspace file has 30 folders: the root, every project,
the log folder. Pylance starts one analysis per folder, and the root's covered every project again (2,721 files,
past its 2,000-file index limit). It runs on VS Code's own Node.js, whose heap is about 1 GB: it died with
`JavaScript heap out of memory` while first reading the folders, VS Code restarted it, it died again — seven times
in a morning, every 25 s at worst — and nothing on screen said why; completion and symbol search were simply gone.
Excluding the projects from the root alone did not stop it (two more crashes); a Node.js of Pylance's own
(`python.analysis.nodeExecutable: auto`, an 8 GB heap by its default `--max-old-space-size=8192`) did. Pylance
declares that setting, and `nodeArguments`, `"scope": "machine"`: VS Code reads it only from User settings.

**Decision.** The root `pyrightconfig.json` excludes every project inside the workspace (each is a folder of its
own; imports still resolve through `extraPaths`). The fix aisland cannot apply is written where the user meets the
problem: the workspace file's header gives the steps, and `aisland doctor`'s `pylance-memory` reads the newest
language-server log of a window opened on the workspace and fails with the line to add. aisland never writes the
user's VS Code settings.

**Consequences.** Each file is analysed once. A window opened on the bare root folder indexes no project. The check
reads VS Code's local logs, so a remote machine passes with a note; a crash after which Pylance started on its own
Node.js passes, saying when.

**Example.**

```
✗ pylance-memory   Pylance ran out of memory 7 times, the last at 2026-10-30 12:06:49 (…/window9/exthost/ms-python.python/Python Language Server.log) — no completion or symbol search while it restarts
    fix: add "python.analysis.nodeExecutable": "auto" to your VS Code User settings (Cmd/Ctrl+Shift+P → "Preferences: Open User Settings (JSON)"), then "Developer: Reload Window"
```

**What you may change.** The log lines the check matches (`_PYLANCE_OUT_OF_MEMORY`, `_PYLANCE_OWN_NODE`) when
Pylance changes its wording; the 16 GB of the second remedy.

---

[The architecture and every decision](../architecture.md#the-decisions)
