# `manager.py` is split by what each part does (2026-10-29)

## Context

`manager.py` had grown to 1722 lines holding eight unrelated jobs: the workspace root and project list, the tool
installers, the state folders, `BaseService`, the editor files, the MCP client files, the root `uv.toml`, the quality
gates — and `aisland setup`, which runs them. 18 modules imported it, 13 of them in a cycle broken by imports placed
inside functions, and other modules reached into it by private names (`_install_brew_packages`,
`_set_local_submodule_active`, `_dependency_order`).

## Decision

One module per job: `projects`, `installers`, `paths`, `services/base`, `editor_files`, `mcp_config`, `uv_config`,
`quality`; `manager` keeps `aisland setup` and runs the others. Every definition moved unchanged; a name another module
uses lost its underscore. No alias was left behind (pre-1.0: no backward compatibility).

## Consequences

`manager.py` was then 300 lines. `projects` imported nothing of aisland but `locations`, and `installers` only `locations`,
`paths`, `failures`, `downloads` and `errors`, so the modules that needed the root or a tool no longer imported the whole of setup; the lazy imports
that broke the old cycle in `registry`, `quality`, `selection` and `sources` became top-level imports. A test patching
`manager.<name>` patches the module the name now lives in.

## Example

The names as they are today, after the layering of 2026-11-06 ([architecture](../architecture.md#the-layers)) moved
`manager` into `setup/` and `projects` into `workspace/`:

```python
from aisland.workspace.projects import get_workspace_root        # was aisland.setup.manager
from aisland.workspace.model import Workspace                    # the install order: Workspace(root).install_order
from aisland.setup.installers import install_userspace_tool      # its table: aisland.userspace_tools.USERSPACE_TOOLS
```

## What you may change

Where a new step lives: give it its own module and call it from `setup_workspace`; never grow `manager.py` again.

---

[The architecture and every decision](../architecture.md#the-decisions)
