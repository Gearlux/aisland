"""The examples under ``examples/`` run as written — the check CI's "Verify Examples" stage makes, here as well.

An example is a page's runnable twin and asserts what the page shows
(``tests/test_docs.py::test_every_example_is_shown_on_a_page``). Only CI's stage ran them: ``02_workspace_init.py``
went stale when ``init`` began writing ``@.agents/rules.md`` into the workspace's ``AGENTS.md``, and every test stayed
green until a pushed commit made that stage fail. Each example runs as its own process from a folder of its own, the
way a reader runs it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

#: An example never waits on its own: past this it is hung, not slow.
EXAMPLE_SECONDS = 600

#: Examples measured at a minute or more, run by the slow leg instead: ``04_completion.py`` installs the completion of
#: every command-line tool of the environment it runs in (101 s in the workspace's own venv).
SLOW_EXAMPLES = frozenset({"04_completion.py"})


def _examples() -> List[object]:
    return [
        pytest.param(example, id=example.name, marks=[pytest.mark.slow] if example.name in SLOW_EXAMPLES else [])
        for example in sorted(EXAMPLES.glob("*.py"))
    ]


@pytest.mark.parametrize("example", _examples())
def test_an_example_runs_as_written(example: Path, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(example)], cwd=tmp_path, capture_output=True, text=True, timeout=EXAMPLE_SECONDS
    )
    assert result.returncode == 0, f"{example.name} exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
