"""`aisland wheels …`: build, zip, install, prune and reset a project's wheels (:mod:`aisland.wheels`).
"""

import json
from pathlib import Path
from typing import List, Optional, Tuple

import click
from click.shell_completion import CompletionItem

from aisland.cli._base import Group
from aisland.errors import quiet_completion
from aisland.wheels import build as wheels
from aisland.wheels import files as wheels_files
from aisland.wheels import projects as wheels_projects
from aisland.wheels import prune as wheels_prune
from aisland.wheels import report as wheels_report
from aisland.workspace.projects import get_workspace_root

# --- Wheels For A Project Installed Outside The Workspace ---


@quiet_completion
def _complete_projects(incomplete: str, *, with_wheels: bool = False) -> List[CompletionItem]:
    """The workspace projects checked out here whose name starts with ``incomplete`` (``with_wheels``: only those
    whose ``wheels/`` holds a ``wheels.yaml``, the ones `aisland wheels zip` packs).

    Completion runs in the user's shell on every TAB: it reads the folders and their ``pyproject.toml`` only, and a
    workspace it cannot read offers nothing instead of an error.
    """
    projects = wheels_projects.workspace_projects(get_workspace_root())
    return [
        CompletionItem(project.name)
        for project in sorted(projects.values(), key=lambda p: p.name)
        if project.checked_out
        and project.name.startswith(incomplete)
        and (not with_wheels or (project.path / wheels_files.WHEELS_DIR / wheels_files.WHEELS_YAML).is_file())
    ]


def _complete_buildable(ctx: click.Context, param: click.Parameter, incomplete: str) -> List[CompletionItem]:
    return _complete_projects(incomplete)


def _complete_zippable(ctx: click.Context, param: click.Parameter, incomplete: str) -> List[CompletionItem]:
    return _complete_projects(incomplete, with_wheels=True)


class _WheelsGroup(Group):
    """`aisland wheels <project> …` is `aisland wheels build <project> …`: a first word that names no command is
    the project to build — so a TAB there offers the projects after the commands."""

    def resolve_command(
        self, ctx: click.Context, args: List[str]
    ) -> Tuple[Optional[str], Optional[click.Command], List[str]]:
        if args and args[0] not in self.commands and not args[0].startswith("-"):
            args = ["build", *args]
        return super().resolve_command(ctx, args)

    def shell_complete(self, ctx: click.Context, incomplete: str) -> List[CompletionItem]:
        items = super().shell_complete(ctx, incomplete)
        if incomplete.startswith("-"):
            return items
        return items + [item for item in _complete_projects(incomplete) if item.value not in self.commands]


@click.group(name="wheels", cls=_WheelsGroup, invoke_without_command=True)
@click.option(
    "--install",
    "install_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Short for `aisland wheels install ZIP`: unpack ZIP and install it into --python.",
)
@click.option("--python", "python", type=click.Path(path_type=Path, dir_okay=False), help="With --install.")
@click.option("--into", "into", type=click.Path(path_type=Path, file_okay=False), help="With --install.")
@click.pass_context
def wheels_group(
    ctx: click.Context, install_path: Optional[Path], python: Optional[Path], into: Optional[Path]
) -> None:
    """The wheels a project installed outside the workspace needs: build them, zip them, install a zip.

    \b
    aisland wheels build PROJECT [--include-published | --all] [--include-project] [--extra NAME] [--bundle] [--zip] [--commit] [--force] [--no-check] [--json]
    aisland wheels zip PROJECT
    aisland wheels install ZIP --python PYTHON [--into DIR] [-- install.py options]
    aisland wheels prune PROJECT [--apply] [--all] [--json]
    aisland wheels reset PROJECT [--json]
    aisland wheels PROJECT …          (= aisland wheels build PROJECT …)
    aisland wheels --install ZIP --python PYTHON [--into DIR]
    """
    if ctx.invoked_subcommand is not None:
        if install_path is not None:
            raise click.UsageError("--install takes no command after it")
        return
    if install_path is None:
        raise click.UsageError(
            "say what to do: aisland wheels build PROJECT, aisland wheels zip PROJECT, "
            "or aisland wheels install ZIP --python PYTHON"
        )
    if python is None:
        raise click.UsageError("--install needs --python: the interpreter of the environment to install into")
    status = wheels_files.install_zip(install_path, python, into or Path.cwd(), [], workspace_root=get_workspace_root())
    raise SystemExit(status)


@wheels_group.command(name="build")
@click.argument("project", shell_complete=_complete_buildable)
@click.option(
    "--include-published",
    is_flag=True,
    help="Also build the projects PROJECT needs that are on PyPI, from their checkouts (the commit, not the release).",
)
@click.option(
    "--all", "include_all", is_flag=True, help="Build every workspace project that has a checkout here, needed or not."
)
@click.option(
    "--include-project",
    is_flag=True,
    help="Also build PROJECT's own wheel; install.py installs it in place of PROJECT's folder, and the zip then "
    "installs from any folder.",
)
@click.option(
    "--extra",
    "extra",
    metavar="NAME",
    help="Also write the extra NAME into PROJECT's pyproject.toml, with a uv source per wheel built, so "
    "`uv pip install -e ./PROJECT[NAME]` installs PROJECT editable with every wheel; NAME must be in its "
    "[tool.aisland] manual-extras. Once written, every later build keeps it current without --extra, except "
    "one with --include-project. --commit commits pyproject.toml too.",
)
@click.option(
    "--bundle",
    is_flag=True,
    help="Also merge PROJECT and the wheels it needs into ONE wheel, PROJECT/wheels/bundle/PROJECT_bundle-VERSION-"
    "py3-none-any.whl, that plain `pip install` installs; pip then knows only that one name (`pip show <dependency>` "
    "finds nothing). Checked alone in an empty venv of its own.",
)
@click.option("--zip", "make_zip", is_flag=True, help="Also pack the folder into PROJECT/wheels/PROJECT-wheels.zip.")
@click.option(
    "--commit",
    is_flag=True,
    help="Commit PROJECT/wheels (the zip excepted) in PROJECT once the install check passed; without it git ignores "
    "the folder.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Build a project with uncommitted changes from its files as they are (those git ignores left out) instead "
    "of refusing it; its commit reads <sha>+uncommitted. Not with --commit.",
)
@click.option(
    "--no-check",
    is_flag=True,
    help="Build and write the wheels only: compare nothing with PyPI and install nothing in an empty venv. "
    "Not with --commit.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON (what an MCP tool would return).")
def wheels_build(
    project: str,
    include_published: bool,
    include_all: bool,
    include_project: bool,
    extra: Optional[str],
    bundle: bool,
    make_zip: bool,
    commit: bool,
    force: bool,
    no_check: bool,
    as_json: bool,
) -> None:
    """Build the wheels PROJECT needs into PROJECT/wheels, with wheels.yaml and install.py; check the install.

    By default only the projects PROJECT needs that are not on PyPI are built; --include-published
    builds every project it needs that has a checkout here, --all every workspace project that has
    one; --include-project builds PROJECT's own wheel too; --extra NAME writes the extra NAME into PROJECT's
    pyproject.toml, which installs PROJECT editable with every wheel built; --bundle merges PROJECT and the wheels it needs into
    one wheel that plain pip installs, in PROJECT/wheels/bundle. Each wheel is built from its project's last commit (a
    project with uncommitted changes is refused; --force builds it from its files as they are). wheels.yaml lists the wheels and which to install (yours to edit; a rerun keeps it);
    install.py installs them, naming each by its file, then PROJECT editable. Each dependency PROJECT
    still takes from PyPI is compared with its checkout, file by file; then install.py installs
    PROJECT in an empty venv and every module is imported. Exit 1 when that install or an import
    fails: the report names the project to release first; --no-check skips both checks. Git ignores
    the folder unless --commit, which commits it in PROJECT when the check passed.

    \b
    aisland wheels build models
    aisland wheels build models --include-published --zip
    aisland wheels build models --include-project --zip
    aisland wheels build models --all --extra wheels
    aisland wheels build models --include-published --bundle
    aisland wheels build models --all --commit --json
    aisland wheels build models --all --force --zip
    aisland wheels build models --all --zip --no-check
    """
    scope: wheels_report.WheelsScope = "all" if include_all else "needed" if include_published else "unpublished"
    report = wheels.vendor_wheels(
        get_workspace_root(),
        project,
        scope=scope,
        commit=commit,
        make_zip=make_zip,
        force=force,
        check=not no_check,
        include_project=include_project,
        bundle=bundle,
        extra=extra,
    )
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else wheels_report.render_report(report), nl=as_json)
    raise SystemExit(0 if report.ok else 1)


@wheels_group.command(name="zip")
@click.argument("project", shell_complete=_complete_zippable)
def wheels_zip(project: str) -> None:
    """Pack PROJECT/wheels as it is into PROJECT/wheels/PROJECT-wheels.zip.

    The zip installs with plain Python, from PROJECT's folder on any machine: `python PROJECT-wheels.zip`
    unpacks it into ./wheels and runs wheels/install.py (every other argument goes to install.py). A folder
    that installs PROJECT's own wheel (built with --include-project) makes a zip that installs from any folder.
    """
    archive = wheels.zip_wheels(get_workspace_root(), project)
    where = "any folder" if wheels_files.installs_own_wheel(archive.parent) else f"the {project} folder"
    click.echo(f"{archive}\ninstall it from {where} with: python {archive.name}")


@wheels_group.command(name="install", context_settings={"ignore_unknown_options": True})
@click.argument("zip_path", type=click.Path(path_type=Path, dir_okay=False))
@click.option(
    "--python",
    "python",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="The interpreter of the environment to install into (never the workspace's .venv).",
)
@click.option(
    "--into",
    "into",
    type=click.Path(path_type=Path, file_okay=False),
    help="The project's folder (default: the current one); the zip unpacks into its wheels/.",
)
@click.argument("rest", nargs=-1, type=click.UNPROCESSED)
def wheels_install(zip_path: Path, python: Path, into: Optional[Path], rest: Tuple[str, ...]) -> None:
    """Unpack ZIP into the project's wheels/ and run its install.py with PYTHON; REST goes to install.py.

    \b
    aisland wheels install models-wheels.zip --python ~/envs/models/bin/python
    aisland wheels install models-wheels.zip --python ~/envs/models/bin/python -- --dry-run
    """
    status = wheels_files.install_zip(
        zip_path, python, into or Path.cwd(), list(rest), workspace_root=get_workspace_root()
    )
    raise SystemExit(status)


@wheels_group.command(name="prune")
@click.argument("project", shell_complete=_complete_buildable)
@click.option(
    "--apply",
    is_flag=True,
    help="Rewrite the history and force-push origin; without it the run only says what would go.",
)
@click.option(
    "--all",
    "everything",
    is_flag=True,
    help="Strip EVERY wheel version from all branches and tags too, not only the ones none holds.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def wheels_prune_command(project: str, apply: bool, everything: bool, as_json: bool) -> None:
    """Strip from PROJECT's history every committed wheel version no branch or tag holds.

    A version stays when a branch or a tag of this clone or of origin holds it, so every branch tip and tag keeps
    exactly its files; only commit ids change. --apply refuses a clone with changes no commit holds or a branch
    that differs from origin's, backs every ref up into a bundle, rewrites, force-pushes origin's branches and tags
    in one atomic push (leased: a push someone made meanwhile refuses it all), shrinks this clone and re-records
    the workspace root's pointer to PROJECT. Every other clone then runs `aisland wheels reset PROJECT` before
    its next pull. Needs git-filter-repo: uv pip install 'aisland[prune]'.

    --all removes every wheel from every branch and tag as well: no tip keeps a wheel, so the committed files
    of each branch and tag change, not only their commit ids.

    \b
    aisland wheels prune models
    aisland wheels prune models --apply
    aisland wheels prune models --all --apply
    """
    report = wheels_prune.prune(get_workspace_root(), project, apply=apply, everything=everything)
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else wheels_prune.render_prune(report))


@wheels_group.command(name="reset")
@click.argument("project", shell_complete=_complete_buildable)
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def wheels_reset_command(project: str, as_json: bool) -> None:
    """Bring this clone of PROJECT onto the history `aisland wheels prune` rewrote on another machine.

    Strips the wheel versions of each of origin's prunes this clone still reaches — the same rewrite, so every
    branch that was origin's lands on origin's new commit, and commits origin lacks are kept on top. Changes no
    commit holds are set aside and put back; every ref is backed up into a bundle first. When the workspace root
    still records an old commit of PROJECT, it records the commit's rewritten twin — also in a clone already reset.
    Run it before the next pull: a pull merges the old history back in. Nothing pruned on origin, nothing changes.

    \b
    aisland wheels reset models
    """
    report = wheels_prune.reset(get_workspace_root(), project)
    click.echo(json.dumps(report.to_dict(), indent=2) if as_json else wheels_prune.render_reset(report))
