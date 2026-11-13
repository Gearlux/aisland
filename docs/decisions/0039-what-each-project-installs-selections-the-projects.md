# What each project installs: selections the projects declare, committed in `workspace.toml`

*2026-10-23; replaces the framework registry of 2026-07-30 and the one `config.toml` of 2026-08-01* [frameworks] [config]

## Context

Selection began on 2026-07-30, when the workspace's packages put each ML framework behind an extra and "install every
extra" was wrong twice over: a project's engine extras are alternatives no machine wants all of, and `dev`,
`notebook` or `all` sit in the same table, so no name rule tells an engine from a documentation extra. So a project
declared its choices and the workspace selected names, each project installing what it declares ∩ what is selected,
in two layers like the project sources; the CI scaffolder read only the committed one, because one developer's "I
only use torch" reaching a runner would turn a suite green by not running it. On 2026-08-01 the committed files
then in use (`sources.toml`, `frameworks.toml`, `trackers.toml`) were merged into one `config.toml`.

By 2026-10-23 aisland hard-coded two selections, `FRAMEWORKS` and `TRACKERS` (their names, questions and hints), beside its own
`EXTRAS`. A project declared its menu under `[tool.aisland] frameworks = [...]`, the choice was committed in
`config.toml` (`frameworks`, `trackers`) and kept per machine as `aisland.frameworks`; the commands were `aisland
framework|tracker list|set|unset` (with `--no-apply`) and `aisland setup --framework/--extra`, and `aisland config
show|migrate` read the file and folded the pre-2026-08 ones into it. A choice that was no extra was found by a
separate check (`require_defined_declarations`) that named no line.

## Decision

A selection is the projects': the one whose `[tool.aisland.selection.<name>]` has `question` defines it (trainer
asks `framework` and `tracker`), every project's `choices` are its menu, and discovery refuses a choice that is no
extra, naming the file and the line. aisland keeps only `extras`. The choice is committed as `[workspace] <name> =
[...]` in `workspace.toml` and chosen per machine as `aisland.<name>.selected`; each selection is a workspace-level
plugin of `aisland workspace configure` (`set NAME… [--machine-only]`, `unset [NAME…] [--machine-only]`), which
reinstalls at once.
`config.toml`, `config.py`, `frameworks.py`, `aisland config`, `aisland framework|tracker`, setup's
`--framework/--extra` and `--no-apply` are gone. A service whose backend is missing names `set` with the trackers
already chosen plus its own: `set` takes the whole list, and the old message (`aisland tracker set mlflow`) would
have dropped the rest.

Every committed setting is now in `workspace.toml` — a project's in its table, a selection's choice under
`[workspace]` — and its per-machine deviation in `.git/config`. `uv.toml` never joins it: uv reads only its own
settings file and rejects a foreign table (measured against uv 0.11.8 when `config.toml` was made:
``error: Failed to parse: `config.toml`. The `sources` field is not allowed in a `uv.toml` file.``), and the root
`uv.toml` is generated per machine from the activated projects and gitignored, so a private index reaches it only
while its project is active.

Kept from 2026-07-30: unset is not empty (setup asks on unset; a recorded empty choice is honoured), the reinstall
runs in setup's install order (a consumer's extra can name a sibling's, `models[keras]` requiring `lib[keras]`, and
installing the consumer first resolves against the sibling's old metadata), deselecting uninstalls nothing, and a
project that needs one framework outright keeps it as a base dependency rather than a faked extra.

## Consequences

- aisland names no framework or tracker; a workspace has exactly the selections its projects ask for.
- One committed file. Measured on the workspace aisland is developed in: every generated `Jenkinsfile`, `Jenkinsfile.local` and `ci.yml` (78 files)
  is byte-identical to the `config.toml` era's, with every dependency released and with none.
- A machine opts out of a choice with `set … --machine-only`; choosing before a first setup is `set` then
  `setup -y`.

## Example

```toml
# trainer/pyproject.toml — defines the selection
[tool.aisland.selection.tracker]
question = "Which experiment-tracking backends should this machine install?"
choices = ["mlflow", "tensorboard", "aim", "clearml", "wandb"]

# workspace.toml — the committed choice
[workspace]
tracker = ["mlflow", "tensorboard", "aim", "clearml", "wandb"]
```

```bash
aisland workspace configure tracker set mlflow tensorboard                  # the whole list; reinstalls at once
aisland workspace configure tracker set mlflow --machine-only               # this machine: aisland.tracker.selected
```

## What you may change

- A selection's question (in the project that defines it), the listing's columns, the prompt's layout.
- Never: a selection named in aisland's code (but `extras`), a second committed file, `uv.toml`'s settings in
  `workspace.toml`, or a machine's choice reaching a generated CI file.

## A selection cannot take a name aisland reads (2026-11-07)

A selection's choice is committed under its own name in `[workspace]`, and the selection becomes `aisland workspace
configure <name>`. Only `name` was refused, so a project declaring `[tool.aisland.selection.format]` made `render`
write `format` twice and the file no longer parsed, and `track`, `test-stats`, `extras`, `active`, `mirror` or `source`
would have shadowed aisland's own. `discovery.RESERVED_SELECTION_NAMES` names each with why; discovery refuses them
with the file and the line, and a test holds the list equal to `check.WORKSPACE_KEYS`, `configure.PLUGINS` and
`selection.EXTRAS`.

---

[The architecture and every decision](../architecture.md#the-decisions)
