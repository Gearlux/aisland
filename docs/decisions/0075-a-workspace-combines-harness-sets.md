# A workspace combines harness sets

*2026-11-02* (extends "Every workspace gets aisland's agent commands and rules")

## Context

Every workspace got aisland's own commands and rules, with no way to drop them, bring a team's own, or use the
harness another project already keeps (its `AGENTS.md`, its `.claude/commands`, its Cursor rules). A workspace's own
rules could only be appended below the import line of aisland's.

## Decision

- `workspace.toml` lists **harness sets** in order (`[harness] sets`), aisland's own when it lists none: `"aisland"`,
  a project of the workspace (the folder its `[tool.aisland] harness` names, else the project itself), a folder of the
  workspace, a name an installed package registers (`aisland.harnesses`, shaped like `aisland.ci_templates`), or
  `{ git = <url>, ref = <ref> }` — cloned into the state folder, fetched again only when the url or ref changes.
- A set says what it brings in `harness.toml` (`rules`, `commands`, `scripts`, `[files]`); a folder without one is
  read where agents read (`AGENTS.md`/`CLAUDE.md`, `.claude/commands/*.md` that is a real folder — the link aisland
  makes is the workspace's, never the set's —, `.cursor/rules/*`, `.github/copilot-instructions.md`). aisland's own
  set has a `harness.toml` too, so `aisland workspace harness export` gives a set that works on its own.
- Each set's rules land in `.agents/<set>/AGENTS.md`; `.agents/rules.md` imports them in the list's order, and the
  workspace's `AGENTS.md` imports that one file — adding or dropping a set never touches it. aisland's own rules stay
  in `.agents/aisland/AGENTS.md`, so an `AGENTS.md` importing them still works; setup names the new line.
- Two sets placing one file are **refused** before anything is installed, naming both and the line; `exclude` on an
  entry leaves a file out, and an `exclude` that names nothing is refused. No set wins by its place in the list: a
  silently replaced `/ship` would be a surprise, and a precedence rule is one more thing to learn.
- The record (`.agents/installed.json`) holds each copy's fingerprint and each link, so the files of a dropped set
  are found and taken away; an edited copy is kept and named, as before.

## Consequences

A workspace with no `[harness]` installs what it did, plus `.agents/rules.md`. A git set makes setup use the network
the first time and when its ref changes, and an unreachable host stops setup naming the line. The commands aisland
ships cite aisland's rules; a workspace that drops aisland's rules keeps those commands at its own risk.

## Example

```toml
[harness]
sets = [{ set = "aisland", exclude = ["commands/ship.md"] }, "models", { git = "https://github.com/acme/rules", ref = "v2" }]
```

```text
$ aisland workspace harness install
==> Harness: aisland (5 commands, 1 script, rules), models (1 command, rules), rules (rules)
```

## What you may change

The usual places read for a set without `harness.toml`, as agents add theirs. Never a precedence between sets, and
never an edit of the workspace's own `AGENTS.md`.

**Amendment (2026-11-21): the projects' skills reach the root.** Claude Code offers the skills of the folder a session
starts in, so a project's skill reached a session at the workspace root only through a link the root repository
tracked — and a workspace with a root project has no root repository to track it (decision 0083). The harness now
links each skill an installed project ships, `<project>/.claude/skills/<skill>/SKILL.md`, as
`<root>/.claude/skills/<skill>`, recorded and ignored like the sets' files and taken away once no installed project
ships it: a project switched off on a machine brings none, so no link points at nothing. Two projects shipping one
skill name link neither, and a folder of the workspace's own keeps its place; both are said. Skills are not a file
kind of a set: a project ships them where Claude Code reads them, with no declaration.

---

[The architecture and every decision](../architecture.md#the-decisions)
