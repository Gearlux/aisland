"""Every module of the package imports first, on its own: no module-level import cycle; and the import graph keeps
the package's layers.

The package keeps every import of its own modules at the top: no function imports aisland to hide a cycle. A cycle
shows only when the module that starts it is imported FIRST, so each module is imported with no ``aisland`` module
loaded before it. The layers — only the CLI imports the CLI, the directions between subpackages no cycle uses, the
standard-library-only modules — are import-linter's contracts in ``pyproject.toml`` (``[tool.importlinter]``), run
here so the test run enforces them.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Dict, List, Optional

import grimp
import pytest

import aisland

PACKAGE = Path(aisland.__file__).parent
PYPROJECT = PACKAGE.parent / "pyproject.toml"

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


def _contracts() -> List[dict[str, object]]:
    contracts: List[dict[str, object]] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["importlinter"][
        "contracts"
    ]
    return contracts


def _lint_imports(
    cwd: Path, *arguments: str, env: Optional[Dict[str, str]] = None
) -> "subprocess.CompletedProcess[str]":
    """``lint_imports(<arguments>)`` in a process of its own: import-linter configures logging with the standard
    library's ``dictConfig``, which disables every logger already made — aisland's too, so a later test of this
    process that reads aisland's debug log through ``caplog`` would read nothing."""
    call = f"import sys; from importlinter.cli import lint_imports; sys.exit(lint_imports({', '.join(arguments)}))"
    return subprocess.run([sys.executable, "-c", call], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def test_the_import_graph_keeps_every_contract() -> None:
    """``lint-imports``, as a person runs it from aisland's folder: every contract of ``[tool.importlinter]`` kept."""
    result = _lint_imports(PYPROJECT.parent, f"config_filename={str(PYPROJECT)!r}", "no_cache=True")
    assert result.returncode == 0, result.stdout + result.stderr


def test_linting_the_imports_leaves_aislands_loggers_on() -> None:
    """Why the lint runs out of process: in this one, import-linter turned aisland's loggers off, and the tests that
    read aisland's debug log after it on the same worker read nothing."""
    test_the_import_graph_keeps_every_contract()
    assert not logging.getLogger("aisland").disabled
    assert not logging.getLogger("aisland.setup.installers").disabled


def test_the_standard_library_contract_names_every_third_party_package_aisland_imports() -> None:
    """The contract lists packages, not "everything outside the standard library": a package any module imports must
    be in its list, or a standard-library-only module could import it unseen."""
    graph = grimp.build_graph(
        "aisland", include_external_packages=True, exclude_type_checking_imports=True, cache_dir=None
    )
    imported = {m for m in graph.modules if m.split(".")[0] != "aisland" and m not in sys.stdlib_module_names}
    (stdlib_only,) = [c for c in _contracts() if c["id"] == "stdlib-only"]
    forbidden = stdlib_only["forbidden_modules"]
    assert isinstance(forbidden, list)
    assert imported - set(forbidden) == set()


def test_every_top_level_module_is_in_a_layer_or_a_leaf() -> None:
    """A module the layers do not name and the leaves do not list would be held to nothing: a new one joins a list."""
    (layers,) = [c for c in _contracts() if c["id"] == "layers"]
    (leaves,) = [c for c in _contracts() if c["id"] == "leaves"]
    layered, leaf_modules, forbidden = layers["layers"], leaves["source_modules"], leaves["forbidden_modules"]
    assert isinstance(layered, list) and isinstance(leaf_modules, list) and isinstance(forbidden, list)
    named = {name.strip() for layer in layered for name in str(layer).split("|")}
    graph = grimp.build_graph("aisland", exclude_type_checking_imports=True, cache_dir=None)
    top = {module for module in graph.modules if module.count(".") == 1}
    assert top - named - set(leaf_modules) == set()
    assert set(forbidden) == named, "the leaves are forbidden every layer, and only those"


@pytest.mark.parametrize(
    "module, line, contract",
    [
        ("doctor.py", "from aisland.cli import main", "layers"),
        ("server.py", "import aisland.cli._base", "layers"),
        ("tunnel/state.py", "from aisland.workspace import projects", "layers"),
        ("wheels/report.py", "from aisland.ci import quality", "layers"),
        ("workspace/file.py", "from aisland.services import registry", "layers"),
        ("workspace/sources.py", "from aisland.setup import editor_files", "layers"),
        ("paths.py", "from aisland.workspace import projects", "leaves"),
        ("atomic.py", "import click", "stdlib-only"),
        ("tunnel/shell.py", "from aisland import locations", "runs-without-aisland"),
    ],
)
def test_a_contract_breaks_on_the_import_it_forbids(tmp_path: Path, module: str, line: str, contract: str) -> None:
    """A copy of the package with one forbidden import added: that contract, and only it, is reported broken."""
    copy = tmp_path / "aisland"
    shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(PYPROJECT, tmp_path / "pyproject.toml")
    target = copy / module
    text = target.read_text(encoding="utf-8")
    target.write_text(f"{line}  # noqa\n{text}", encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(tmp_path), "COLUMNS": "400"}
    broken = []
    for each in _contracts():
        result = _lint_imports(
            tmp_path,
            "config_filename='pyproject.toml'",
            f"limit_to_contracts=('{each['id']}',)",
            "no_cache=True",
            env=environment,
        )
        kept = result.returncode == 0 and "Contracts: 1 kept, 0 broken." in result.stdout
        assert kept or (result.returncode == 1 and "Contracts: 0 kept, 1 broken." in result.stdout), (
            result.stdout + result.stderr
        )
        if not kept:
            broken.append(each["id"])
    assert contract in broken
