"""Every module of the package imports first, on its own: no module-level import cycle.

The package keeps its imports at the top of each module; the two that would close a cycle stay in their function and
say so (``projects.get_workspace_name`` → ``workspace_file``, ``provision.install`` → ``services.jenkins``). A cycle
shows only when the module that starts it is imported FIRST, so each module is imported with no ``aisland`` module
loaded before it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import aisland

PACKAGE = Path(aisland.__file__).parent

#: Imports each module named on the command line with no aisland module loaded, and prints the ones that fail.
_IMPORT_EACH_FIRST = """\
import importlib, json, sys
failures = {}
for name in sys.argv[1:]:
    for loaded in [m for m in sys.modules if m == "aisland" or m.startswith("aisland.")]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except Exception as error:
        failures[name] = f"{type(error).__name__}: {error}"
print(json.dumps(failures))
"""


def _modules() -> list[str]:
    names = []
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
        if "harness" in parts or parts[-1] == "__main__":
            continue
        names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def test_every_module_imports_first_on_its_own() -> None:
    modules = _modules()
    assert "aisland.cli" in modules and "aisland.services.jenkins_jobs" in modules
    result = subprocess.run(
        [sys.executable, "-P", "-c", _IMPORT_EACH_FIRST, *modules],
        cwd=PACKAGE.parent,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {}
