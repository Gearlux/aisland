"""``aisland jenkins scaffold``: a project's CI files, rendered from the workspace's template set.

This module works out the FACTS a template reads (:class:`aisland.ci_templates.Facts`) — the extras CI installs, the
internal dependencies a runner installs from GitHub, the slow marker, the Node test folders, the time limits — and
writes what :func:`aisland.ci_templates.render` makes of them, each Jenkinsfile checked by the running Jenkins'
linter first. Jenkins is asked for nothing else here: the jobs aisland seeds are :mod:`aisland.services.jenkins_jobs`.
"""

import functools
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import click
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from aisland import ci_templates, console, gitcmd
from aisland import pyproject as pyproject_toml
from aisland.errors import AislandError
from aisland.github import GitHubRepository, project_repositories
from aisland.projects import get_project_metadata, get_workspace_name, get_workspace_root
from aisland.pypi import released_on_pypi
from aisland.selection import PLAYWRIGHT_BROWSER
from aisland.selection import axes as sel_axes
from aisland.selection import declared as sel_declared
from aisland.selection import dev_extra_names
from aisland.selection import extras_for as sel_extras_for
from aisland.selection import read_committed as sel_committed
from aisland.sources import DEFAULT_REF, read_committed_sources
from aisland.wheels import extra as wheels_extra

#: Every GitHub Action a generated ``ci.yml`` uses — the ONE place their versions live. Each is a
#: MAJOR tag whose ``action.yml`` declares ``using: node24`` (2026-09-26: GitHub annotated every run
#: "Node.js 20 is deprecated … being forced to run on Node.js 24" for checkout@v4, setup-python@v5
#: and upload-artifact@v4). ``setup-uv`` stays on ``@v7``: from v8 upstream publishes NO moving major
#: tags (immutable ``@v8.0.0``-style pins only), and v7 is its last moving tag and already node24.
#: Verify a bump with ``gh api repos/<owner>/<action>/contents/action.yml?ref=<tag>`` (``runs.using``).
GH_ACTIONS: Dict[str, str] = {
    "checkout": "actions/checkout@v7",
    "setup-python": "actions/setup-python@v7",
    "setup-uv": "astral-sh/setup-uv@v7",
    "upload-artifact": "actions/upload-artifact@v7",
    "create-github-app-token": "actions/create-github-app-token@v3",
    "setup-node": "actions/setup-node@v7",
}

#: A long stage's time limit in minutes in `Jenkinsfile.local` (user choice 2026-10-15): past it the stage FAILS naming
#: itself and the limit (`TIMED_OUT_MESSAGE`), so one hung test never holds the nightly orchestrator — annotaide.local
#: #37's Unit Tests waited 109 min on a test ignoring SIGINT, another project's Slow Tests ran 519 min the
#: night before, and acme.local #36 waited on both with no limit anywhere. Each limit is about twice the longest
#: healthy stage of the 01:00 runs of 2026-10-10..15 (Unit Tests 6.0 min, Slow Tests 29.6 min, Verify Examples 0.5 min,
#: Verify Notebooks 1.4 min); Initialize and the quality gates run tools, not the project's code, and get none. Waiting
#: for an executor does not count: the node is held before the first stage. Raise a limit HERE when a project's healthy
#: run approaches it (the morning report names a timed-out stage), then re-scaffold every project. The push pipelines
#: carry none: a runner's limits are measured on the runner (aisland/TASKS.md). Node Tests (2026-10-31, no nightly run
#: yet): `npm ci` and the tests of the workspace's three Node folders took under a minute each on this machine.
STAGE_LIMITS: Dict[str, int] = {
    "Unit Tests": 15,
    "Slow Tests": 60,
    "Verify Examples": 15,
    "Verify Notebooks": 15,
    "Node Tests": 10,
}
#: A GitHub job's limit is its stage's times this (user pick 2026-10-28): a runner is slower than this machine, whose
#: runs the table was measured on, and without one a hung job holds the runner for GitHub's six hours. `ci.yml`'s
#: `timeout-minutes` come from here; the quality gates get none, as in Jenkins.
RUNNER_LIMIT_FACTOR = 3

#: The one line a stage past its limit writes (Groovy interpolation); `nightly._TIMED_OUT` reads it from the console.
TIMED_OUT_MESSAGE = (
    "${stage} timed out after ${minutes} min - its limit in Jenkinsfile.local (STAGE_LIMITS in aisland/ci_scaffold.py)"
)
#: The `limited` helper `Jenkinsfile.local` defines above `pipeline {` is aisland's template
#: `templates/ci/_limited.groovy.jinja`, which writes this message; each long stage's steps run inside it. Measured 2026-10-15
#: on a probe job (a 1 min limit around a shell ignoring SIGINT): the stage FAILED at 80 s, the build FAILURE, the
#: stage's post block ran, the next stages were skipped, the console ended with the ERROR line; Jenkins printed
#: `After 20s process did not stop` and the hung process lingered — the stage ends anyway, and the orchestrator sees
#: `<job> completed: FAILURE`, the path every failing project takes. Abort clicked before the limit is rethrown and
#: stays an abort. Sandbox: `System.currentTimeMillis` and `Closure.call` are in script-security's generic whitelist,
#: and a catch clause's class name is never checked by it.

#: Folders of a project that hold no package of its own, passed over when its package folder is looked for.
_NOT_PACKAGES = frozenset({"tests", "examples", "notebooks", "docs", "scripts"})

#: Folders a Node package's tests never live in: installed packages, and hidden folders (`.vscode-test`, `.git`).
_NOT_NODE_SOURCES = ("node_modules",)


def runner_limit(stage: str) -> int:
    """The `timeout-minutes` of the GitHub job running ``stage``."""
    return STAGE_LIMITS[stage] * RUNNER_LIMIT_FACTOR


def registers_slow_marker(pyproject_data: pyproject_toml.Table) -> bool:
    """True when the project registers the `slow` pytest marker — its declaration that it has a slow run."""
    markers = pyproject_data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    return any(str(m).split(":", 1)[0].strip() == "slow" for m in markers)


def node_test_folders(project_dir: Path) -> List[str]:
    """The folders of the project holding a `package.json` with a `test` script — its declaration that `npm test` is
    a test run of its own, which CI runs in a Node Tests stage. Paths relative to the project, POSIX, sorted.

    The `package.json` files are the ones git sees there (tracked, or untracked and not ignored): an installed
    package's under `node_modules/` or a service's data folder is no source of the project. Outside a git repository,
    the folders are walked, `node_modules` and hidden folders skipped. A `package.json` that is no JSON object is
    passed over, as npm itself would fail it.
    """
    listed = gitcmd.git(project_dir, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*package.json")
    if listed.returncode == 0:
        candidates = [project_dir / line for line in listed.stdout.splitlines() if Path(line).name == "package.json"]
    else:
        candidates = []
        for walked, subfolders, files in os.walk(project_dir):
            subfolders[:] = [d for d in subfolders if d not in _NOT_NODE_SOURCES and not d.startswith(".")]
            if "package.json" in files:
                candidates.append(Path(walked) / "package.json")
    folders = set()
    for manifest in candidates:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        scripts = data.get("scripts") if isinstance(data, dict) else None
        test = scripts.get("test") if isinstance(scripts, dict) else None
        folder = manifest.parent.relative_to(project_dir)
        if isinstance(test, str) and test.strip() and not set(_NOT_NODE_SOURCES) & set(folder.parts):
            folders.add(folder.as_posix())
    return sorted(folders)


def discover_github_projects(workspace_root: Path) -> Dict[str, GitHubRepository]:
    """Every workspace project whose remote is on GitHub, by its PyPI distribution name, with that repository.

    The remote is the project's own (``.gitmodules``, or a clone's ``origin``: :func:`aisland.github.project_repositories`),
    so the owner is whoever's the workspace's projects are — no organisation is written into aisland. The name is the
    ``[project] name`` of the project's ``pyproject.toml``; a project without one is skipped (it is no Python project
    aisland installs) unless it is DETACHED (a ``github`` / ``pypi`` source empties its folder), whose name is the
    committed source's ``distribution``, else its folder — omitting it would drop the git-URL pin a ``github`` source
    exists to express. A project off GitHub (a Bitbucket server) is never one CI installs from GitHub.

    Replaces a hard-coded list: a new project (a new submodule + pyproject) is picked up on the next
    ``aisland jenkins scaffold`` with no aisland edit.
    """
    detached = read_committed_sources(workspace_root)
    projects: Dict[str, GitHubRepository] = {}
    for folder, repository in project_repositories(workspace_root).items():
        pyproject = workspace_root / folder / "pyproject.toml"
        name: Optional[str] = None
        if pyproject.exists():
            name = pyproject_toml.name(pyproject)
        elif folder in detached:
            name = detached[folder].distribution or folder
        if name:
            projects[name] = repository
    return dict(sorted(projects.items()))


@functools.lru_cache(maxsize=None)
def is_private_repo(repository: GitHubRepository) -> bool:
    """Return True iff ``repository`` is a private GitHub repository.

    Queries ``gh api repos/<owner>/<name> --jq .private`` with a 5 s timeout.
    Falls back to True (assume private) on any error — gh missing, not
    authenticated, network down, repo doesn't exist, etc. The result is
    cached for the process so a single scaffold run makes at most one API
    call per distinct repository.

    Why the conservative fallback: a previous version defaulted to False
    ("assume public") so projects with only public deps wouldn't carry a
    useless token-minting step. That backfired when a workstation without
    ``gh`` regenerated CI for projects that DO have private deps — every
    re-scaffold silently stripped the ``Mint GitHub App token`` step from
    every ``ci.yml``, and the breakage only surfaced on the next CI run.
    Defaulting to True keeps the token step in place by default; the cost
    on a runner that's missing the ``<OWNER>_APP_*`` secrets is one no-op step,
    not a broken build. Set the secrets at the org level once and forget.
    """
    try:
        cp = subprocess.run(
            ["gh", "api", f"repos/{repository.owner}/{repository.name}", "--jq", ".private"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return True
    return cp.stdout.strip().lower() == "true"


def scaffold(project_name: Optional[str], force: bool, jenkins_url: str) -> None:
    """Write the CI files of ``project_name`` — of every project with a ``pyproject.toml`` when it is ``None``,
    aisland included, so its own CI follows the templates it renders for everyone else. A file that exists is kept
    unless ``force``; a Jenkinsfile the Jenkins at ``jenkins_url`` rejects is refused before it is written."""
    workspace_root = get_workspace_root()
    if project_name is None:
        for child in sorted(workspace_root.iterdir()):
            if child.is_dir() and (child / "pyproject.toml").exists():
                scaffold(child.name, force, jenkins_url)
        return

    project_path = workspace_root / project_name
    if not project_path.exists() or not project_path.is_dir():
        raise AislandError(f"Project directory '{project_name}' not found at {project_path}.")
    if not (project_path / "pyproject.toml").exists():
        raise AislandError(f"pyproject.toml not found in {project_name}. Cannot scaffold.")

    facts = project_facts(workspace_root, project_name, jenkins_url)
    rendered = ci_templates.render(ci_templates.choice(workspace_root, project_name), facts)
    for relative, content in rendered.items():
        _write(project_path / relative, content, relative, force, jenkins_url)


def project_facts(workspace_root: Path, project_name: str, jenkins_url: str) -> ci_templates.Facts:
    """What a template reads about ``project_name``: its ``pyproject.toml``, the workspace's COMMITTED selections, and
    each internal dependency's release on PyPI (``ReleaseProbeError`` when PyPI cannot be asked, before any file of
    the project is written)."""
    project_path = workspace_root / project_name
    pyproject_path = project_path / "pyproject.toml"
    distribution = get_project_metadata(pyproject_path)["name"]
    package = package_folder(project_path, distribution)
    console.echo(f"Scaffolding CI/CD for {project_name} (Package: {package})...")

    # Internal dependencies — discovered, not hardcoded: every workspace project whose remote is on GitHub, by its
    # distribution name, with the repository a runner installs it from. Adding a new workspace project is a
    # zero-aisland-edit operation.
    github_projects = discover_github_projects(workspace_root)
    selected = selected_extras(workspace_root, pyproject_path)
    # `dev` is listed first and unconditionally (CI always needs the test tooling), then deduped: since 2026-10-20 the
    # `extras` selection offers `dev` like any other extra, so a workspace that selected it would otherwise emit
    # `.[dev,dev,local]`.
    extras = list(dict.fromkeys(["dev", *selected]))
    data = pyproject_toml.load(pyproject_path)

    # CI asks ONE question per internal dependency — is it RELEASED on PyPI? — and nothing about the workspace's
    # sources (`workspace.toml`) (user ruling 2026-09-26: "it should always point to released packages, and to GitHub
    # for non-released ones"). Released (a final, non-yanked version whose metadata points at github.com/<its owner>/ —
    # `pypi.released_on_pypi`) -> the dep drops out of the pre-install loop and `.[dev]` resolves the release from
    # PyPI. Not released -> the runner installs GitHub `main` (`DEFAULT_REF`). A source (`local` / `github` / `pypi`,
    # any `ref`) describes ONE machine's checkout and venv; before this rule it also decided the runner's install
    # line, so `aisland workspace configure source set` rewrote every dependent's committed CI files and a released
    # package developed locally (liquifai) was built from GitHub `main` by every consumer.
    internal = internal_dependencies(workspace_root, data, distribution, selected, github_projects)
    from_github = [dep for dep in internal if not released_on_pypi(dep, github_projects[dep].owner)]
    # A project that does not live on GitHub (a company's Bitbucket server) never names a project that does in its CI
    # files: they would reveal the workspace it is developed in. Its unreleased dependencies come from the wheels it
    # commits (`aisland wheels build --extra`), installed through that extra; without them it is refused.
    if from_github and project_name not in project_repositories(workspace_root):
        extras = list(dict.fromkeys([*extras, _unreleased_from_wheels(project_name, pyproject_path, from_github)]))
        from_github = []
    token_owner = _token_owner(project_name, from_github, github_projects)

    # A secret name holds letters, digits and underscores only: `acme-labs` -> `ACME_LABS_APP_ID`.
    return ci_templates.Facts(
        project=project_name,
        title=project_name.capitalize(),
        distribution=distribution,
        package=package,
        extras=extras,
        browser=PLAYWRIGHT_BROWSER if "playwright" in dev_extra_names(data) else None,
        slow_tests=registers_slow_marker(data),
        node_tests=node_test_folders(project_path),
        unreleased=[ci_templates.Dependency(dep, github_projects[dep].url, DEFAULT_REF) for dep in from_github],
        unreleased_owners=", ".join(sorted({github_projects[dep].owner for dep in from_github})),
        token_owner=token_owner,
        token_secret=re.sub(r"[^A-Z0-9]", "_", token_owner.upper()) if token_owner else None,
        stage_limits=dict(STAGE_LIMITS),
        runner_limits={stage: runner_limit(stage) for stage in STAGE_LIMITS},
        actions=dict(GH_ACTIONS),
        timed_out_message=TIMED_OUT_MESSAGE,
        workspace=get_workspace_name(workspace_root),
        jenkins_url=jenkins_url,
    )


def package_folder(project_path: Path, distribution: str) -> str:
    """The folder of the project's package, the one CI measures: the distribution's name; else that name without its
    dashes (a distribution name may hold dashes, an import name may not) when it holds an ``__init__.py``, tried
    before the scan so ``examples/`` is never picked; else the first folder holding an ``__init__.py`` that is no
    tests, examples, notebooks, docs or scripts folder; else the distribution's name."""
    if (project_path / distribution).exists():
        return distribution
    dashless = distribution.replace("-", "")
    if (project_path / dashless / "__init__.py").exists():
        return dashless
    for folder in project_path.iterdir():
        if folder.is_dir() and (folder / "__init__.py").exists() and folder.name not in _NOT_PACKAGES:
            return folder.name
    return distribution


def selected_extras(workspace_root: Path, pyproject_path: Path) -> List[str]:
    """The extras of the project its CI installs beside `dev`: what it offers for each selection (aisland's `extras`,
    and each `[tool.aisland.selection.<name>]` a project declares) intersected with that selection's COMMITTED choice
    (`[workspace] <name>` of `workspace.toml`).

    Committed-only, as the release question in :func:`project_facts` is: this machine's `aisland.<name>.selected` must
    never reach a tracked build file, or one developer's "I only use torch" would silently stop every runner from
    testing the Keras backend — and a runner that stops installing mlflow silently stops running the tests that need
    it, which SKIP rather than fail. A project offering nothing gets the historical `.[dev]` unchanged.
    """
    extras: List[str] = []
    for axis in sel_axes(workspace_root):
        extras += sel_extras_for(sel_declared(pyproject_path, axis), sel_committed(workspace_root, axis.name))
    return extras


def internal_dependencies(
    workspace_root: Path,
    data: pyproject_toml.Table,
    distribution: str,
    selected: Sequence[str],
    github_projects: Dict[str, GitHubRepository],
) -> List[str]:
    """The internal dependencies CI pre-installs for the project whose ``pyproject.toml`` is ``data``, each after the
    ones it needs: the closure of exactly what CI's install steps resolve, nothing more.

    When CI installs a direct dependency like `confluid` with --no-deps, `.[dev]` still needs confluid's own BASE
    dependencies (loggair), and an unreleased internal distribution is not on PyPI, so uv fails unless those are
    pre-installed too. What each node contributes (2026-09-02, replaces "walk every extras group of every project",
    which pre-installed packages nothing would ever import):

    - the ROOT project: its base dependencies PLUS the extras its CI installs — `.[dev,<selected axis extras>]` plus
      the best-effort `.[notebook]` step;
    - a TRANSITIVE dependency: its base dependencies PLUS only the extras the incoming requirement named
      (`matrainer[keras]` walks matrainer's keras extra). Its OTHER extras are never requested by any install step
      (each dependency installs bare, and `.[dev]` resolves only its base dependencies), so they add no pre-install —
      the old rule made the CI of a project depending on one other project install everything that project's dev
      extra reaches.

    Specs are parsed with packaging.Requirement: the regex split it replaces could not parse
    `matrainer[torch]>=0.1.0` and silently dropped the spec. A project never pre-installs itself: a dependency cycle
    back to the root (two projects requiring each other through a dev extra) would otherwise install the root's own
    main branch over the very checkout under test.
    """
    # BFS over (dependency, requested extras) pairs: the same dependency may be reached with different extras, and
    # each set can name different siblings.
    root_extras = frozenset(["dev", "notebook", *selected])
    work: List[Tuple[str, frozenset[str]]] = [
        (req.name, frozenset(req.extras))
        for req in _internal_requirements(data.get("project", {}), root_extras, github_projects)
    ]
    visited: Set[Tuple[str, frozenset[str]]] = set()
    seen: Set[str] = set()
    while work:
        item = work.pop()
        if item in visited:
            continue
        visited.add(item)
        dep, dep_extras = item
        seen.add(dep)
        table = _project_table(_workspace_folder(workspace_root, dep) / "pyproject.toml")
        work.extend(
            (req.name, frozenset(req.extras)) for req in _internal_requirements(table, dep_extras, github_projects)
        )
    seen.discard(distribution)

    # Base dependencies suffice for the order: every install runs --no-deps anyway.
    prereqs: Dict[str, Set[str]] = {}
    for name in seen:
        table = _project_table(_workspace_folder(workspace_root, name) / "pyproject.toml")
        prereqs[name] = {
            req.name
            for req in _internal_requirements(table, frozenset(), github_projects)
            if req.name in seen and req.name != name
        }
    return _dependencies_first(prereqs)


def _project_table(pyproject: Path) -> pyproject_toml.Table:
    """The [project] table of one pyproject.toml; {} when missing or unparseable (a detached submodule's folder is
    empty and contributes nothing)."""
    table = pyproject_toml.read(pyproject).get("project", {})
    return table if isinstance(table, dict) else {}


def _internal_requirements(
    project_table: pyproject_toml.Table, extras: frozenset[str], github_projects: Dict[str, GitHubRepository]
) -> List[Requirement]:
    """The internal requirements one node contributes: its base dependencies plus exactly the requested extras."""
    specs: List[str] = list(project_table.get("dependencies") or [])
    optional = project_table.get("optional-dependencies") or {}
    for extra in extras:
        specs.extend(optional.get(extra) or [])
    requirements: List[Requirement] = []
    for spec in specs:
        try:
            requirement = Requirement(spec)
        except InvalidRequirement:
            continue
        if requirement.name in github_projects:
            requirements.append(requirement)
    return requirements


def _workspace_folder(workspace_root: Path, name: str) -> Path:
    """The workspace folder of the internal project ``name``: its own name, else that name without its dashes."""
    if (workspace_root / name).is_dir():
        return workspace_root / name
    return workspace_root / name.replace("-", "")


def _dependencies_first(prereqs: Dict[str, Set[str]]) -> List[str]:
    """The names of ``prereqs``, each after the ones it needs (Kahn's algorithm, ties by name), so loggair installs
    before confluid; a cycle's names come last, sorted."""
    ordered: List[str] = []
    remaining = {name: set(needs) for name, needs in prereqs.items()}
    while remaining:
        ready = sorted(name for name, needs in remaining.items() if not needs)
        if not ready:
            ordered.extend(sorted(remaining))
            break
        for name in ready:
            ordered.append(name)
            del remaining[name]
            for needs in remaining.values():
                needs.discard(name)
    return ordered


def _unreleased_from_wheels(project_name: str, pyproject_path: Path, unreleased: Sequence[str]) -> str:
    """The extra whose wheels hold every one of ``unreleased`` for a project off GitHub, which its CI installs instead
    of naming GitHub; refused, naming the ones no wheel holds and the command that ships them, before anything is
    written."""
    found = wheels_extra.remembered_extra(pyproject_path)
    covered: set[str] = set()
    if found is not None:
        data = pyproject_toml.load(pyproject_path)
        for spec in (data.get("project", {}).get("optional-dependencies", {}) or {}).get(found[0], []) or []:
            try:
                covered.add(canonicalize_name(Requirement(str(spec)).name))
            except InvalidRequirement:
                continue
    missing = [dep for dep in unreleased if canonicalize_name(dep) not in covered]
    if found is not None and not missing:
        return found[0]
    names = ", ".join(missing)
    verb, them = ("is", "it") if len(missing) == 1 else ("are", "them")
    extra = found[0] if found is not None else "wheels"
    raise AislandError(
        f"{project_name} does not live on GitHub, so its CI files never name a project that does, and {names} {verb} "
        f"not released on PyPI. Ship {them} as wheels with the project — aisland wheels build {project_name} --extra "
        f"{extra} --commit (the extra listed in [tool.aisland] manual-extras) — then scaffold again."
    )


def _token_owner(
    project_name: str, from_github: Sequence[str], github_projects: Dict[str, GitHubRepository]
) -> Optional[str]:
    """The GitHub owner whose private repositories CI clones, for whom it mints an app token; None when it clones none.

    The default GITHUB_TOKEN reaches only the current repository. Only the dependencies CI clones from GitHub count —
    a released one comes from the public index and must not keep the token step alive on its own — and
    :func:`is_private_repo` takes one it cannot ask about for private. One app token reaches ONE owner's repositories,
    so private dependencies of two owners are refused rather than half-authorized.
    """
    private_owners = sorted(
        {github_projects[dep].owner for dep in from_github if is_private_repo(github_projects[dep])}
    )
    if len(private_owners) > 1:
        raise AislandError(
            f"{project_name}'s CI would clone private repositories of {len(private_owners)} GitHub owners "
            f"({', '.join(private_owners)}); one app token reaches one owner's repositories. Release the "
            "dependencies of all but one owner, or make their repositories public."
        )
    return private_owners[0] if private_owners else None


def _write(path: Path, content: str, desc: str, force: bool, jenkins_url: str) -> None:
    """Write one CI file, keeping one that exists unless ``force``. A Jenkinsfile is checked by the Jenkins linter
    BEFORE it is written, so a broken template is caught at scaffold time, not when a build fails hours later."""
    if path.exists() and not force:
        console.echo(f"  [SKIP] {desc} already exists. Use --force to overwrite.")
        return
    if desc.startswith("Jenkinsfile") and not _validates(content, desc, jenkins_url):
        console.echo(click.style(f"  [ABORT] Not writing {desc} — fix the template.", fg="red"))
        raise AislandError(f"Template produced invalid {desc}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    console.echo(f"  [OK] Created {desc}")


def _validates(content: str, desc: str, jenkins_url: str) -> bool:
    """POST the content to Jenkins' declarative linter. True when the file validates, or when Jenkins is not running
    (nothing can check it then); False only when Jenkins rejects the file."""
    try:
        # A CSRF crumb is required for every POST, even with security disabled.
        crumb_req = urllib.request.Request(f"{jenkins_url}/crumbIssuer/api/json")
        with urllib.request.urlopen(crumb_req, timeout=3) as resp:
            crumb_data = json.loads(resp.read().decode())
        crumb_header = {crumb_data["crumbRequestField"]: crumb_data["crumb"]}
    except (urllib.error.URLError, OSError, TimeoutError, KeyError):
        console.echo(f"  [WARN] Jenkins not reachable — skipped validation of {desc}.")
        return True

    try:
        body = urllib.parse.urlencode({"jenkinsfile": content}).encode()
        req = urllib.request.Request(
            f"{jenkins_url}/pipeline-model-converter/validate",
            data=body,
            headers={
                **crumb_header,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = resp.read().decode().strip()
    except urllib.error.URLError as e:
        console.echo(f"  [WARN] Linter call failed ({e}); skipped validation of {desc}.")
        return True

    if result.startswith("Jenkinsfile successfully validated"):
        return True
    console.echo(click.style(f"  [FAIL] {desc} did not validate:\n{result}", fg="red"))
    return False
