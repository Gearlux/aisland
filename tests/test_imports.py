"""Every module of the package imports first, on its own: no module-level import cycle.

The package keeps every import of its own modules at the top: no function imports aisland to hide a cycle. A cycle
shows only when the module that starts it is imported FIRST, so each module is imported with no ``aisland`` module
loaded before it.
"""

from __future__ import annotations

import ast
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


def test_no_function_imports_a_module_of_aisland() -> None:
    """An import inside a function is how a cycle hides from the test above; the package has none."""
    hidden = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    continue
                if any(name == "aisland" or name.startswith("aisland.") for name in names):
                    hidden.append(f"{path.relative_to(PACKAGE.parent)}:{node.lineno}")
    assert hidden == []
