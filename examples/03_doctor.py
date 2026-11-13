"""`aisland doctor` — every check, one line each, and an exit status that says whether all passed.

The doctor looks for the environment problems that recur in a shared virtual environment and on a development
machine, and prints the command that fixes each one it finds. It only reads: it installs, deletes and starts
nothing. Its results depend on the machine it runs on, so this example asserts the report's shape, not its verdicts.

It shows, and asserts:

- one line per check, `✓` or `✗` and the check's name, for every check the doctor has;
- a last line counting them — `All N checks passed.` or `K of N checks failed.`;
- exit status 0 exactly when every check passed, 1 otherwise.

`python examples/03_doctor.py`. The page: docs/doctor.md.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

AISLAND = str(Path(sys.executable).with_name("aisland"))
CHECKS = {
    "opencv",
    "huggingface_hub",
    "mcp",
    "docker-creds",
    "colima-tls",
    "jenkins-java",
    "torch-ext-lock",
    "tunnel-relay",
    "tunnel-connect",
    "pylance-memory",
}


def main() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        result = subprocess.run([AISLAND, "doctor"], cwd=scratch, capture_output=True, text=True, timeout=300)
    print(result.stdout)

    verdicts = dict(
        (name, mark == "✓") for mark, name in re.findall(r"^\s+([✓✗])\s+(\S+)", result.stdout, flags=re.MULTILINE)
    )
    assert set(verdicts) == CHECKS, f"one line per check: {sorted(verdicts)}"

    failed = [name for name, passed in verdicts.items() if not passed]
    last = result.stdout.strip().splitlines()[-1]
    if failed:
        assert last == f"{len(failed)} of {len(CHECKS)} checks failed.", last
        assert result.returncode == 1, f"a failed check exits 1 (got {result.returncode})"
    else:
        assert last == f"All {len(CHECKS)} checks passed.", last
        assert result.returncode == 0, f"all checks passed exits 0 (got {result.returncode})"
    print(f"{len(CHECKS)} checks, {len(failed)} failed: {', '.join(failed) or 'none'}; exit {result.returncode}")


if __name__ == "__main__":
    main()
