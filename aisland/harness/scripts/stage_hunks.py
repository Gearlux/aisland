# Shipped by aisland (aisland/harness/scripts/stage_hunks.py), installed into .agents/scripts/ by `aisland setup`
# (aisland/setup/harness.py). Change it in aisland; `aisland setup` installs it again.
"""Stage only some of a file's hunks, each exactly where it was written — the step /commit-session uses when another
session has changed the same file.

    python3 .agents/scripts/stage_hunks.py list <path>          # the file's hunks against the index, numbered
    python3 .agents/scripts/stage_hunks.py take <path> 0,2,5    # stage those hunks only

Run it inside the repository that owns ``<path>`` (a submodule's own folder for a submodule's file).

Why not ``git apply --cached --unidiff-zero`` on a patch of the chosen hunks: git places a hunk with no context —
a pure insertion — by its NEW-side line number, which counts the lines of the hunks left out, so an insertion after a
left-out hunk lands that many lines too far down (twice in real commits — a function read a variable the
misplaced line was meant to set; README table rows landed at the end of the file).

What this does instead: it takes the file as the INDEX holds it (HEAD, plus anything another session already
staged), applies the chosen hunks of ``git diff -U0`` (index → working tree) by the index version's OWN line numbers,
from the bottom up so each hunk's numbers stay valid, checks every removed line against what it replaces, and writes
the result as the staged blob (``git hash-object`` + ``git update-index``), keeping the file's mode. Nothing in the
working tree changes.
"""

import re
import subprocess
import sys
from typing import List, NamedTuple, Optional

_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NO_NEWLINE = b"\\ No newline at end of file"


class Hunk(NamedTuple):
    """One hunk of a zero-context diff: where it sits in the old file, what it removes and what it puts there."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    minus: List[bytes]
    plus: List[bytes]


def _git(*args: str, data: Optional[bytes] = None) -> bytes:
    done = subprocess.run(["git", *args], input=data, capture_output=True, check=False)
    if done.returncode != 0:
        raise SystemExit(done.stderr.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
    return done.stdout


def _hunks(path: str) -> List[Hunk]:
    diff = _git("diff", "-U0", "--no-color", "--no-ext-diff", "--", path)
    if b"\nBinary files " in b"\n" + diff:
        raise SystemExit(f"{path} is a binary file — stage it whole or not at all")
    hunks: List[Hunk] = []
    last: Optional[List[bytes]] = None
    for line in diff.splitlines(keepends=True):
        found = _HEADER.match(line)
        if found:
            old_count = 1 if found.group(2) is None else int(found.group(2))
            new_count = 1 if found.group(4) is None else int(found.group(4))
            hunks.append(Hunk(int(found.group(1)), old_count, int(found.group(3)), new_count, [], []))
            last = None
        elif hunks and line.startswith(b"-") and not line.startswith(b"---"):
            hunks[-1].minus.append(line[1:])
            last = hunks[-1].minus
        elif hunks and line.startswith(b"+") and not line.startswith(b"+++"):
            hunks[-1].plus.append(line[1:])
            last = hunks[-1].plus
        elif line.rstrip(b"\n") == _NO_NEWLINE and last:
            last[-1] = last[-1].rstrip(b"\n")  # the line before the marker ends the file without a newline
    return hunks


def _index_entry(path: str) -> bytes:
    entry = _git("ls-files", "-s", "--", path)
    if not entry:
        raise SystemExit(f"{path} is not in the index — a new file is staged whole: git add -- {path}")
    return entry.split()[0]


def _first_changed(hunk: Hunk) -> str:
    line = hunk.minus[0] if hunk.minus else hunk.plus[0] if hunk.plus else b""
    sign = "-" if hunk.minus else "+"
    return f"{sign}{line.decode(errors='replace').rstrip()}"[:100]


def list_hunks(path: str) -> None:
    for number, hunk in enumerate(_hunks(path)):
        old = f"-{hunk.old_start}" + ("" if hunk.old_count == 1 else f",{hunk.old_count}")
        new = f"+{hunk.new_start}" + ("" if hunk.new_count == 1 else f",{hunk.new_count}")
        print(f"{number}  {old} {new}  | {_first_changed(hunk)}")


def take_hunks(path: str, wanted: str) -> None:
    mode = _index_entry(path)
    if not wanted.strip():
        raise SystemExit("name the hunks to stage, e.g. 0,2 (python3 .agents/scripts/stage_hunks.py list <path>)")
    hunks = _hunks(path)
    numbers = sorted({int(part) for part in wanted.split(",") if part.strip()})
    for number in numbers:
        if not 0 <= number < len(hunks):
            raise SystemExit(f"{path} has {len(hunks)} hunks (0-{len(hunks) - 1}); there is no hunk {number}")
    lines = _git("show", f":{path}").splitlines(keepends=True)
    for number in sorted(numbers, key=lambda n: hunks[n].old_start, reverse=True):
        hunk = hunks[number]
        # "-N,0" inserts AFTER line N; "-N,k" replaces lines N to N + k - 1
        start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if lines[start : start + hunk.old_count] != hunk.minus:
            raise SystemExit(f"{path}: hunk {number} no longer matches the staged file — list the hunks again")
        lines[start : start + hunk.old_count] = hunk.plus
    blob = _git("hash-object", "-w", "--stdin", data=b"".join(lines)).decode().strip()
    _git("update-index", "--cacheinfo", f"{mode.decode()},{blob},{path}")
    left = [str(n) for n in range(len(hunks)) if n not in numbers]
    print(f"{path}: staged hunks {', '.join(map(str, numbers))}; left unstaged: {', '.join(left) or 'none'}")


def main(argv: List[str]) -> None:
    if len(argv) == 2 and argv[0] == "list":
        list_hunks(argv[1])
    elif len(argv) == 3 and argv[0] == "take":
        take_hunks(argv[1], argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
