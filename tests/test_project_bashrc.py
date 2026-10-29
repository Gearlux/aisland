"""`project.bashrc`: the workspace root and aisland's tool folder, found from where the real file sits.

From a checkout the root's `project.bashrc` links to `aisland/project.bashrc`; from a wheel `aisland setup` copies the
packaged file into `<workspace>/.aisland/` and links the root's entry to that copy. Either way the file's own folder
holds `bin/`, and its parent is the workspace root.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from aisland import locations, manager

BASHRC = Path(__file__).resolve().parents[1] / "project.bashrc"


def _bash(root: Path, script: str, extra_env: Optional[Dict[str, str]] = None) -> str:
    """Stdout of `script` run by bash in `root` from a clean environment plus `extra_env`."""
    env = {"HOME": str(root), "PATH": "/usr/bin:/bin", **(extra_env or {})}
    return subprocess.run(["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, check=True).stdout


def _source(root: Path) -> Tuple[str, str]:
    """`PROJECT_ROOT` and `PATH` after sourcing the root's entry from a clean environment."""
    out = _bash(root, 'source ./project.bashrc && printf "%s\\n%s" "$PROJECT_ROOT" "$PATH"')
    project_root, path = out.split("\n")
    return project_root, path


def _fake_venv(root: Path) -> None:
    """A `.venv` whose `activate` only records which venv was activated."""
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text(f'export VIRTUAL_ENV="{root}/.venv"\n')


def _layout(root: Path, home: str) -> None:
    (root / home).mkdir(parents=True)
    (root / home / "project.bashrc").write_text(BASHRC.read_text())
    os.symlink(f"{home}/project.bashrc", root / "project.bashrc")


def test_from_a_checkout_the_root_and_aislands_bin_folder(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, "aisland")
    project_root, path = _source(root)
    assert project_root == str(root)
    assert path.split(":")[0] == f"{root}/aisland/bin"


def test_from_a_wheel_the_root_and_the_workspace_s_aisland_bin_folder(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    project_root, path = _source(root)
    assert project_root == str(root)
    assert path.split(":")[0] == f"{root}/.aisland/bin"


def test_a_project_root_inherited_from_another_workspace_is_replaced_by_this_ones(tmp_path: Path) -> None:
    # A terminal opened from a shell that sourced another workspace inherits its PROJECT_ROOT; this workspace's
    # file still activates this workspace's venv.
    other = (tmp_path / "other").resolve()
    _layout(other, "aisland")
    _fake_venv(other)
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    _fake_venv(root)
    out = _bash(
        root,
        'source ./project.bashrc && printf "%s\\n%s" "$PROJECT_ROOT" "$VIRTUAL_ENV"',
        {"PROJECT_ROOT": str(other), "VIRTUAL_ENV": f"{other}/.venv"},
    )
    assert out.split("\n") == [str(root), f"{root}/.venv"]


def test_the_venv_of_the_workspace_itself_is_activated_from_a_clean_environment(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, "aisland")
    _fake_venv(root)
    assert _bash(root, 'source ./project.bashrc && printf "%s" "$VIRTUAL_ENV"') == f"{root}/.venv"


@pytest.mark.parametrize("content", ["", "# only a comment\n# DATA_ROOT=/data\n", "# comment\n\n   \n"])
def test_an_env_file_without_settings_prints_nothing(tmp_path: Path, content: str) -> None:
    # A bare `export` lists every exported variable (`declare -x HOME=...`) on each terminal start.
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text(content)
    assert _bash(root, "source ./project.bashrc") == ""


def test_the_env_files_settings_are_exported_and_its_comments_skipped(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text("# DATA_ROOT=/commented\nDATA_ROOT=/data\n\nRUN_DIR=/runs\n")
    out = _bash(root, 'source ./project.bashrc && bash -c \'printf "%s\\n%s" "$DATA_ROOT" "$RUN_DIR"\'')
    assert out.split("\n") == ["/data", "/runs"]


def test_a_quoted_value_with_a_space_is_exported_whole_and_a_variable_in_a_value_expanded(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text('DATA_ROOT="/Volumes/Store/My Data"\nRUN_DIR=$HOME/runs\n')
    out = _bash(root, 'source ./project.bashrc && bash -c \'printf "%s\\n%s" "$DATA_ROOT" "$RUN_DIR"\'')
    assert out.split("\n") == ["/Volumes/Store/My Data", f"{root}/runs"]


def _completions(root: Path, words: str, extra_env: Optional[Dict[str, str]] = None) -> list:
    """What Tab offers after `words` (bash source text, the word under the cursor last) once the file is sourced."""
    script = (
        f"source ./project.bashrc && COMP_WORDS=({words}) && COMP_CWORD=$((${{#COMP_WORDS[@]}} - 1)) && "
        'func=$(complete -p "${COMP_WORDS[0]}" | sed -n "s/.* -F \\([^ ]*\\) .*/\\1/p") && '
        '"$func" "${COMP_WORDS[0]}" "${COMP_WORDS[COMP_CWORD]}" "${COMP_WORDS[COMP_CWORD-1]}" && '
        'printf "%s\\n" "${COMPREPLY[@]}"'
    )
    return [line for line in _bash(root, script, extra_env).splitlines() if line]


def _models(root: Path) -> Path:
    """A model root holding two `rai…` folders (one with subfolders), a spaced name and a file."""
    models = root / "models"
    (models / "raidar" / "detector").mkdir(parents=True)
    (models / "raidar" / "classifier").mkdir()
    (models / "raidar-ultralytics").mkdir()
    (models / "Signal Truth Set").mkdir()
    (models / "raid.txt").write_text("")
    return models


@pytest.mark.parametrize(
    "helper,variable", [("cdp", None), ("cdd", "DATA_ROOT"), ("cdm", "MODEL_ROOT"), ("cdr", "RUN_DIR")]
)
def test_tab_after_a_cd_helper_offers_the_folders_under_its_root_and_never_a_file(
    tmp_path: Path, helper: str, variable: Optional[str]
) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    models = _models(root)
    if variable is None:
        (root / "models" / "raid.txt").rename(root / "raid.txt")
        for folder in ("raidar", "raidar-ultralytics"):
            (models / folder).rename(root / folder)
    env = {variable: str(models)} if variable else None
    assert sorted(_completions(root, f"{helper} rai", env)) == ["raidar-ultralytics/", "raidar/"]


def test_tab_after_a_folder_offers_the_folders_inside_it(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    env = {"MODEL_ROOT": str(_models(root))}
    assert sorted(_completions(root, "cdm raidar/", env)) == ["raidar/classifier/", "raidar/detector/"]


def test_a_folder_name_holding_a_space_is_offered_escaped_and_completes_from_its_escaped_start(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    env = {"MODEL_ROOT": str(_models(root))}
    assert _completions(root, "cdm Sig", env) == ["Signal\\ Truth\\ Set/"]
    assert _completions(root, "cdm 'Signal\\ Tr'", env) == ["Signal\\ Truth\\ Set/"]


def test_a_second_argument_and_an_unset_root_are_offered_nothing(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    env = {"MODEL_ROOT": str(_models(root))}
    assert _completions(root, "cdm raidar rai", env) == []
    assert _completions(root, "cdr rai") == []


def test_h_lists_the_history_and_with_a_word_only_the_lines_holding_it_in_any_case(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    script = (
        "set -o history; source ./project.bashrc; history -c; history -s 'pytest -q'; history -s 'ls'; "
        "history -s 'PYTEST -m slow'; echo '== all'; h; echo '== pytest'; h pytest"
    )
    out = _bash(root, script).splitlines()
    assert [line.split(None, 1)[1] for line in out if not line.startswith("==")] == [
        "pytest -q",
        "ls",
        "PYTEST -m slow",
        "pytest -q",
        "PYTEST -m slow",
    ]
    assert out[0] == "== all" and out[4] == "== pytest"


def test_h_is_defined_even_where_an_alias_h_exists(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    out = _bash(root, "shopt -s expand_aliases; alias h='echo alias'; source ./project.bashrc && type -t h")
    assert out.strip() == "function"


def _tm_arguments(root: Path, script: str) -> list:
    """The arguments `tm` hands tmux when `script` runs after sourcing, read from a stub `tmux` that records them."""
    stub = root / "stub-bin"
    stub.mkdir()
    record = root / "tmux-args"
    (stub / "tmux").write_text(f"#!/bin/bash\nprintf '%s\\0' \"$@\" > '{record}'\n")
    (stub / "tmux").chmod(0o755)
    _bash(root, f"source ./project.bashrc && {script}", {"PATH": f"{stub}:/usr/bin:/bin"})
    return record.read_text().split("\0")[:-1]


def test_tm_starts_a_session_in_the_current_folder_with_every_exported_variable(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / "sub dir").mkdir()
    args = _tm_arguments(
        root, "cd 'sub dir' && export SPACED=$'a b\\nc' && UNEXPORTED=1 && tm -s work && : \"$UNEXPORTED\""
    )
    assert args[:3] == ["new-session", "-c", f"{root}/sub dir"]
    assert args[-2:] == ["-s", "work"]
    passed = args[3:-2]
    assert passed[0::2] == ["-e"] * (len(passed) // 2)
    names = {kv.split("=", 1)[0]: kv.split("=", 1)[1] for kv in passed[1::2]}
    assert names["SPACED"] == "a b\nc"
    assert names["PROJECT_ROOT"] == str(root)
    assert names["PATH"].startswith(f"{root}/.aisland/bin:")
    assert "UNEXPORTED" not in names


def test_tm_leaves_the_terminal_and_tmuxs_own_variables_to_tmux(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    script = "export TERM=xterm TERM_PROGRAM=iTerm.app TMUX=/tmp/s,1,0 TMUX_PANE=%1 OLDPWD=/ KEPT=1 && tm"
    names = {kv.split("=", 1)[0] for kv in _tm_arguments(root, script)[4::2]}
    assert "KEPT" in names
    assert not names & {"TERM", "TERM_PROGRAM", "TMUX", "TMUX_PANE", "SHLVL", "PWD", "OLDPWD", "_"}


def test_tm_is_defined_even_where_an_alias_tm_exists(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    out = _bash(root, "shopt -s expand_aliases; alias tm='echo alias'; source ./project.bashrc && type -t tm")
    assert out.strip() == "function"


def test_variables_set_after_the_env_file_are_not_exported(tmp_path: Path) -> None:
    root = (tmp_path / "acme").resolve()
    _layout(root, ".aisland")
    (root / ".env").write_text("DATA_ROOT=/data\n")
    out = _bash(root, 'source ./project.bashrc && LATER=1 && bash -c \'printf "%s" "${LATER:-unexported}"\'')
    assert out == "unexported"


def test_setup_from_a_wheel_copies_the_packaged_file_and_links_the_root_to_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    package = tmp_path / "site" / "aisland"
    package.mkdir(parents=True)
    (package / "project.bashrc").write_text(BASHRC.read_text())
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setattr(locations, "PACKAGE_DIR", package)
    monkeypatch.setattr(manager, "BASE_DIR", root / ".aisland")
    manager.link_project_bashrc(root)
    assert (root / ".aisland" / "project.bashrc").read_text() == BASHRC.read_text()
    assert os.readlink(root / "project.bashrc") == ".aisland/project.bashrc"
    # A newer packaged file replaces the copy on the next setup: the copy is aisland's, never the user's.
    (package / "project.bashrc").write_text(BASHRC.read_text() + "# newer\n")
    manager.link_project_bashrc(root)
    assert (root / ".aisland" / "project.bashrc").read_text().endswith("# newer\n")
