# Stability

What a release of aisland keeps, and what may change in any release. aisland follows semantic versioning. Before
1.0, a minor release (0.1 → 0.2) may change what this page lists as kept, and its CHANGELOG entry says what changed
and what to do about it. A patch release (0.1.0 → 0.1.1) changes none of it.

## What a release keeps

- **The commands and their options**, as `aisland --help` and each command's `--help` list them. An expected failure
  prints one `Error:` line on stderr and exits 1; success exits 0. The words a command prints are for a person: a
  script reads `--json` where a command offers it (`aisland workspace show --json`, `aisland tests report <project>
  --json`, `aisland wheels build <project> --json`, …), and the keys of that JSON are kept.
- **`workspace.toml`, format 1**: `[workspace]` (`name`, `format`, `root`, `track`, `test-stats`, the selections),
  `[ci]` (`templates`, `[ci.variables]`), `[harness]` (`sets`), and each `[project."<name>"]` (`path`, `url`, `branch`, `source`,
  `version`, `distribution`, `mirror`, `active`, `track`, `test-stats`, `ci`). A later aisland reads a format 1 file;
  a file of a newer format is refused with the upgrade to install. See [the workspace page](workspace.md).
  `template.toml` and `harness.toml` carry a `format` the same way: a set may ask for a newer aisland, never be
  misread by an older one.
- **What a project declares in its own `pyproject.toml`**, so it never imports aisland:
  - `[tool.aisland.selection.<name>]` (`question`, `choices`) — [selections](selections.md);
  - `[tool.aisland.service.<name>]` (`command`, `port`, `ready`, `needs`) — [services](services.md). Such a service
    always runs as `aisland services start|stop|restart|status|info|reset <name>`. It also gets a shortcut,
    `aisland <name> …`, as long as aisland has no command of that name. A release may add commands; when one takes
    the service's name, the service loses only the shortcut, and `aisland workspace show` says so. A project's
    service may not be named like one of aisland's own services, `docker`, `jenkins` and `swiftbar`. When a release
    adds a service of its own, a project's service of that name is refused from that release on, and its CHANGELOG
    says so;
  - `[tool.aisland.sys-dependencies]` (`brew`, `userspace`) and `[tool.aisland]` `manual-extras` and
    `click-commands` — [the workspace page](workspace.md);
  - the entry point group `aisland.plugins`: a class of kind `service` and what aisland hands it, `ctx.name`,
    `ctx.workspace`, `ctx.workspace_name`, `ctx.data_dir`, `ctx.port`, `ctx.background`, `ctx.options`, `ctx.run`,
    `ctx.write_env`, `ctx.read_env`, `ctx.ports`, `ctx.echo`, `ctx.fail`, `ctx.choose_command`. The contract has a
    version, a class's `aisland_api` (1 when absent): a release serves every earlier version, and a plugin asking for
    a newer one is refused with the upgrade to install. `aisland.plugins` (`ServiceContext`, `ServicePlugin`) types
    it, the one module a project may name, under `TYPE_CHECKING`.
- **A harness set** ([harness](harness.md)): `workspace.toml`'s `[harness] sets` (a name, `{ set, exclude, name }`,
  `{ git, ref, name, exclude }`), `harness.toml`'s `format` (1), `rules`, `commands`, `scripts` and `[files]`, the places a set
  without one is read from, a project's `[tool.aisland] harness`, the entry point group `aisland.harnesses`, and where
  the files land (`.agents/workflows/`, `.agents/scripts/`, `.agents/<set>/AGENTS.md`, `.agents/rules.md`).
- **A CI template set** ([ci](ci.md)): `template.toml`'s `format` (1), `[files]` and `[variables]`, the delimiters `<[ ]>`,
  `<% %>` and `<# #>`, the entry point group `aisland.ci_templates`, and the facts a template reads: `project`,
  `title`, `distribution`, `package`, `extras`, `browser`, `slow_tests`, `unreleased` (each with `name`, `url`,
  `ref`), `unreleased_owners`, `token_owner`, `token_secret`, `stage_limits`, `runner_limits`, `actions`,
  `timed_out_message`, `workspace`, `jenkins_url`, `node_tests`, `coverage`. A later release may add facts; it removes or renames none.
  aisland's own set keeps its variable `python_versions`.
- **The tools of `aisland-mcp`** ([mcp](mcp.md)): their names, their arguments and the keys of the JSON object each
  returns — for `services_status`, the same object `aisland services status --json` prints — and `warnings`, the
  lines a tool warned on stderr, there when it did.
- **The project order** `aisland workspace projects` prints, dependencies first: what a script that walks the
  projects reads, in place of an import of aisland.
- **The environment variables** the README's table lists, and what each one does.
- **The pytest plugin's switch**: `aisland workspace test-stats on|off` and what it writes into `workspace.toml`.

## What may change in any release

- **aisland's Python modules**, all but `aisland.plugins`. `import aisland.<module>` is no interface: every name in
  them may move or change. A project declares what it brings (above) and never imports aisland.
- **The files aisland generates**: the CI files, the VS Code workspace file, `pyrightconfig.json`, `project.bashrc`,
  `.mcp.json`, `.gemini/settings.json`, the root `uv.toml`, the completion file. Each says in its header how to
  write it again; after an upgrade, run `aisland setup` and `aisland jenkins scaffold <project>`. A CI template set
  of your own keeps its files' text, since aisland renders it as written.
- **The words a command prints**, its progress lines and what `aisland --debug` shows.
- **aisland's state folder** (`aisland/data/` from a checkout, `.aisland/` from a wheel): its layout is aisland's own.
- **This machine's settings** in `.git/config` (`aisland.*`): change them through the commands that write them
  (`--machine-only`), never by hand.
- **aisland's own harness set** — its commands and rules ([harness](harness.md)): they follow the workflow aisland's
  own development uses, and a release may change any of them; a copy you edited is kept.
