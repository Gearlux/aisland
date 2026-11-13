# Every workspace gets aisland's agent commands and rules (2026-10-20)

## Context

The workspace commands (`/commit-all`, `/commit-session`, `/list-actions`, `/pull-all`, `/push-all`, `/ship`) and the
rules agents follow lived in ONE workspace's root: its `.agents/workflows/` and its `AGENTS.md`. The commands already
depended only on aisland (`git pull-all`, the mirror sync, the install order), and most of the rules — evidence
before claims, agree the scope first, commit only on request, sessions coordinate before they edit — had nothing to
do with that workspace's projects. Another aisland workspace started with neither, so its agents behaved
differently, and every improvement had to be copied by hand.

## Decision

aisland ships the shared part in `aisland/harness/` and installs it into every workspace (`harness.install_harness`,
from `aisland setup` and `aisland workspace init`):

- **What:** `commands/*.md` → `.agents/workflows/` (`.claude/commands` links there), `scripts/*.py` →
  `.agents/scripts/`, `AGENTS.md` → `.agents/aisland/AGENTS.md`. Only what holds in any workspace; a test refuses
  any project, private or domain name.
- **How the rules reach an agent:** the workspace's own `AGENTS.md` starts with `@.agents/aisland/AGENTS.md` — Claude
  Code and Gemini CLI follow `@` imports, relative to the importing file — and keeps only its own rules below.
  Rejected: aisland writing the rules into a marked block of the workspace's `AGENTS.md`. Every agent would read it,
  including ones without imports, but the file would be half aisland's: an edit inside the block lost on the next
  setup, and no way to make the rules a link into a checkout.
- **Link or copy:** from a checkout each file is a relative link into the package, so an edit made where an agent
  reads it is an edit of aisland's file, reviewed and committed in aisland. From a wheel a link would point into
  `site-packages`, which a reinstall replaces, so each file is a copy.
- **A copy someone edited is kept** (chosen 2026-10-20): `installed.json` records each copy's fingerprint; a
  copy still equal to it (or to aisland's file) is replaced, anything else is reported and left. Rejected:
  overwriting with a report — it destroys the edit it reports.
- **Never the workspace's own files:** a command aisland does not ship is not touched; each installed file is
  ignored by its own path, not by its folder, so the workspace's own commands stay committable; an existing
  `AGENTS.md` is never edited — setup names the missing import line instead.

## Consequences

- A rule or command changes in one place for every workspace; a workspace on a wheel takes it with the next aisland
  release and `aisland setup`.
- An agent that reads `AGENTS.md` without following imports sees the import line, not the rules.
- Claude Code reads `.claude/commands/` of the folder a session starts in only (measured 2026-10-20: started in a
  project folder it listed none of the six), so every project folder gets the same link, kept out of git through its
  repository's `.git/info/exclude` — a line in each project's tracked `.gitignore` would change every project.
- A command dropped from aisland disappears on the next setup: its link is removed, an unedited copy deleted, an
  edited copy kept and named.

## Example

```python
from pathlib import Path
from aisland import harness

harness.install_harness(Path("~/work/acme").expanduser(), link=False)
# []  — or, after someone edited a copy:
# ["kept your edited .agents/workflows/ship.md — delete it to take aisland's"]
[name for _source, name in harness.shipped_files()]
# ['.agents/workflows/commit-all.md', ..., '.agents/scripts/stage_hunks.py', '.agents/aisland/AGENTS.md']
```

## What you may change

- A rule or a command, in `aisland/harness/` (never an installed copy); a command's citations and the rule's bold
  heading together (`tests/test_harness.py::test_every_section_a_command_cites_is_in_the_rules`).
- A new kind of shipped file: a folder under `harness/`, its pattern in `[tool.setuptools.package-data]`, its target in
  `shipped_files()`.
- Never: editing a workspace's `AGENTS.md`, overwriting an edited copy, ignoring `.agents/workflows/` as a folder.

---

[The architecture and every decision](../architecture.md#the-decisions)
