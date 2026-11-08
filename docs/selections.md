# Selections

This page covers selections: the choices (ML frameworks, tracking backends, any extra) that decide which optional dependencies a machine installs, how they are committed or kept per machine, and how projects declare them.

## Selections: what each project installs (`aisland workspace configure <selection>`)

Workspace packages keep their core minimal and put optional capabilities behind *extras*. A **selection** decides
which of them a machine installs. aisland has one of its own, `extras` (every extra a project defines); every other
selection is declared by the projects themselves, so a workspace has exactly the ones its projects ask for — for example
`framework` (what a machine **trains with**) and `tracker` (where its results **go**). A selection other than
`extras` exists only when a project declares it ([below](#a-project-declares-its-selections)); the examples on this page
use those two, and in a workspace whose projects declare neither, `aisland workspace configure framework` is not a
command:

```text
$ aisland workspace configure framework
  FRAMEWORK    COMMITTED  THIS MACHINE  EFFECTIVE  PROJECTS
  fastai       no         yes           yes        models, tools, viewer
  keras        yes        yes           yes        models, tools, lib, viewer
  rfdetr       yes        no            no         viewer
  torch        yes        yes           yes        models, tools, lib, viewer, app
  …
```

`-` in a column means that layer has no choice. The commands:

```bash
aisland workspace configure framework                              # the choices, both layers, who offers each
aisland workspace configure framework set torch lightning          # committed; CI installs it too
aisland workspace configure framework set keras --machine-only     # THIS machine only; nothing committed
aisland workspace configure framework unset --machine-only         # back to the committed choice
aisland workspace configure framework unset                        # forget both; the next setup asks again
aisland workspace configure tracker set mlflow tensorboard aim     # the same for every selection
aisland workspace configure tracker unset aim --machine-only       # THIS machine without aim; nothing committed
aisland workspace configure tracker unset aim                      # the committed choice without aim
```

`unset` with names drops just those from the layer `set` would write — the committed list, or with `--machine-only`
this machine's own (which starts from the committed list when the machine has none) — keeps the rest, and
reinstalls like `set`. A name that layer does not choose is refused before anything changes. Without names, `unset`
forgets the whole layer, as above.

TAB completes the names: after `set` the choices not typed yet, after `unset` the ones chosen on this machine.

`set` takes the **whole list** and reinstalls, at once and in dependency order, every project offering one of the
selection's choices:

```text
$ aisland workspace configure framework set keras --machine-only
==> framework = keras on this machine (nothing committed)
==> Installing lib (/w/lib[dev,keras])...
==> Installing tools (/w/tools[dev,keras,mlflow,tensorboard])...
```

A name no project offers is refused before anything is installed:

```text
$ aisland workspace configure framework set torch jax2
Error: no project offers framework 'jax2' (the choices: fastai, huggingface, jax, keras, lightning, mlx, rfdetr, tensorflow, torch)
```

**Switching does not uninstall.** uv has no notion of "the packages that belonged to this extra" — they are ordinary
dependencies, possibly shared — so after switching to `keras` the torch wheels are still present. Recreate the
`.venv` and run `aisland setup` for a genuinely clean set.

### A project declares its selections

The project that **asks the question** defines a selection; every project lists the extras it offers for it:

```toml
# the training project's pyproject.toml — it defines both selections
[tool.aisland.selection.framework]
question = "Which ML frameworks should this machine install?"
choices = ["torch", "lightning", "keras", "tensorflow"]

[tool.aisland.selection.tracker]
question = "Which experiment-tracking backends should this machine install?"
choices = ["mlflow", "tensorboard", "aim", "clearml", "wandb"]

# any other project's pyproject.toml — it only offers choices
[tool.aisland.selection.framework]
choices = ["lightning", "torch", "keras"]
```

Each choice must be an extra of the project offering it; `aisland setup` and every `configure` command refuse
anything else up front, naming the file and the line:

```text
Error: /w/models/pyproject.toml:31: models offers 'mlx' for selection 'framework', but defines no extra of that name
```

A selection's own name cannot be one aisland already reads: `name`, `format`, `track` and `test-stats` are
`[workspace]` settings, and `extras`, `active`, `mirror` and `source` are `aisland workspace configure` commands of
aisland's own. A selection named like one is refused the same way, naming its file and line.

The names line up across projects on purpose (`keras` is `keras` everywhere), so one choice drives every project.
Each project installs what it offers ∩ what is chosen, in the order it lists them:

| framework choice | lib offers `torch, keras` | models offers `lightning, torch, keras` |
| --- | --- | --- |
| `torch lightning keras` | `[dev,torch,keras]` | `[dev,lightning,torch,keras]` |
| `keras` | `[dev,keras]` | `[dev,keras]` |
| *(none)* | `[dev]` | `[dev]` |

A project that needs a framework outright keeps it as a plain dependency and offers no choice for it. Without the
project that asks the question, there is no such selection and the others' choices install nothing.

### Choosing at setup time

`aisland setup` asks each selection's question once, offering every choice with the projects that offer it, and
commits the answer (a blank answer is everything); `aisland setup -y` with nothing chosen takes everything rather
than hanging. To choose beforehand, set it first: `aisland workspace configure framework set torch --machine-only &&
aisland setup -y`. `aisland setup --all` takes every extra every project defines.

An extra no machine should get by default — one that pins a dependency to a remote branch, which would replace a
local checkout of it — is marked by its project:

```toml
[tool.aisland]
manual-extras = ["remote"]
```

The menu then shows it as `[4] remote       models (only when named)`, and a blank answer, `-y` and `--all` leave it
out; typing its name, or `aisland workspace configure extras set … remote`, installs it.

### The two layers

| layer | where | drives |
| --- | --- | --- |
| **committed** | `[workspace] framework = [...]` in `workspace.toml` | the shared default **and** what every generated CI pipeline installs |
| **this machine** | `git config --local aisland.framework.selected` | this machine's `.venv` only; it wins over the committed one |

A machine-local choice never reaches a tracked build file: if one developer's narrowing to torch reached CI, every
runner would silently stop testing the Keras backend — and tests needing a tracker `importorskip` it, so a runner
that stops installing `mlflow` produces a green build that checks less. `aisland jenkins scaffold` reads the
committed layer only.

**Unset is not empty.** No key and no machine choice means *nobody has chosen*, and setup asks; a recorded empty list
means *none, deliberately*, and is honoured silently.

**A service will not install its own backend.** A service a project brings whose backend is one of a selection's
choices — a tracking server whose package is a `tracker` choice, say — does not install it when it is missing: it
stops with the command that adds the backend to the choices already made, so the choice is recorded and survives the
next setup, for example:

```text
  Add it to the tracker selection, so the choice is recorded and survives the next setup:
      aisland workspace configure tracker set tensorboard mlflow
  Or, for this machine only:
      aisland workspace configure tracker set tensorboard mlflow --machine-only
```
