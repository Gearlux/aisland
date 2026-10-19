"""aisland names neither bridge nor signals: its code, tests and docs illustrate with a general acme project.

aisland serves every domain and runs from a wheel in any workspace, where the signal domain (signals) and a
private bridge (bridge) mean nothing. Root ``AGENTS.md`` → "General Projects Name Neither Bridge Nor Signals"
(user ruling 2026-10-19).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List

PROJECT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile("bridge|signals", re.IGNORECASE)


def _project_files() -> List[Path]:
    """Every file git tracks in aisland, and every new one it does not ignore (data/ and logs/ are ignored)."""
    listed = subprocess.run(
        ["git", "-C", str(PROJECT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [PROJECT / name for name in listed if (PROJECT / name).is_file()]


def test_no_file_of_aisland_names_bridge_or_signals() -> None:
    this_file = Path(__file__).resolve()
    hits = []
    for path in _project_files():
        if path.resolve() == this_file:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(PROJECT)}:{number}: {line.strip()}")
    assert not hits, "aisland names bridge or signals — use a general acme project instead:\n" + "\n".join(hits)
