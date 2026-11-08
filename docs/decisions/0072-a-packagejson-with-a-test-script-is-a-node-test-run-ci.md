# A `package.json` with a `test` script is a Node test run CI runs

*2026-10-31*

## Context

Three projects of the workspace this was built in hold a Node package with tests — two web front ends (`vitest run`,
474 and 14 tests) and an editor extension (`node --test`, 15) — and no CI ran any of them: every template ran pytest
only. The extension's tests include one against the project's own Python helper, so its stage needs the project
installed as well as Node.

## Decision

- The declaration is the `package.json` itself: a `scripts.test` that is a non-empty string. aisland adds no
  setting for it, as for a registered `slow` marker and a `playwright` dev dependency. The files are the ones git
  sees in the project (`git ls-files --cached --others --exclude-standard`), so an installed package under
  `node_modules/` or a service's ignored data folder (Jenkins' plugins ship `package.json` files) is no source; a
  project outside git is walked, `node_modules` and hidden folders skipped.
- The stage runs after the unit tests, a folder at a time, with the venv first on its `PATH`. The push pipelines
  install from the lock file (`npm ci`, `npm install` without one); the local pipeline installs nothing, as it tests
  the venv rather than install into it — `npm ci` would also delete and reinstall `node_modules` in the developer's
  checkout every night — and a folder without `node_modules` stops the stage naming the command.
- GitHub gets a `node-tests` job beside `unit-tests` (`needs: [quality-gates]`): the project installed as the
  examples job installs it, then `actions/setup-node@v7` (node24) with Node.js 24, the current LTS.
- The local stage's limit is 10 minutes (`STAGE_LIMITS`), set before any nightly run: the three folders' installs and
  tests took under a minute each on this machine. Raise it there when a nightly run says so.

## Example

```text
$ aisland jenkins scaffold models --force      # models/frontend/package.json: {"scripts": {"test": "vitest run"}}
Jenkinsfile:       stage('Node Tests') { ... dir('frontend') { npm ci; npm test } }
Jenkinsfile.local: stage('Node Tests') { limited('Node Tests', 10) { dir('frontend') { npm test } } }
ci.yml:            node-tests: ... - name: Node Tests (frontend)
```

Rejected: a `[tool.aisland] node-tests = [...]` list. It would be a second place saying what `package.json` already
says, and a folder added later would be untested until someone remembered the list.

---

[The architecture and every decision](../architecture.md#the-decisions)
