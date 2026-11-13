"""`aisland notebooks clean` — a notebook's outputs cleared on its way in, a documentation notebook's kept.

`aisland notebooks clean` reads a notebook on standard input and writes it to standard output without the
outputs of its code cells, which is what a git filter needs: a notebook is committed without its results.
A notebook whose metadata says `"is_documentation": true` keeps its outputs — its results are its point.

It shows, and asserts:

- a code cell's outputs are dropped and its execution count cleared;
- the code itself is kept;
- a documentation notebook comes out with its outputs.

No network, no Jupyter: `python examples/05_notebooks_clean.py`. The page: docs/notebooks.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

AISLAND = str(Path(sys.executable).with_name("aisland"))


def _notebook(documentation: bool) -> Dict[str, Any]:
    cell = {
        "cell_type": "code",
        "source": ["1 + 1"],
        "outputs": [{"output_type": "stream", "name": "stdout", "text": ["2\n"]}],
        "execution_count": 1,
        "metadata": {},
    }
    metadata = {"is_documentation": True} if documentation else {}
    return {"cells": [cell], "metadata": metadata, "nbformat": 4, "nbformat_minor": 5}


def _clean(notebook: Dict[str, Any]) -> Dict[str, Any]:
    result = subprocess.run(
        [AISLAND, "notebooks", "clean"], input=json.dumps(notebook), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cleaned: Dict[str, Any] = json.loads(result.stdout)
    return cleaned


def main() -> None:
    (cell,) = _clean(_notebook(documentation=False))["cells"]
    print(json.dumps(cell, indent=1))
    assert cell["outputs"] == [], "the outputs are gone"
    assert cell["execution_count"] is None, "the execution count is cleared"
    assert cell["source"] == ["1 + 1"], "the code is kept"

    (kept,) = _clean(_notebook(documentation=True))["cells"]
    assert (
        kept["outputs"] == _notebook(documentation=True)["cells"][0]["outputs"]
    ), "a documentation notebook keeps them"
    print("a notebook loses its outputs; a documentation notebook keeps them")


if __name__ == "__main__":
    main()
