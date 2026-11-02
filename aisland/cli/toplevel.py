"""The top-level commands that are no service and no tool of their own: `setup`, `check`, `doctor`, `clean` and
`backups`, `tests`, `completion`, `mcp` and `notebooks`.
"""

import json
from pathlib import Path
from typing import Optional

import click

from aisland import nightly, pytest_stats
from aisland.clean import DEFAULT_KEEP_DAYS
from aisland.cli._base import _Command, _Group
from aisland.manager import setup_workspace
from aisland.mcp_config import generate_gemini_settings, generate_mcp_json
from aisland.pytest_stats import DEFAULT_WINDOW_DAYS
from aisland.quality import run_quality_gates

# --- Notebook Commands ---


@click.group(cls=_Group)
def notebooks() -> None:
    """Manage Jupyter notebooks in the workspace."""
    pass


@notebooks.command(name="clean")
def notebooks_clean() -> None:
    """
    Clear outputs of code cells from standard input.
    Can be used as a git filter to avoid committing notebook outputs.
    """
    from aisland.notebook_utils import clean_notebook_stdin

    clean_notebook_stdin()


@click.command(cls=_Command)
@click.option(
    "--non-interactive",
    "-y",
    is_flag=True,
    help="Reuse this machine's saved answers; still ask about an optional project never answered.",
)
@click.option(
    "--all",
    "install_all",
    is_flag=True,
    help="Install every extra every project defines, without asking, but those a project installs only when "
    "named ([tool.aisland] manual-extras). Computed from the projects' own [project.optional-dependencies]; does "
    "not need an extra named `all`.",
)
def setup(non_interactive: bool, install_all: bool) -> None:
    """Full workspace setup: provision tools, ask about the optional projects, install the active ones.

    Which projects this machine has: `aisland workspace configure active` (a project with an `active` value in
    workspace.toml is optional, and setup asks about it once).

    What each project installs is a selection — `extras`, and each one the projects declare (`framework`,
    `tracker`, …) — asked once and committed in workspace.toml; choose beforehand with
    `aisland workspace configure <selection> set`, or take every extra with --all.

    \b
    aisland setup --all
    aisland workspace configure framework set torch --machine-only && aisland setup -y
    """
    setup_workspace(non_interactive=non_interactive, install_all=install_all)


@click.command(cls=_Command)
def doctor() -> None:
    """Check the shared .venv and the Docker/Colima setup for known recurring skew (exit 1 if any check fails)."""
    from aisland.doctor import run_doctor

    raise SystemExit(run_doctor())


@click.command(name="clean", cls=_Command)
@click.argument("project")
@click.option(
    "--keep-days",
    type=click.IntRange(min=0),
    default=DEFAULT_KEEP_DAYS,
    show_default=True,
    help="Delete backups older than this; the sweep runs on EVERY invocation.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be moved and swept; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def clean(project: str, keep_days: int, dry_run: bool, yes: bool) -> None:
    """Move PROJECT's git-ignored content into a dated backup under .aisland/data/backups.

    Reclaims the disk a checkout accumulates (`.venv/`, `build/`, caches, run dirs) without
    the irreversibility of `git clean -Xdf`: everything is renamed into the backup store and
    only really disappears when the retention sweep expires it. ONE project per invocation —
    there is no `--all`, and the workspace root is refused.

    \b
    aisland clean models --dry-run     # what would move, and which backups expire
    aisland clean models               # move it, after confirming
    aisland clean models --keep-days 1 # sweep harder on a full disk
    """
    from aisland.clean import clean_project

    raise SystemExit(clean_project(project, keep_days=keep_days, dry_run=dry_run, assume_yes=yes))


@click.group(cls=_Group)
def backups() -> None:
    """Inspect and restore the backups `aisland clean` takes.

    A separate group from `clean` on purpose: `aisland clean <project>` acts on a project's
    working tree, while these act on the STORE under `.aisland/data/backups` (`aisland/data/backups` from a
    checkout). Keeping the
    destructive verb one word long also keeps it from ever being typed by accident.
    """
    pass


@backups.command(name="list")
@click.argument("project", required=False)
@click.option(
    "--keep-days",
    type=click.IntRange(min=0),
    default=DEFAULT_KEEP_DAYS,
    show_default=True,
    help="Retention window the EXPIRES column is computed against.",
)
def backups_list(project: Optional[str], keep_days: int) -> None:
    """Show every backup in the store (newest first), optionally just PROJECT's."""
    from aisland.clean import list_store

    raise SystemExit(list_store(project, keep_days=keep_days))


@backups.command(name="prune")
@click.option(
    "--keep-days",
    type=click.IntRange(min=0),
    default=DEFAULT_KEEP_DAYS,
    show_default=True,
    help="Delete backups older than this, for every project.",
)
@click.option("--dry-run", is_flag=True, help="Show what would be deleted; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def backups_prune(keep_days: int, dry_run: bool, yes: bool) -> None:
    """Apply the retention window to the whole store, without cleaning anything.

    `aisland clean` already sweeps on every run; this is the same sweep on its own, for a
    workspace nobody is currently cleaning. It covers every project — the retention window is
    a property of the store, not of one project.

    \b
    aisland backups prune --dry-run    # what has expired
    aisland backups prune              # delete it (asks first)
    aisland backups prune --keep-days 1
    """
    from aisland.clean import prune_store

    raise SystemExit(prune_store(keep_days=keep_days, dry_run=dry_run, assume_yes=yes))


@backups.command(name="restore")
@click.argument("project")
@click.option("--stamp", default=None, help="Backup to restore from  [default: the most recent]")
@click.option("--entry", "entries", multiple=True, help="Restore only this path (repeatable).")
@click.option("--force", is_flag=True, help="Replace a path the project already has, instead of skipping it.")
@click.option("--dry-run", is_flag=True, help="Show what would be restored; change nothing.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation --force asks for.")
def backups_restore(
    project: str,
    stamp: Optional[str],
    entries: tuple[str, ...],
    force: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Move a backup's content back into PROJECT.

    Moves rather than copies — the disk was full enough to run a clean, so a second copy is
    the one thing that may not fit. The backup is consumed as it is restored: its manifest is
    rewritten and an emptied backup is removed. A path the project already has is SKIPPED
    (exit 1) unless `--force` replaces it.

    \b
    aisland backups restore models                       # the most recent backup, whole
    aisland backups restore models --stamp 20260804-164945
    aisland backups restore models --entry checkpoints   # just this one, repeatable
    aisland backups restore models --force               # overwrite what came back on its own
    """
    from aisland.clean import restore_backup

    raise SystemExit(restore_backup(project, stamp=stamp, only=entries, force=force, dry_run=dry_run, assume_yes=yes))


# --- Test-Run Statistics ---
#
# Root AGENTS.md "Two Test Runs": the fast/slow split is decided from these records, never by
# guess. The pytest plugin in `aisland/pytest_stats.py` writes them; this group reads them.


@click.group(cls=_Group)
def tests() -> None:
    """Every pytest run in the workspace, recorded — read back per project."""


@tests.command(name="report")
@click.argument("project")
@click.option(
    "--days",
    type=click.IntRange(min=1),
    default=DEFAULT_WINDOW_DAYS,
    show_default=True,
    help="How far back to read the local runs and the Jenkins builds.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON (what an MCP tool would return).")
def tests_report(project: str, days: int, as_json: bool) -> None:
    """PROJECT's fast run against its 5-minute budget, its slowest tests, the tests that failed, and what to move.

    Reads the local runs (`.aisland/data/test-stats/PROJECT/`, `aisland/data/…` from a checkout) and the local Jenkins builds'
    JUnit results. A fast run over budget lists the never-failed tests to mark `slow`; a slow
    test that failed is listed for the fast run.

    \b
    aisland tests report models
    aisland tests report models --days 90
    aisland tests report models --json
    """
    if not (pytest_stats.WORKSPACE_ROOT / project / "pyproject.toml").is_file():
        raise click.BadParameter(
            f"no project {project!r} in the workspace {pytest_stats.WORKSPACE_ROOT}", param_hint="PROJECT"
        )
    report = pytest_stats.build_report(project, days=days)
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else pytest_stats.render_report(report))


@tests.command(name="nightly")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON (what an MCP tool would return).")
@click.option("--no-notify", "no_notify", is_flag=True, help="Write the report but show no desktop notification.")
def tests_nightly(as_json: bool, no_notify: bool) -> None:
    """Report the local Jenkins' nightly runs since the previous report; exit 1 when one failed or none ran.

    Reads every 01:00 run of the orchestrator (`<workspace>.local`) started since the previous
    report, writes `.aisland/data/nightly/<date>.md` (`aisland/data/` from a checkout) — each
    project's result, a failed one's stage, tests and error lines — and shows a desktop
    notification when a night failed, had no run, or is still building. The `<workspace>.nightly-check` Jenkins job runs it each weekday at 09:30.

    \b
    aisland tests nightly
    aisland tests nightly --json --no-notify
    """
    report, status = nightly.run_nightly(notify=not no_notify)
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else nightly.render_markdown(report))
    raise SystemExit(status)


# --- Completion Commands ---


@click.group(cls=_Group)
def completion() -> None:
    """Install shell completion for every CLI in the workspace."""
    pass


@completion.command(name="install")
@click.option(
    "--target-rc",
    type=click.Path(path_type=Path),
    default=None,
    help="Rc file to write into. Default: the workspace `.project.bashrc.completion`, which "
    "project.bashrc sources. Pass ~/.bashrc (or project.bashrc) to install elsewhere.",
)
@click.option("--shell", type=click.Choice(["bash", "zsh", "fish"]), default=None, help="Default: detect from $SHELL.")
@click.option("--no-prune", is_flag=True, help="Keep blocks for programs that are no longer installed.")
def completion_install(target_rc: Optional[Path], shell: Optional[str], no_prune: bool) -> None:
    """Install completion for every workspace CLI — liquifai apps, click apps, argparse apps.

    Idempotent: every block is marker-delimited and rewritten in place, so re-run this after
    installing a new project rather than re-running the whole of `aisland setup`.
    """
    from aisland.completion import install_completions, is_sourced_by, shell_snippet, verify_click_completion

    report = install_completions(target_rc=target_rc, shell=shell, prune=not no_prune)

    click.echo(f"==> {report.rc} ({report.shell})")
    click.echo(f"    liquifai apps: {', '.join(report.liquifai_apps) or '(none discovered)'}")
    click.echo(f"    other CLIs:    {', '.join(report.non_liquifai) or '(none found)'}")
    if report.pruned:
        click.echo(f"    pruned stale:  {', '.join(report.pruned)}")
    if not verify_click_completion("aisland"):
        click.echo("    WARNING: `aisland` emitted no completion script — its block will be inert.")

    # The fragment only does anything once something sources it. Say so, but only when it
    # is actually unsourced — this is a hint, not a nag on every re-run.
    root = report.rc.parent
    if not any(is_sourced_by(report.rc, root / name) for name in ("project.bashrc", ".bashrc", ".zshrc")):
        if not is_sourced_by(report.rc, Path.home() / f".{report.shell}rc"):
            click.echo(f"\n    Not sourced yet — add to your shell rc:\n      {shell_snippet(report.rc)}")
    click.echo(f"\n    Open a new shell (or `exec {report.shell}`) to pick it up.")


@click.command(name="check", cls=_Command)
@click.argument("arg1", required=False)
@click.argument("arg2", required=False)
def check(arg1: Optional[str], arg2: Optional[str]) -> None:
    """Run the quality gates (isort, black, flake8, mypy, pytest, act): every one, or one tool, of every project or one.

    \b
    aisland check                 every project, every tool
    aisland check models          every tool for models
    aisland check pytest          pytest for every project
    aisland check pytest models   pytest for models
    """
    run_quality_gates(arg1=arg1, arg2=arg2)


# --- MCP Commands ---


@click.group(cls=_Group)
def mcp() -> None:
    """Manage MCP client configuration for workspace MCP servers."""
    pass


@mcp.command(name="gemini")
def mcp_gemini() -> None:
    """Generate .gemini/settings.json for the workspace MCP servers (gitignored; regenerated)."""
    generate_gemini_settings()


@mcp.command(name="claude")
def mcp_claude() -> None:
    """Generate .mcp.json (Claude Code's project servers) for the workspace MCP servers (gitignored; regenerated)."""
    generate_mcp_json()
