"""`aisland completion install` — the three CLI families, and the pruner's safety rail.

The bug that motivated this module is the reason for most of these: the generated rc file
sat stale for weeks with no signal, because the only way to refresh it was to re-run the
whole provisioning script. So the pins are about the things that fail SILENTLY — a family
missing from the file, a block that evals to nothing, and a pruner that could empty it.
"""

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from aisland import completion as comp
from aisland.cli import main
from aisland.projects import get_workspace_name

LIQUIFAI_APPS = ["matrainer", "sonair"]


@pytest.fixture
def rc(tmp_path: Path) -> Path:
    return tmp_path / ".project.bashrc.completion"


@pytest.fixture
def fake_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub liquifai's app discovery.

    The real one PROBES every candidate binary in the venv (`--show-completion bash`) for any
    app that has not declared the `liquifai.apps` entry point, which costs ~38s per call. The
    CLI tests exercise the command's own wiring, not liquifai's discovery.
    """
    import liquifai.completion as lc

    monkeypatch.setattr(lc, "discover_liquifai_apps", lambda prefix=None: list(LIQUIFAI_APPS))


#: The real discovery of the workspace's click scripts, kept before the autouse stub replaces it.
_WORKSPACE_CLICK_CLIS = comp.workspace_click_clis


@pytest.fixture(autouse=True)
def no_workspace_clis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workspace these tests run in has click scripts of its own; a test names the ones it means."""
    monkeypatch.setattr(comp, "workspace_click_clis", lambda root=None: [])


@pytest.fixture
def all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every program resolves — the normal, fully-installed workspace."""
    monkeypatch.setattr(comp, "_program_exists", lambda prog: True)


# --------------------------------------------------------------------------- #
# the three families
# --------------------------------------------------------------------------- #


def test_the_families_land_in_one_file(rc: Path, all_present: None) -> None:
    """The whole point: every family in a single install.

    Only TWO are populated by the workspace today — ``streamstudio`` was the sole argparse CLI
    until its 2026-08-26 liquifai migration, and it now completes with the rest of that family.
    The argcomplete RENDERING is still exercised, on a synthetic row, by the test below.
    """
    report = comp.install_completions(target_rc=rc, shell="bash", apps=LIQUIFAI_APPS)
    text = rc.read_text()

    for app in LIQUIFAI_APPS:  # (a) liquifai
        assert f"# >>> liquifai completion for {app} >>>" in text
    assert "_AISLAND_COMPLETE=bash_source aisland" in text  # (b) click

    assert report.liquifai_apps == LIQUIFAI_APPS
    assert report.non_liquifai == ["aisland"]


def test_a_projects_declared_click_command_is_found_without_importing_it(tmp_path: Path) -> None:
    """aisland names no workspace CLI: a project lists its click scripts in `[tool.aisland] click-commands`."""
    root = tmp_path / "ws"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "pyproject.toml").write_text(
        '[project]\nname = "tools"\nversion = "0.1.0"\n\n[project.scripts]\n'
        'tool = "nosuch_module:main"\nplain = "nosuch_module:plain"\n\n[tool.aisland]\nclick-commands = ["tool"]\n'
    )
    assert _WORKSPACE_CLICK_CLIS(root) == [comp.NonLiquifaiCli("tool", "click")]


def test_a_workspace_whose_declarations_are_refused_completes_no_project_cli(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "pyproject.toml").write_text(
        '[project]\nname = "tools"\nversion = "0.1.0"\n\n[tool.aisland]\nclick-commands = ["missing"]\n'
    )
    assert _WORKSPACE_CLICK_CLIS(root) == []


def test_a_found_click_script_is_installed_beside_aisland(
    rc: Path, all_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = [comp.NonLiquifaiCli("tool", "click"), comp.NonLiquifaiCli("matrainer", "click")]
    monkeypatch.setattr(comp, "workspace_click_clis", lambda root=None: found)
    report = comp.install_completions(target_rc=rc, shell="bash", apps=["matrainer"])
    assert "_TOOL_COMPLETE=bash_source tool" in rc.read_text()
    assert report.non_liquifai == ["aisland", "tool"]  # a liquifai app keeps its own block, never a second one


def test_a_streamstudio_row_would_be_a_SECOND_dead_block(rc: Path, all_present: None) -> None:
    """streamstudio must not be in the non-liquifai table — it is a liquifai app now.

    Left there, every install would emit an argcomplete registration ALONGSIDE the liquifai one,
    for a binary that no longer calls ``argcomplete.autocomplete``.
    """
    assert "streamstudio" not in [cli.prog for cli in comp.NON_LIQUIFAI_CLIS]


def test_the_argcomplete_family_still_renders(rc: Path, all_present: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The branch stays covered on a synthetic row, so it is ready for the next argparse CLI."""
    monkeypatch.setattr(comp, "NON_LIQUIFAI_CLIS", (comp.NonLiquifaiCli("someargparsecli", "argcomplete"),))
    comp.install_completions(target_rc=rc, shell="bash", apps=[])
    assert "register-python-argcomplete --shell bash someargparsecli" in rc.read_text()


def test_the_click_block_is_guarded_on_the_bash_version(rc: Path, all_present: None) -> None:
    """Click refuses to emit for bash < 4.4 and macOS ships /bin/bash 3.2, so an unguarded
    eval would error on every shell start for anyone whose interactive shell is the system one."""
    comp.install_completions(target_rc=rc, shell="bash", apps=[])

    assert '"${BASH_VERSINFO[0]}" -gt 4' in rc.read_text()


def test_a_non_bash_shell_gets_that_shells_source_and_no_version_guard(rc: Path, all_present: None) -> None:
    comp.install_completions(target_rc=rc, shell="zsh", apps=[])
    text = rc.read_text()

    assert "_AISLAND_COMPLETE=zsh_source aisland" in text
    assert "BASH_VERSINFO" not in text


def test_a_missing_program_is_skipped_not_written_broken(rc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace without streamstudio must not get a block that evals a missing binary."""
    monkeypatch.setattr(comp, "_program_exists", lambda prog: prog == "aisland")
    report = comp.install_completions(target_rc=rc, shell="bash", apps=[])

    assert report.non_liquifai == ["aisland"]
    assert "streamstudio" not in rc.read_text()


# --------------------------------------------------------------------------- #
# idempotence and the pruner
# --------------------------------------------------------------------------- #


def test_re_running_rewrites_in_place(rc: Path, all_present: None) -> None:
    """Every block is marker-delimited, so this is safe to run from setup.sh AND by hand."""
    comp.install_completions(target_rc=rc, shell="bash", apps=LIQUIFAI_APPS)
    first = rc.read_text()
    comp.install_completions(target_rc=rc, shell="bash", apps=LIQUIFAI_APPS)

    assert rc.read_text() == first
    assert first.count(comp._NON_LIQUIFAI_START) == 1
    assert first.count("# >>> liquifai completion for sonair >>>") == 1


def test_a_block_for_a_vanished_app_is_pruned(rc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """liquifai's installer merges by marker and leaves foreign blocks alone, so a renamed
    app (fluxstudio -> streamstudio) would keep a dead block forever."""
    monkeypatch.setattr(comp, "_program_exists", lambda prog: True)
    comp.install_completions(target_rc=rc, shell="bash", apps=["sonair", "fluxstudio"])
    assert "# >>> liquifai completion for fluxstudio >>>" in rc.read_text()

    monkeypatch.setattr(comp, "_program_exists", lambda prog: prog != "fluxstudio")
    report = comp.install_completions(target_rc=rc, shell="bash", apps=["sonair"])

    assert report.pruned == ["fluxstudio"]
    assert "fluxstudio" not in rc.read_text()
    assert "# >>> liquifai completion for sonair >>>" in rc.read_text()


def test_the_pruner_stands_down_when_NO_app_was_discovered(rc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety rail. Discovery returning nothing means discovery is broken, not that every
    app was uninstalled — pruning on that signal would empty the file. This is the shape of
    the disaster the old shell version guarded against with `command -v liquifai`."""
    monkeypatch.setattr(comp, "_program_exists", lambda prog: True)
    comp.install_completions(target_rc=rc, shell="bash", apps=LIQUIFAI_APPS)

    monkeypatch.setattr(comp, "_program_exists", lambda prog: False)
    report = comp.install_completions(target_rc=rc, shell="bash", apps=[])

    assert report.pruned == []
    for app in LIQUIFAI_APPS:
        assert f"# >>> liquifai completion for {app} >>>" in rc.read_text()


def test_no_prune_keeps_a_stale_block(rc: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comp, "_program_exists", lambda prog: True)
    comp.install_completions(target_rc=rc, shell="bash", apps=["sonair", "gone"])

    monkeypatch.setattr(comp, "_program_exists", lambda prog: prog != "gone")
    report = comp.install_completions(target_rc=rc, shell="bash", apps=["sonair"], prune=False)

    assert report.pruned == []
    assert "# >>> liquifai completion for gone >>>" in rc.read_text()


# --------------------------------------------------------------------------- #
# the target rc is a choice
# --------------------------------------------------------------------------- #


def test_the_default_target_is_resolved_from_the_workspace_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never a hardcoded path (workspace mandate) — it follows `get_workspace_root`."""
    monkeypatch.setattr(comp, "get_workspace_root", lambda: Path("/somewhere/ws"))

    assert comp.default_target_rc() == Path("/somewhere/ws/.project.bashrc.completion")


def test_a_users_own_rc_can_be_the_target(tmp_path: Path, all_present: None) -> None:
    """`--target-rc ~/.bashrc` — for someone who does not want the sourced fragment."""
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# my own rc\nexport FOO=1\n")

    comp.install_completions(target_rc=bashrc, shell="bash", apps=["sonair"])

    text = bashrc.read_text()
    assert "export FOO=1" in text, "an existing rc must be appended to, never overwritten"
    assert "# >>> liquifai completion for sonair >>>" in text
    assert "_AISLAND_COMPLETE=bash_source aisland" in text


def test_a_tilde_target_is_expanded(tmp_path: Path, all_present: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    comp.install_completions(target_rc=Path("~/.bashrc"), shell="bash", apps=[])

    assert (tmp_path / ".bashrc").exists()


def test_the_source_hint_uses_HOME_not_an_absolute_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert comp.shell_snippet(tmp_path / "ws" / "f") == '[ -f "$HOME/ws/f" ] && source "$HOME/ws/f"'


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #


def test_the_generated_file_carries_its_provenance(rc: Path, all_present: None) -> None:
    comp.install_completions(target_rc=rc, shell="bash", apps=[])
    text = rc.read_text()

    assert "AUTO-GENERATED" in text
    assert "aisland.completion" in text
    assert f"To regenerate:  {comp.REGENERATE_COMMAND}" in text


def test_the_regenerate_header_is_a_command_the_cli_accepts() -> None:
    """Parsed against the REAL command, not string-matched.

    The workspace has been bitten by exactly this: a scaffold header advertised
    `--project <name>` for months while the CLI took a positional, so the only instruction
    the generated file carried failed with "No such option".
    """
    prog, *argv = comp.REGENERATE_COMMAND.split()
    assert prog == "aisland"

    result = CliRunner().invoke(main, [*argv, "--help"])

    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- #
# the CLI surface
# --------------------------------------------------------------------------- #


def test_the_cli_reports_what_it_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_apps: None) -> None:
    monkeypatch.setattr(comp, "_program_exists", lambda prog: True)
    monkeypatch.setattr(comp, "verify_click_completion", lambda prog="aisland": True)
    rc = tmp_path / "frag"

    result = CliRunner().invoke(main, ["completion", "install", "--target-rc", str(rc), "--shell", "bash"])

    assert result.exit_code == 0, result.output
    assert str(rc) in result.output
    assert "liquifai apps:" in result.output and "other CLIs:" in result.output


def test_the_cli_warns_when_a_click_app_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_apps: None
) -> None:
    """An inert block is the silent failure this whole module exists to surface."""
    monkeypatch.setattr(comp, "_program_exists", lambda prog: True)
    monkeypatch.setattr(comp, "verify_click_completion", lambda prog="aisland": False)

    result = CliRunner().invoke(main, ["completion", "install", "--target-rc", str(tmp_path / "frag")])

    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output


def test_setup_sh_delegates_instead_of_reimplementing() -> None:
    """setup.sh must CALL the command, not carry its own copy of the logic.

    The inline version is what rotted; a second implementation would rot the same way.
    """
    setup = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()

    assert 'aisland" completion install' in setup or "aisland completion install" in setup
    assert "liquifai-install-completions" not in setup, "the shell copy is gone; the command owns this"
    assert comp._NON_LIQUIFAI_START not in setup, "the block is rendered in Python now"


# --------------------------------------------------------------------------- #
# the interactive-only guard (the fork bomb of 2026-10-20)
# --------------------------------------------------------------------------- #


def test_the_block_does_nothing_in_a_NON_interactive_shell(rc: Path, all_present: None) -> None:
    """The block's `eval "$(_X_COMPLETE=bash_source X)"` RUNS the CLI at source time, so any
    non-interactive bash that reads this file executes it.

    Measured 2026-10-20: with `BASH_ENV` pointing at `project.bashrc` (which sources this file),
    `aisland` emitted completion, click probed the shell with `bash --norc -c 'echo
    "${BASH_VERSION}"'`, that shell read `BASH_ENV`, sourced this file and ran `aisland` again —
    an endless aisland <-> bash recursion that filled the machine's process table (2931
    processes; nothing on the machine could fork, twice needing a reboot). It is self-sustaining:
    killing a slice lets the survivors refill.

    Completion means nothing without a prompt, so the block leaves at once unless the shell is
    interactive. That makes the recursion impossible whatever sets `BASH_ENV`.
    """
    comp.install_completions(target_rc=rc, shell="bash", apps=[])
    body = rc.read_text()
    start = body.index(comp._NON_LIQUIFAI_START)

    guard = "case $- in *i*) ;; *) return 0 ;; esac"
    assert guard in body, "the generated block must leave a non-interactive shell at once"
    assert body.index(guard) < body.index(
        "_AISLAND_COMPLETE", start
    ), "the guard must come BEFORE the eval that runs the CLI, or it guards nothing"


def test_the_guard_is_rendered_for_every_shell(rc: Path, all_present: None) -> None:
    """zsh and fish emit no bash version guard, but they run the CLI at source time just the
    same — the recursion is not bash's alone."""
    for shell in ("bash", "zsh"):
        comp.install_completions(target_rc=rc, shell=shell, apps=[])
        assert "case $- in *i*) ;; *) return 0 ;; esac" in rc.read_text(), shell


# --------------------------------------------------------------------------- #
# without liquifai (aisland installed from PyPI on its own)
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_liquifai(monkeypatch: pytest.MonkeyPatch) -> None:
    """`import liquifai.completion` fails, as in a venv where aisland is installed without it."""
    monkeypatch.setitem(sys.modules, "liquifai", None)
    monkeypatch.setitem(sys.modules, "liquifai.completion", None)


def test_without_liquifai_the_other_clis_are_still_installed(rc: Path, all_present: None, no_liquifai: None) -> None:
    """A venv without liquifai has no liquifai app to complete, so there is nothing to hand over — and no error."""
    report = comp.install_completions(target_rc=rc, shell="bash")
    assert report.liquifai_apps == []
    assert "aisland" in report.non_liquifai
    assert 'eval "$(_AISLAND_COMPLETE=bash_source aisland 2>/dev/null)" || true' in rc.read_text()


def test_without_liquifai_a_stale_liquifai_block_is_kept_not_pruned(
    rc: Path, all_present: None, no_liquifai: None
) -> None:
    """No liquifai means no discovery; pruning then would delete blocks of apps that may still exist."""
    rc.write_text("# >>> liquifai completion for sonair >>>\nblock\n# <<< liquifai completion for sonair <<<\n")
    report = comp.install_completions(target_rc=rc, shell="bash")
    assert report.pruned == []
    assert "liquifai completion for sonair" in rc.read_text()


@pytest.mark.parametrize(
    "login_shell, expected",
    [("/bin/zsh", "zsh"), ("/usr/local/bin/fish", "fish"), ("/bin/bash", "bash"), ("/bin/tcsh", "bash")],
)
def test_without_liquifai_the_shell_comes_from_SHELL(
    rc: Path,
    all_present: None,
    no_liquifai: None,
    monkeypatch: pytest.MonkeyPatch,
    login_shell: str,
    expected: str,
) -> None:
    monkeypatch.setenv("SHELL", login_shell)
    assert comp.install_completions(target_rc=rc).shell == expected


def test_without_liquifai_the_command_reports_no_liquifai_app(
    tmp_path: Path, all_present: None, no_liquifai: None
) -> None:
    rc = tmp_path / ".project.bashrc.completion"
    result = CliRunner().invoke(main, ["completion", "install", "--target-rc", str(rc), "--shell", "bash"])
    assert result.exit_code == 0, result.output
    assert "    liquifai apps: (none discovered)" in result.output.splitlines()


@pytest.mark.usefixtures("empty_workspace")
def test_the_file_names_no_workspace_project(rc: Path, all_present: None) -> None:
    """What aisland writes into a user's shell file is aisland's: its block is marked `aisland`, and its
    provenance line names the module (a pip user has no `aisland/aisland/` folder)."""
    comp.install_completions(target_rc=rc, shell="bash", apps=LIQUIFAI_APPS)
    text = rc.read_text()
    block = text[text.index(comp._NON_LIQUIFAI_START) : text.index(comp._NON_LIQUIFAI_END)]
    # aisland's block; liquifai's helpers hold this machine's venv path. A workspace named "aisland" is the block's own.
    name = get_workspace_name().lower()
    assert name == "aisland" or name not in block.lower()
    assert "# >>> aisland completion for non-liquifai CLIs >>>" in text
    assert "# <<< aisland completion for non-liquifai CLIs <<<" in text
    assert "# Source template: aisland.completion (_render_non_liquifai)" in text
