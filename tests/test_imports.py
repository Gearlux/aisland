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
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import List

import grimp
import pytest
from importlinter.cli import lint_imports

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


def test_the_import_graph_keeps_every_contract(capsys: pytest.CaptureFixture[str]) -> None:
    """``lint-imports``, as a person runs it from aisland's folder: every contract of ``[tool.importlinter]`` kept."""
    status = lint_imports(config_filename=str(PYPROJECT), no_cache=True)
    assert status == 0, capsys.readouterr().out


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


@pytest.mark.parametrize(
    "module, line, contract",
    [
        ("doctor.py", "from aisland.cli import main", "cli-on-top"),
        ("server.py", "import aisland.cli._base", "cli-on-top"),
        ("tunnel/state.py", "from aisland.workspace import projects", "tunnel-alone"),
        ("wheels/report.py", "from aisland.ci import quality", "wheels-below-ci-and-services"),
        ("workspace/file.py", "from aisland.services import registry", "setup-and-workspace-below-services-and-wheels"),
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
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from importlinter.cli import lint_imports;"
                f"sys.exit(lint_imports(config_filename='pyproject.toml', limit_to_contracts=('{each['id']}',),"
                " no_cache=True))",
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        kept = result.returncode == 0 and "Contracts: 1 kept, 0 broken." in result.stdout
        assert kept or (result.returncode == 1 and "Contracts: 0 kept, 1 broken." in result.stdout), (
            result.stdout + result.stderr
        )
        if not kept:
            broken.append(each["id"])
    assert contract in broken
