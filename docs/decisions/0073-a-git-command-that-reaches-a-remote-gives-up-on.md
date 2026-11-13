# A git command that reaches a remote gives up on one that stops sending

*2026-11-01*

## Context

`gitcmd.git(network=True)` had a 60 s overall limit, but no caller used it: the mirror sync, the wheels prune, the
fetch before an untracked project's branch switch and the fetch before a local source's switch each ran git on their
own, without a limit. A remote that accepts the connection and then sends nothing (a stuck proxy, a VPN that dropped
the route) left `git` waiting forever — measured against a local server that accepts and never answers: still
waiting when killed. An overall limit is wrong for those calls: a first push to a mirror or a prune's force-push can
take many minutes and is healthy.

## Decision

- One guard, in `gitcmd`: `network_options()` — `-c http.lowSpeedLimit=1 -c http.lowSpeedTime=STALL_SECONDS` (60) —
  and `network_env()` — `GIT_TERMINAL_PROMPT=0`. git then gives up after 60 s without a byte (`Operation too slow.
  Less than 1 bytes/sec transferred the last 60 seconds`), whatever the transfer's length.
- `gitcmd.git(network=True)` adds it to its overall limit; the runners whose behaviour differs (`mirrors._git`,
  `wheels.prune._run`) add it with no overall limit; `gitcmd.fetch` is the best-effort fetch with the guard alone.
- git has such a setting over HTTP(S) only. Over SSH a silent server is bounded by ssh's own `ServerAliveInterval`;
  aisland does not set `core.sshCommand`, which is the user's.
- The doctor's probes and the tunnel's sign-in question got limits too (`doctor.PROBE_SECONDS`,
  `tunnel.host.ANSWER_SECONDS`): the MCP server answers through them, and one hung probe stopped every tool.

## Consequences

A dead remote costs a minute and a named error instead of a hung `aisland setup`, `mirror sync` or MCP call. A
transfer that is merely slow (more than one byte a second) is never cut.

## Example

```text
$ git -c http.lowSpeedLimit=1 -c http.lowSpeedTime=3 ls-remote http://127.0.0.1:62698/x.git   # accepts, sends nothing
fatal: unable to access 'http://127.0.0.1:62698/x.git/': Operation too slow. Less than 1 bytes/sec transferred the last 3 seconds
```

Without the two options the same command was still waiting when `timeout 8` killed it.

## What you may change

`STALL_SECONDS`, and which calls take an overall limit on top. Never a bare `subprocess.run(["git", "fetch", …])` for
a remote, and never an overall limit on a push or a prune's fetch.

---

[The architecture and every decision](../architecture.md#the-decisions)
