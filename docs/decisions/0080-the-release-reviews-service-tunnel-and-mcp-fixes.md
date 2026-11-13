# The release review's service, tunnel and MCP fixes

*2026-11-08* (follows "Imports at the top, containers typed, one completion guard")

## Context

The 0.1.0 review of the services, the tunnel, the doctor and the MCP server found what only shows under use: `aisland
jenkins remove ../x` removed a folder outside the Jenkins home; macOS got Linux's VS Code CLI, so `tunnel status`
died with `Exec format error`; a pidfile whose process `ps` could not name was taken for the tunnel and signalled;
the MCP server's 300 s limit ended `colima start` (allowed 900 s) and left its VM half started; the doctor ran its
checks one after the other, past ten minutes in the worst case, while its MCP tool must answer; `aisland doctor` and
`services status` made a `data/<service>/` folder per service; the supervisor's snapshot and the failure count were
module globals under a server that serves tool calls side by side.

## Decision

- A Jenkins job is removed by its folder name alone: a name with a `/`, `..`, or a job folder that links out is
  refused (`jenkins_jobs._job_folder`). The jobs seeded are the workspace's project folders
  (`locations.project_folders`), so an alias link gets no second job.
- The tunnel downloads this system's CLI (`cli-darwin-*`, a `.zip`, on macOS; `cli-alpine-*` on Linux), checked
  against the digest the update service lists, as before; a CLI the system will not run is refused in words. A pid is
  the tunnel only when its command line is read (`/proc/<pid>/cmdline`, else `ps`) and names the CLI. What a pasted
  address names to open must be an absolute path, so it never reads as an option on `code`'s command line.
- A tool's command runs in a session of its own; past its limit — the service's `START_SECONDS` when longer than
  `SERVICE_TIMEOUT`, twice that for a restart — the whole process group is ended.
- The doctor's checks run side by side within one budget, `BUDGET_SECONDS` (`PROBE_SECONDS` + 30); a check past it
  is reported `not checked`. A broken check stays one ✗ (`_guarded`'s `except Exception`, now named among the
  allowed broad excepts).
- The snapshot and the failure count are `ContextVar`s, as the console is; a service's constructor names paths and
  makes nothing — the data folder is made by its first start.
- No code branches on Windows: aisland imports `fcntl` and `termios` and does not run there.

## Consequences

Each is pinned by a test that failed before the change (`tests/test_jenkins_security.py`,
`tests/test_symlinked_folders.py`, `tests/test_tunnel.py`, `tests/test_tunnel_process.py`, `tests/test_server.py`,
`tests/test_doctor.py`, `tests/test_supervisor_snapshot.py`, `tests/test_errors_exit_one.py`,
`tests/test_swiftbar_init.py`). The darwin CLI's digest is the update service's, read at each download, as the
Linux one's always was: nothing is pinned to one VS Code release.
