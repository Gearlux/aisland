"""Tests for ``setup_git.sh`` alias installation.

The workspace git aliases assume the acme mono-repo layout, so they MUST
land in LOCAL config — on the root repo AND every active submodule (each
submodule has its own .git config, and e.g. ``git safe`` runs inside one).
Earlier versions wrote them ``--global``, polluting unrelated repos on the
machine; the script now also migrates those stale global copies away.

The whole script runs against a throwaway workspace with ``GIT_CONFIG_GLOBAL``
pointed at a sandbox file, so the developer's real global config is never
touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SETUP_GIT = Path(__file__).resolve().parents[1] / "aisland" / "git" / "setup_git.sh"
# Where the script sits in a workspace; it finds the root three levels above itself.
SCRIPTS = Path("aisland", "aisland", "git")

ALIASES = ["pull-all", "push-all", "switch-all", "merge-all", "tag-all", "status-all", "safe"]


def _git(*args: str, cwd: Path, env: dict) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, env=env, text=True, stderr=subprocess.DEVNULL).strip()


@pytest.fixture()
def sandbox(tmp_path: Path) -> tuple[Path, dict]:
    """A temp workspace repo with one submodule and a sandboxed global config."""
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig-global")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "test@example.com"

    sub_origin = tmp_path / "sub-origin"
    sub_origin.mkdir()
    subprocess.check_call(["git", "init", "-q"], cwd=sub_origin, env=env)
    (sub_origin / "README.md").write_text("sub\n")
    subprocess.check_call(["git", "add", "."], cwd=sub_origin, env=env)
    subprocess.check_call(["git", "commit", "-q", "-m", "init"], cwd=sub_origin, env=env)

    root = tmp_path / "workspace"
    root.mkdir()
    subprocess.check_call(["git", "init", "-q"], cwd=root, env=env)
    subprocess.check_call(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_origin), "subproject"],
        cwd=root,
        env=env,
    )
    subprocess.check_call(["git", "commit", "-q", "-m", "add submodule"], cwd=root, env=env)

    (root / SCRIPTS).mkdir(parents=True)
    (root / SCRIPTS / "setup_git.sh").write_text(SETUP_GIT.read_text())
    return root, env


def _run_script(root: Path, env: dict) -> str:
    return subprocess.check_output(
        ["bash", str(root / SCRIPTS / "setup_git.sh")], cwd=root, env=env, text=True, stderr=subprocess.STDOUT
    )


def test_aliases_installed_locally_on_root_and_submodule(sandbox: tuple[Path, dict]) -> None:
    root, env = sandbox
    _run_script(root, env)
    for repo in (root, root / "subproject"):
        for alias in ALIASES:
            value = _git("config", "--local", "--get", f"alias.{alias}", cwd=repo, env=env)
            assert value.startswith("!"), f"alias.{alias} missing/odd in {repo}"


def test_no_alias_leaks_to_global_config(sandbox: tuple[Path, dict]) -> None:
    root, env = sandbox
    _run_script(root, env)
    global_config = Path(env["GIT_CONFIG_GLOBAL"])
    content = global_config.read_text() if global_config.exists() else ""
    assert "[alias]" not in content, f"global config polluted:\n{content}"


def test_stale_global_alias_is_migrated_away(sandbox: tuple[Path, dict]) -> None:
    root, env = sandbox
    # Simulate a machine provisioned by the old script version.
    subprocess.check_call(["git", "config", "--global", "alias.safe", "!echo old-global-safe"], cwd=root, env=env)
    output = _run_script(root, env)
    assert "removed stale GLOBAL alias.safe" in output
    assert "old-global-safe" not in Path(env["GIT_CONFIG_GLOBAL"]).read_text()
    # The local replacement is in place.
    assert _git("config", "--local", "--get", "alias.safe", cwd=root, env=env).startswith("!")


def test_rerun_is_idempotent(sandbox: tuple[Path, dict]) -> None:
    root, env = sandbox
    _run_script(root, env)
    second = _run_script(root, env)
    assert "removed stale GLOBAL" not in second


def test_safe_alias_resolves_root_and_project(sandbox: tuple[Path, dict]) -> None:
    """The safe alias's mypy step must run from the workspace root with the
    project subdir as target (root mypy.ini's mypy_path only works there)."""
    root, env = sandbox
    _run_script(root, env)
    safe = _git("config", "--local", "--get", "alias.safe", cwd=root, env=env)
    assert "--show-superproject-working-tree" in safe
    assert 'mypy "$PROJECT"' in safe and 'cd "$WS_ROOT"' in safe
    # Exercise the resolution logic exactly as the alias body does.
    snippet = (
        "WS_ROOT=$(git rev-parse --show-superproject-working-tree); "
        'PROJECT=$(basename "$(git rev-parse --show-toplevel)"); '
        'if [ -z "$WS_ROOT" ]; then WS_ROOT=$(git rev-parse --show-toplevel); PROJECT=.; fi; '
        'echo "$WS_ROOT|$PROJECT"'
    )
    in_sub = subprocess.check_output(["sh", "-c", snippet], cwd=root / "subproject", env=env, text=True).strip()
    ws_root, project = in_sub.split("|")
    assert Path(ws_root).resolve() == root.resolve() and project == "subproject"
    at_root = subprocess.check_output(["sh", "-c", snippet], cwd=root, env=env, text=True).strip()
    ws_root, project = at_root.split("|")
    assert Path(ws_root).resolve() == root.resolve() and project == "."


def test_every_alias_is_one_line_so_git_config_list_reads_as_key_value_pairs(sandbox: tuple[Path, dict]) -> None:
    """A tool reading ``git config --list`` line by line (git-filter-repo, which ``aisland wheels prune`` runs)
    takes every line for ``key=value``: an alias body holding a newline breaks it."""
    root, env = sandbox
    _run_script(root, env)
    for repo in (root, root / "subproject"):
        for alias in ALIASES:
            value = _git("config", "--local", "--get", f"alias.{alias}", cwd=repo, env=env)
            assert "\n" not in value, f"alias.{alias} spans lines in {repo}"
        listing = _git("config", "--list", cwd=repo, env=env).splitlines()
        assert all("=" in line for line in listing), [line for line in listing if "=" not in line]


def test_a_multi_line_alias_an_earlier_run_wrote_is_replaced_by_one_line(sandbox: tuple[Path, dict]) -> None:
    root, env = sandbox
    subprocess.check_call(["git", "config", "--local", "alias.tag-all", "!f() {\n  echo old;\n}; f"], cwd=root, env=env)
    _run_script(root, env)
    assert "\n" not in _git("config", "--local", "--get", "alias.tag-all", cwd=root, env=env)


def test_the_one_line_aliases_still_run(sandbox: tuple[Path, dict]) -> None:
    root, env = sandbox
    _run_script(root, env)
    usage = subprocess.run(["git", "tag-all"], cwd=root, env=env, capture_output=True, text=True)
    assert usage.returncode == 1 and "Usage: git tag-all <tag> [message]" in usage.stdout
    no_message = subprocess.run(["git", "safe"], cwd=root / "subproject", env=env, capture_output=True, text=True)
    assert no_message.returncode == 1 and "Error: Please provide a commit message." in no_message.stdout
    status = subprocess.run(["git", "status-all"], cwd=root, env=env, capture_output=True, text=True)
    assert status.returncode == 0, status.stderr
    assert status.stdout.startswith("ROOT") and "\nsubproject" in status.stdout
