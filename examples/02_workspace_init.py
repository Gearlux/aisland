"""A new workspace, made with `aisland workspace init` in a temporary folder — what it writes, and its git aliases.

`aisland workspace init <folder>` creates a git repository with the workspace's starting files, installs the
workspace git aliases (`git pull-all`, `git push-all`, …) in its local git config, and copies aisland's agent
commands and rules into `.agents/`. Nothing outside the new folder is changed.

It shows, and asserts:

- the folder is a git repository holding `.gitignore`, `.env`, `mypy.ini` and `AGENTS.md`;
- `AGENTS.md` imports the rules of the workspace's harness sets (`.agents/rules.md`), and that file imports aisland's,
  which sit in `.agents/aisland/AGENTS.md`;
- the aliases are in the repository's LOCAL config, not your global one;
- a second `init` of the same folder writes over nothing it already has.

No network: `python examples/02_workspace_init.py`. The page: docs/workspace.md.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

AISLAND = str(Path(sys.executable).with_name("aisland"))


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    # GIT_CONFIG_GLOBAL=/dev/null: your own git configuration plays no part, and none is changed.
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


def main() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        here = Path(scratch)
        result = _run(AISLAND, "workspace", "init", "acme", cwd=here)
        print(result.stdout)
        assert result.returncode == 0, result.stdout + result.stderr
        workspace = here / "acme"

        assert (workspace / ".git").is_dir(), "the workspace is a git repository"
        for name in (".gitignore", ".env", "mypy.ini", "AGENTS.md"):
            assert (workspace / name).is_file(), f"init wrote {name}"
        assert (workspace / ".agents" / "aisland" / "AGENTS.md").is_file(), "aisland's rules are in .agents/"
        imported_by_workspace = (workspace / "AGENTS.md").read_text()
        imported_by_rules = (workspace / ".agents" / "rules.md").read_text()
        assert "@.agents/rules.md" in imported_by_workspace, "AGENTS.md imports the rules"
        assert "@.agents/aisland/AGENTS.md" in imported_by_rules, "which import aisland's"

        local = _run("git", "config", "--local", "--get-regexp", r"^alias\.", cwd=workspace).stdout
        aliases = sorted(
            {line.split()[0].removeprefix("alias.") for line in local.splitlines() if line.startswith("alias.")}
        )
        assert {"pull-all", "push-all", "status-all"} <= set(aliases), f"the workspace aliases are local: {aliases}"

        (workspace / ".env").write_text("DATA_ROOT=/data\n")
        again = _run(AISLAND, "workspace", "init", "acme", cwd=here)
        assert again.returncode == 0, again.stdout + again.stderr
        assert (workspace / ".env").read_text() == "DATA_ROOT=/data\n", "a second init keeps the files it finds"

    print(f"workspace acme: a git repository, its starting files, aliases {', '.join(aliases)}")


if __name__ == "__main__":
    main()
