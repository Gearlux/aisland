# A service a project brings: declared in its `pyproject.toml`, or a class of its own that imports nothing of aisland

*2026-10-24/25* [declarations]

## Context

Every service was a class inside aisland, listed by hand in each of `aisland start|stop|status|info|reset`, whatever
project actually used it (FiftyOne serves curator's datasets, Aim and ClearML trainer's trackers). A workspace
without that project still carried the service, and adding one meant editing aisland.

## Decision

One list, `services/registry.py`, behind `aisland services [start|stop|…] [NAME…]`. A project adds to it in two ways:
a declaration (`[tool.aisland.service.<name>]`: command, port, ready path, `needs`), run as a `DeclaredService`; or an
`aisland.plugins` entry point of kind `service`, a class of its own needing only `start(ctx)`, run as a
`PluginService` — what it leaves out is aisland's own. The class gets a `ServiceContext` (its folder, the port asked
for, `run`, `write_env`, `fail`, …) and never imports aisland. Both start only when named, get `aisland <name> …`,
and may not take a name aisland uses. A moved service disappears with its project.

## Consequences

- FiftyOne lives in curator (`curator/fiftyone_service.py`); its data folder stays `data/fiftyone/`.
- `aisland services start` no longer starts FiftyOne; `aisland services start fiftyone` does.
- Finding the list imported each installed project's class (about 0.1 s for curator) until 2026-10-29, when the
  listing stopped importing project code (record "Listing the services imports no project's code").

## Example

```python
class FiftyOne:                      # curator/fiftyone_service.py — no aisland import
    kind = "service"

    def start(self, ctx):
        port = ctx.port or 5151
        ctx.write_env({"FIFTYONE_URL": f"http://localhost:{port}"})
        ctx.run([sys.executable, "-m", "fiftyone", "app", "launch", "--port", str(port), "--remote"],
                port=port, ready="/")
```

## What you may change

- What the context offers — when a real service needs more, never ahead of one.
- Never: a service class that imports aisland, or a project's service started by default.

**Amendment (2026-10-25): Aim and ClearML move to trainer.** The two needed more than FiftyOne, so the class may
now declare `options` (Aim's `--repo`, offered beside `--port`) and `supervised = False` (ClearML's containers are
Docker's, so its `info` stands alone), and the context gives `workspace_name`, `read_env()` (ClearML reuses the
login it wrote) and `choose_command(selection, name)` — the command that adds a tracker keeping the ones chosen, so
the "not installed" message is word for word aisland's. aisland keeps Jenkins, MLflow, Label Studio, Docker,
SwiftBar and IQEngine.

**Amendment (2026-10-26): Label Studio moves to labels, IQEngine to the signal project that reads SigMF.** aisland now keeps Jenkins,
MLflow, Docker and SwiftBar alone. Label Studio is labels's `labelstudio` extra, installed only when named (it
pins its own Django); its class asks git for the default user itself, so the context did not grow. IQEngine runs
under Docker (`supervised = False`) with its own `stop`/`status`/`reset`, and its reset wipes no data — so a class
may name the question `aisland <name> reset` asks first (`reset_question`), which `registry.declared` carries onto
the entry. Adding to an `extras` selection never chosen keeps `dev` in the command (`selection.add_command`): unset,
it installs each project's `dev`, and `set labelstudio` alone would have dropped it.

```python
class IQEngine:                      # the signal project's — no aisland import
    kind = "service"
    supervised = False
    options = {"recordings": "Directory of SigMF recordings to serve …"}
    reset_question = f"Remove the IQEngine container and the {image_ref()} image? (recordings are kept)"
```

**Amendment (2026-10-28): MLflow moves to trainer.** aisland keeps Jenkins, Docker and SwiftBar alone — the
services every workspace may want; a tracking server is the trackers' project's. It came out of the release review:
aisland's own MLflow failed with `aisland workspace configure tracker set mlflow`, a command that exists only where a
project defines the `tracker` selection, so in a workspace without that project its error named a command that is
not there. As trainer's class it exists only where that selection does, and `selection.missing_message` went with
it (`ctx.choose_command` is the one wording). Nothing the context offers had to grow. What changed for a user: `aisland
mlflow` comes with the project; `aisland services start` without names no longer starts it (a project's service starts
when named); `aisland mlflow ui <db>` is gone (`mlflow ui --backend-store-uri sqlite:///<db>` is the same). Its data
folder is still `data/mlflow/`, so a store written before the move is served after it.

```python
class MLflow:                        # trainer's — no aisland import
    kind = "service"

    def start(self, ctx: Any) -> None:
        ...
        ctx.run([sys.executable, "-m", "mlflow", "server", ..., "--backend-store-uri", _store(ctx.data_dir)],
                port=port, ready="/health")
```

---

[The architecture and every decision](../architecture.md#the-decisions)
