import base64
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import click
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from aisland import ci_templates, failures, gitcmd, provision
from aisland import pyproject as pyproject_toml
from aisland import supervisor
from aisland.downloads import TIMEOUT, download
from aisland.errors import AislandError
from aisland.installers import install_brew_packages
from aisland.paths import BIN_DIR
from aisland.projects import get_workspace_root
from aisland.selection import PLAYWRIGHT_BROWSER
from aisland.selection import axes as sel_axes
from aisland.selection import declared as sel_declared
from aisland.selection import dev_extra_names
from aisland.selection import extras_for as sel_extras_for
from aisland.selection import read_committed as sel_committed
from aisland.services.base import BaseService
from aisland.services.jenkins_java import JenkinsJava, choose_java, find_runtimes, newest_versioned_formula
from aisland.sources import DEFAULT_REF, GitHubRepository, project_repositories, released_on_pypi

logger = logging.getLogger(__name__)

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

#: When the LOCAL orchestrator (`<workspace>.local`) builds every project on its own: 01:00 every night (user
#: ruling 2026-10-06), so each project's slow run happens and the test-run statistics fill (`aisland tests report`).
#: Jenkins reads it in the server's time zone; a machine asleep at that minute misses the night (Jenkins does not catch
#: up a missed timer). The GitHub-backed orchestrator gets none: it builds what is pushed.
NIGHTLY_CRON = "0 1 * * *"
#: When the `<workspace>.nightly-check` job reports the nightly runs (`aisland tests nightly`): 09:30 on weekdays (user
#: choice 2026-10-06), so a Monday's report covers Saturday's, Sunday's and Monday's nights.
NIGHTLY_CHECK_CRON = "30 9 * * 1-5"

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


def runner_limit(stage: str) -> int:
    """The `timeout-minutes` of the GitHub job running ``stage``."""
    return STAGE_LIMITS[stage] * RUNNER_LIMIT_FACTOR


#: The one line a stage past its limit writes (Groovy interpolation); `nightly._TIMED_OUT` reads it from the console.
TIMED_OUT_MESSAGE = "${stage} timed out after ${minutes} min - its limit in Jenkinsfile.local (STAGE_LIMITS in aisland/services/jenkins.py)"
#: The `limited` helper `Jenkinsfile.local` defines above `pipeline {` is aisland's template
#: `templates/ci/_limited.groovy.jinja`, which writes this message; each long stage's steps run inside it. Measured 2026-10-15
#: on a probe job (a 1 min limit around a shell ignoring SIGINT): the stage FAILED at 80 s, the build FAILURE, the
#: stage's post block ran, the next stages were skipped, the console ended with the ERROR line; Jenkins printed
#: `After 20s process did not stop` and the hung process lingered — the stage ends anyway, and the orchestrator sees
#: `<job> completed: FAILURE`, the path every failing project takes. Abort clicked before the limit is rethrown and
#: stays an abort. Sandbox: `System.currentTimeMillis` and `Closure.call` are in script-security's generic whitelist,
#: and a catch clause's class name is never checked by it.


def registers_slow_marker(pyproject_data: pyproject_toml.Table) -> bool:
    """True when the project registers the `slow` pytest marker — its declaration that it has a slow run."""
    markers = pyproject_data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
    return any(str(m).split(":", 1)[0].strip() == "slow" for m in markers)


#: Folders a Node package's tests never live in: installed packages, and hidden folders (`.vscode-test`, `.git`).
_NOT_NODE_SOURCES = ("node_modules",)


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


def _project_has_slow_tests(project_dir: Path) -> bool:
    """`registers_slow_marker` of the project's `pyproject.toml`; False for a folder without one (a detached project)."""
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return False
    return registers_slow_marker(pyproject_toml.load(pyproject))


def discover_github_projects(workspace_root: Path) -> Dict[str, GitHubRepository]:
    """Every workspace project whose remote is on GitHub, by its PyPI distribution name, with that repository.

    The remote is the project's own (``.gitmodules``, or a clone's ``origin``: :func:`aisland.sources.project_repositories`),
    so the owner is whoever's the workspace's projects are — no organisation is written into aisland. The name is the
    ``[project] name`` of the project's ``pyproject.toml``; a project without one is skipped (it is no Python project
    aisland installs) unless it is DETACHED (a ``github`` / ``pypi`` source empties its folder), whose name is the
    committed source's ``distribution``, else its folder — omitting it would drop the git-URL pin a ``github`` source
    exists to express. A project off GitHub (a Bitbucket server) is never one CI installs from GitHub.

    Replaces a hard-coded list: a new project (a new submodule + pyproject) is picked up on the next
    ``aisland jenkins scaffold`` with no aisland edit.
    """
    from aisland.sources import read_committed_sources

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


def _unreleased_from_wheels(project_name: str, pyproject_path: Path, unreleased: Sequence[str]) -> str:
    """The extra whose wheels hold every one of ``unreleased`` for a project off GitHub, which its CI installs instead
    of naming GitHub; refused, naming the ones no wheel holds and the command that ships them, before anything is
    written."""
    from aisland import wheels_extra

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


def pyproject_to_workspace_dir(pyproject_name: str, initialized_set: set) -> str:
    """Map a pyproject distribution name to its workspace directory name.

    Handles two conventions:
      1. Identity (most projects): ``confluid`` -> ``confluid``.
      2. Dashed PyPI name -> dashless dir: ``acme-tools`` -> ``acmetools``.

    Returns the input unchanged if no rule matches (caller decides whether
    that unknown name is a problem).
    """
    if pyproject_name in initialized_set:
        return pyproject_name
    dashless = pyproject_name.replace("-", "")
    if dashless in initialized_set:
        return dashless
    return pyproject_name


class JenkinsService(BaseService):
    """Jenkins CI/CD Server."""

    URL = "https://get.jenkins.io/war-stable/latest/jenkins.war"
    REPO = "jenkinsci/plugin-installation-manager-tool"
    #: The port Jenkins starts on unless `--port` says otherwise; everything else asks :meth:`url`.
    DEFAULT_PORT = 9080

    PLUGINS = [
        "workflow-multibranch",
        "workflow-aggregator",
        "git",
        "cloudbees-folder",
        "build-monitor-plugin",
        "view-job-filters",
        "dashboard-view",
        "configuration-as-code",
        "credentials-binding",
        "warnings-ng",
        "coverage",
        "junit",
        "pipeline-stage-view",
    ]

    def __init__(self) -> None:
        super().__init__("jenkins")
        self.war_file = BIN_DIR / "jenkins.war"
        # Find any existing manager jar in the bin directory
        self.manager_jar = self._find_manager_jar()

    def _find_manager_jar(self) -> Path:
        """Find the most recent manager jar in the bin directory."""
        jars = list(BIN_DIR.glob("jenkins-plugin-manager-*.jar"))
        if jars:
            # Sort by version or modification time if multiple exist
            return sorted(jars)[-1]
        return BIN_DIR / "jenkins-plugin-manager.jar"

    def _get_latest_manager_info(self) -> dict:
        """Fetch the latest release info from GitHub API."""
        api_url = f"https://api.github.com/repos/{self.REPO}/releases/latest"
        headers = {"User-Agent": "aisland"}
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                data = json.loads(response.read().decode())
                if not isinstance(data, dict):
                    return {}
                return data
        except (OSError, ValueError):  # no answer, or no JSON
            # Silence this error if we already have a jar file, as we can still proceed.
            if not self.manager_jar.exists():
                click.echo(
                    "  - Error fetching latest Jenkins Plugin Manager info from GitHub API.",
                    err=True,
                )
            return {}

    def ensure_war(self) -> bool:
        """Download the Jenkins WAR unless it is already in the bin dir; return whether it is there now."""
        self.war_file.parent.mkdir(exist_ok=True)
        if self.war_file.exists():
            return True
        click.echo(f"Downloading Jenkins from {self.URL}...")
        try:
            download(self.URL, self.war_file)
        except OSError as e:
            failures.report(f"Error downloading Jenkins: {e}")
            return False
        click.echo("  - Jenkins download complete.")
        return True

    def resolve_java(self) -> JenkinsJava:
        """Ask the downloaded WAR which installed JDK it runs on (see :mod:`aisland.services.jenkins_java`)."""
        return choose_java(self.war_file, find_runtimes())

    def java_asking(self) -> Path:
        """The `java` Jenkins runs on, by full path — the one it accepts installed first, once the user agrees.

        Never the bare `java` on PATH: a shell profile puts the newest JDK there, and Jenkins
        refuses any Java its launcher does not name — a refusal the supervisor would only
        retry three times and record in its log. Without an accepted Java and without a yes (no terminal, or
        "no") the command refuses with the formula that installs one, having installed nothing.
        """
        choice = self.resolve_java()
        if choice.runtime is None:
            if provision.this_platform() == "brew" and provision.agree(
                f"{choice.problem()}. Install one ({choice.remedy()})?"
            ):
                self.provision_java()
                choice = self.resolve_java()
            if choice.runtime is None:
                raise AislandError(choice.refusal().removeprefix("Error: "))
        click.echo(choice.describe())
        return choice.runtime.java

    def ensure_war_asking(self) -> bool:
        """The Jenkins WAR, downloaded once the user agrees; without a yes the command refuses, downloading nothing."""
        if self.war_file.exists():
            return True
        missing = f"Jenkins needs its WAR ({self.URL}), which is not downloaded yet."
        if not provision.agree(f"{missing} Download it?"):
            raise AislandError(
                f"{missing} Run the command in a terminal and answer yes, or: aisland workspace provision install"
            )
        return self.ensure_war()

    def provision_java(self) -> None:
        """Install, through Homebrew, the newest Java this Jenkins accepts — unless one already is.

        The WAR names what it supports only when a JDK runs its launcher, so on a machine with
        no JDK that can (none at all, or only Java 8) the newest `openjdk@N` formula is
        installed first to ask with; when the WAR refuses that one too, the formula it names is
        installed next. Never exits: a failed WAR download or a machine left without an
        accepted Java is reported, and the rest of setup goes on.
        """
        click.echo("==> Choosing the Java Jenkins runs on...")
        if not self.ensure_war():
            click.echo("  - Skipped: without the Jenkins WAR there is no asking which Java it runs on.")
            return
        choice = self.resolve_java()
        installed: set = set()
        while choice.runtime is None:
            formula = choice.formula or newest_versioned_formula()
            if formula is None or formula in installed:
                break
            click.echo(f"  - {choice.problem()}.")
            install_brew_packages({formula})
            installed.add(formula)
            choice = self.resolve_java()
        if choice.runtime is None:
            click.echo(choice.refusal())
            return
        click.echo(f"  - {choice.describe()}.")

    def setup(self) -> None:
        """Download Jenkins binary and the latest plugin manager."""
        if not self.ensure_war_asking():
            return  # the download's failure is reported: the command exits 1 (aisland.failures)

        # Check for latest manager version
        info = self._get_latest_manager_info()
        if info:
            tag = info.get("tag_name", "")
            assets = info.get("assets", [])
            jar_asset = next((a for a in assets if a["name"].endswith(".jar")), None)

            if jar_asset:
                latest_jar_name = jar_asset["name"]
                latest_jar_path = BIN_DIR / latest_jar_name
                download_url = jar_asset["browser_download_url"]

                if not latest_jar_path.exists():
                    click.echo(f"Downloading Jenkins Plugin Manager {tag}...")
                    try:
                        download(download_url, latest_jar_path)
                        click.echo(f"  - Plugin Manager {tag} download complete.")
                        # Remove old versions
                        for old_jar in BIN_DIR.glob("jenkins-plugin-manager-*.jar"):
                            if old_jar != latest_jar_path:
                                old_jar.unlink()
                    except OSError as e:
                        failures.report(f"Error downloading Plugin Manager: {e}")

                self.manager_jar = latest_jar_path

        if not self.manager_jar.exists():
            failures.report(
                "  - Error: Plugin Manager could not be discovered or downloaded.",
            )

    def _are_plugins_installed(self) -> bool:
        """Check if all required plugins are present in the plugins directory."""
        plugins_dir = self.service_data_dir / "plugins"
        if not plugins_dir.exists():
            return False

        missing = []
        for plugin in self.PLUGINS:
            jpi = plugins_dir / f"{plugin}.jpi"
            hpi = plugins_dir / f"{plugin}.hpi"
            plugin_folder = plugins_dir / plugin

            if not (
                jpi.exists()
                or hpi.exists()
                or plugin_folder.exists()
                or (plugins_dir / f"{plugin}.jpi.pinned").exists()
            ):
                missing.append(plugin)

        if missing:
            click.echo(f"  - Missing plugins: {', '.join(missing)}")
            return False
        return True

    def setup_plugins(self) -> bool:
        """Download Jenkins plugins using the official plugin-manager tool.
        Returns True if plugins were downloaded, False if they were already present.
        """
        installed = self._are_plugins_installed()
        if installed:
            logger.debug("every required Jenkins plugin is already in %s", self.service_data_dir / "plugins")
            return False

        plugins_dir = self.service_data_dir / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)

        # Create plugins.txt for the manager
        plugins_txt = self.service_data_dir / "plugins.txt"
        plugins_txt.write_text("\n".join(self.PLUGINS))

        cmd = [
            str(self.java_asking()),
            "-jar",
            str(self.manager_jar),
            "--war",
            str(self.war_file),
            "--plugin-file",
            str(plugins_txt),
            "--plugin-download-directory",
            str(plugins_dir),
        ]

        try:
            # Run the manager tool. It handles dependencies and version resolution.
            # We capture output to keep it clean unless there's an error.
            click.echo("  - Downloading necessary Jenkins plugins...")
            logger.debug("running %s", " ".join(cmd))
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            if "Installed" in result.stdout or "Done" in result.stdout:
                click.echo("  - Jenkins plugins and dependencies synchronized.")
            return True
        except subprocess.CalledProcessError as e:
            failures.report(f"  - Error installing plugins: {e.stderr}")
            return False

    def _has_remote_pipelines(self) -> bool:
        """Check if any initialized pipelines require remote Git credentials."""
        jobs_dir = self.service_data_dir / "jobs"
        if not jobs_dir.exists():
            return False

        for project_job_dir in jobs_dir.iterdir():
            if not project_job_dir.is_dir():
                continue
            config_xml = project_job_dir / "config.xml"
            if config_xml.exists():
                try:
                    tree = ET.parse(config_xml)
                    root = tree.getroot()
                    # Look for the remote URL in the GitSCMSource configuration
                    remote_elem = root.find(".//remote")
                    if remote_elem is not None and remote_elem.text:
                        url = remote_elem.text
                        # Local paths start with / (Unix) or X: (Windows)
                        is_local = url.startswith("/") or (len(url) > 1 and url[1] == ":")
                        if not is_local:
                            return True
                except (OSError, ET.ParseError):  # a job file Jenkins is still writing: no remote to read
                    pass
        return False

    def check_credentials_and_print_instructions(self, port: int) -> None:
        """Check if Git credentials are configured and print instructions if not."""
        # Only warn if there are actually remote pipelines that need credentials
        if not self._has_remote_pipelines():
            return

        config_file = self.service_data_dir / "credentials.xml"
        admin_pass_file = self.service_data_dir / "secrets/initialAdminPassword"

        has_creds = False
        if config_file.exists():
            try:
                tree = ET.parse(config_file)
                root = tree.getroot()
                if (
                    root.find(".//com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl") is not None
                    or root.find(".//com.cloudbees.jenkins.plugins.sshcredentials.impl.BasicSSHUserPrivateKey")
                    is not None
                ):
                    has_creds = True
            except (OSError, ET.ParseError):  # unreadable: treated as no credentials, so the instructions show
                pass

        if not has_creds:
            click.echo("\n" + "=" * 65)
            click.echo(" Jenkins Initial Setup / Missing Credentials ".center(65, "="))
            click.echo("=" * 65)
            click.echo("No Git credentials found. To allow Jenkins to pull code:")
            click.echo("  1. Open Jenkins at " + click.style(f"http://localhost:{port}", fg="blue", bold=True))
            if admin_pass_file.exists():
                password = admin_pass_file.read_text().strip()
                click.echo(f"  2. Unlock using password: {click.style(password, bg='black', fg='white')}")
            click.echo("  3. Navigate to: Manage Jenkins -> Credentials -> System ->")
            click.echo("     Global credentials -> Add Credentials")
            click.echo("  4. Add your Git username/password or SSH key.")
            click.echo(
                "     "
                + click.style(
                    "CRITICAL: You MUST set the ID field to 'aisland-github'",
                    fg="red",
                    bold=True,
                )
            )
            click.echo(
                "  5. Run " + click.style("aisland jenkins init", fg="green") + " to install plugins and populate jobs."
            )
            click.echo("=" * 65 + "\n")

    def launch(self, port: int = DEFAULT_PORT, background: bool = False) -> None:
        """Start the Jenkins server."""
        self.setup()
        java = self.java_asking()
        click.echo(f"Jenkins: http://localhost:{port}")
        self.check_credentials_and_print_instructions(port)

        # Suppress Java 25+ warnings for terminal deprecation and illegal access
        jvm_args = [
            "-Dhudson.plugins.git.GitSCM.ALLOW_LOCAL_CHECKOUT=true",
            "--add-opens=java.base/java.lang=ALL-UNNAMED",
            "--add-opens=java.base/java.io=ALL-UNNAMED",
            "--add-opens=java.base/java.util=ALL-UNNAMED",
            "--add-opens=java.base/java.util.concurrent=ALL-UNNAMED",
            "--add-opens=java.base/sun.reflect.annotation=ALL-UNNAMED",
            "--add-opens=java.base/java.lang.reflect=ALL-UNNAMED",
            "--add-opens=java.rmi/sun.rmi.transport=ALL-UNNAMED",
        ]

        # Start Jenkins without skipping the setup wizard to ensure security is enabled
        cmd = [str(java), *jvm_args, "-jar", str(self.war_file), f"--httpPort={port}"]
        workspace_root = get_workspace_root()
        venv_bin = workspace_root / ".venv" / "bin"
        # BIN_DIR (aisland/bin) hosts symlinks to userspace-installed tools like
        # node/npm/npx (see installers.install_userspace_tool). Putting it on
        # Jenkins' PATH means every job inherits these without per-Jenkinsfile
        # plumbing. Order: venv first (its `python`/`uv` shadow any system ones),
        # then aisland-vendored tools, then whatever the parent shell exposed.
        env = {
            "JENKINS_HOME": str(self.service_data_dir),
            "PATH": f"{venv_bin}:{BIN_DIR}:{os.environ.get('PATH', '')}",
        }

        # /login answers 200 as soon as the Jenkins web UI is up (even while
        # the setup wizard is pending), which is exactly "ready" for aisland.
        self.start(cmd, env=env, background=background, readiness={"port": port, "path": "/login"})

    def status(self) -> None:
        """Show extended Jenkins status."""
        super().status()
        admin_pass_file = self.service_data_dir / "secrets/initialAdminPassword"
        if self.is_running() and admin_pass_file.exists():
            password = admin_pass_file.read_text().strip()
            click.echo(f"    Initial Admin Password: {password}")

    def url(self) -> str:
        """Where Jenkins answers: the port it runs on, else :attr:`DEFAULT_PORT` (it is not running, or not here)."""
        ports = self.get_ports()
        return f"http://localhost:{ports[0] if ports else self.DEFAULT_PORT}"

    def info(self) -> None:
        """Show detailed Jenkins configuration."""
        super().info()
        click.echo(f"  URL           : {self.url()}")
        click.echo(f"  JENKINS_HOME  : {self.service_data_dir}")
        click.echo(f"  WAR file      : {self.war_file}")
        if self.war_file.exists():
            click.echo(f"  Java          : {self.resolve_java().describe()}")
        click.echo(f"  Plugin manager: {self.manager_jar}")
        click.echo(f"  Plugins dir   : {self.service_data_dir / 'plugins'}")
        click.echo(f"  Jobs dir      : {self.service_data_dir / 'jobs'}")
        admin_pass_file = self.service_data_dir / "secrets/initialAdminPassword"
        if admin_pass_file.exists():
            password = admin_pass_file.read_text().strip()
            click.echo("  Admin user    : admin")
            click.echo(f"  Admin password: {password}")
        else:
            click.echo("  Admin password: (not yet generated — start Jenkins to create it)")
        creds_file = self.service_data_dir / "credentials.xml"
        click.echo(f"  Credentials   : {creds_file} " + ("(present)" if creds_file.exists() else "(not configured)"))

    def _get_git_remote(self, project_path: Path) -> Optional[str]:
        """Get the Git remote origin URL for a project."""
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (OSError, subprocess.CalledProcessError):  # no origin, or no git
            return None

    def _find_credentials(self, remote_url: str) -> str:
        """Find matching credentials in Jenkins configuration."""
        remote_lower = remote_url.lower()
        is_github = "github" in remote_lower
        expected_id = "aisland-github" if is_github else None

        config_file = self.service_data_dir / "credentials.xml"
        if not config_file.exists():
            if expected_id:
                failures.warn(
                    f"  - Warning: credentials.xml missing. You MUST add your Git credential with ID '{expected_id}'."
                )
            return expected_id or ""

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()

            # Extract all credential IDs
            cred_ids = []
            for entry in root.findall(".//com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl"):
                cred_ids.append(entry.findtext("id") or "")
            for entry in root.findall(".//com.cloudbees.jenkins.plugins.sshcredentials.impl.BasicSSHUserPrivateKey"):
                cred_ids.append(entry.findtext("id") or "")

            if expected_id:
                if expected_id not in cred_ids:
                    failures.warn(
                        f"  - Warning: Credential with ID '{expected_id}' not found in credentials.xml! "
                        f"You MUST name your Jenkins credential exactly '{expected_id}'."
                    )
                return expected_id

            # Fallback for non-github: return the first available if any
            if cred_ids:
                return cred_ids[0]

        except (OSError, ET.ParseError) as e:
            failures.report(f"  - Error parsing credentials.xml: {e}")

        return expected_id or ""

    def remove_pipelines(self, project_name: Optional[str] = None) -> None:
        """Remove one or all project pipelines from Jenkins."""
        jobs_dir = self.service_data_dir / "jobs"
        if not jobs_dir.exists():
            click.echo("No Jenkins jobs directory found.")
            return

        if project_name:
            project_job_dir = jobs_dir / project_name
            if project_job_dir.exists():
                click.echo(f"Removing Jenkins pipeline for project '{project_name}'...")
                shutil.rmtree(project_job_dir)
                click.echo(f"  - Removed: {project_name}")
            else:
                failures.report(
                    f"  - Error: Jenkins job for project '{project_name}' not found.",
                )
        else:
            click.echo("Removing ALL project pipelines from Jenkins...")
            for project_job_dir in jobs_dir.iterdir():
                if project_job_dir.is_dir() and (project_job_dir / "config.xml").exists():
                    shutil.rmtree(project_job_dir)
                    click.echo(f"  - Removed: {project_job_dir.name}")
            click.echo("All project pipelines removed. Please restart Jenkins if it was already running.")

    def safe_restart(self) -> None:
        """Issue a safeRestart command to Jenkins."""
        if not self.is_running():
            return

        click.echo("  - Issuing Safe Restart to Jenkins (waiting for active jobs)...")
        admin_pass_file = self.service_data_dir / "secrets/initialAdminPassword"

        url = self.url()

        if admin_pass_file.exists():
            password = admin_pass_file.read_text().strip()
            auth = base64.b64encode(f"admin:{password}".encode()).decode()
            auth_str = f"Basic {auth}"

            # Get Crumb
            crumb_headers = {}
            try:
                crumb_url = f"{url}/crumbIssuer/api/json"
                req = urllib.request.Request(crumb_url)
                req.add_header("Authorization", auth_str)
                with urllib.request.urlopen(req, timeout=5) as response:
                    data = json.loads(response.read().decode())
                    crumb_headers[data["crumbRequestField"]] = data["crumb"]
            except (OSError, ValueError, KeyError):  # no crumb issuer: the restart is tried without one
                pass

            try:
                req = urllib.request.Request(url + "/safeRestart", method="POST")
                req.add_header("Authorization", auth_str)
                for k, v in crumb_headers.items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=5):
                    pass
                click.echo("  - Safe Restart initiated. Jenkins will reload soon.")
                return
            except (OSError, ValueError) as e:  # no answer, a refusal (HTTPError is an OSError), no JSON
                click.echo(f"  - Safe Restart API failed: {e}. Falling back to process restart.")
        else:
            click.echo("  - Missing API credentials. Falling back to process restart.")

        self.stop()
        self.launch(background=True)

    def _create_orchestrator_job(self, local: bool, initialized_projects: List[str]) -> None:
        """Create an orchestrator job that builds all discovered projects in topological order."""
        from aisland.projects import get_install_order, get_workspace_name, get_workspace_root

        workspace_root = get_workspace_root()
        install_order = get_install_order(workspace_root)
        base_name = get_workspace_name()

        # `initialized_projects` is a list of Jenkins job dir names, which follow the
        # workspace directory names (e.g. "loggair", "recordstream") — NOT the pyproject
        # distribution names (which may differ: "loggair", "recordstream"). Build a
        # lookup that maps both forms to the canonical workspace-dir form so we can
        # match either a pyproject name or a dir name against initialized_projects.
        initialized_set = set(initialized_projects)

        def _to_dir_name(pyproject_name: str) -> str:
            return pyproject_to_workspace_dir(pyproject_name, initialized_set)

        depths: dict[str, int] = {}
        for p in install_order:
            dir_name = _to_dir_name(p["name"])
            if dir_name not in initialized_set:
                continue

            deps = []
            for dep in p["dependencies"]:
                dep_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
                dep_dir = _to_dir_name(dep_name)
                if dep_dir in initialized_set and dep_dir != dir_name:
                    deps.append(dep_dir)

            max_dep_depth = -1
            for d in deps:
                max_dep_depth = max(max_dep_depth, depths.get(d, -1))

            depths[dir_name] = max_dep_depth + 1

        phases: dict[int, list[str]] = {}
        for proj_name, depth_val in depths.items():
            phases.setdefault(depth_val, []).append(proj_name)

        stages_str = ""
        for depth in sorted(phases.keys()):
            phase_projects = phases[depth]
            if not phase_projects:
                continue

            stages_str += f"        stage('Phase {depth}') {{\n            parallel {{\n"
            for proj in phase_projects:
                job_name = f"{proj}.local" if local else f"{proj}/main"
                stages_str += f"                stage('Build {proj}') {{\n"
                if local:
                    # In local-orchestration mode we want the run to walk
                    # through every project even when individual builds fail
                    # — `catchError` marks the offending stage red but keeps
                    # the overall orchestrator result at SUCCESS so the user
                    # can see all failures in one shot instead of fixing
                    # them one-by-one. CI keeps the strict propagate: true
                    # so PRs/branches stay gated.
                    stages_str += "                    steps {\n"
                    stages_str += (
                        "                        catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {\n"
                    )
                    # A project with slow tests gets the nightly run's switch (`slowTests()` below); one without has
                    # no Slow Tests stage, and Jenkins would only drop a parameter it does not define.
                    slow_switch = (
                        ", parameters: [booleanParam(name: 'SLOW_TESTS', value: slowTests())]"
                        if _project_has_slow_tests(workspace_root / proj)
                        else ""
                    )
                    stages_str += f"                            build job: '{job_name}', wait: true, propagate: true{slow_switch}\n"
                    stages_str += "                        }\n"
                    stages_str += "                    }\n"
                else:
                    stages_str += (
                        f"                    steps {{ build job: '{job_name}', wait: true, propagate: true }}\n"
                    )
                stages_str += "                }\n"
            stages_str += "            }\n        }\n"

        # `agent none`: the orchestrator only TRIGGERS jobs (`build job:` needs no node). Holding a
        # built-in executor for the whole run left one of the node's two executors for the projects,
        # so every "parallel" phase ran one project at a time (2026-09-13: one project waited 875 s for
        # another; two more sat at "Waiting for next available executor").
        # The LOCAL orchestrator runs every night (`NIGHTLY_CRON`). The `triggers` directive re-applies the timer at
        # every run — a pipeline without one clears the job's timer on its first run — and the job's properties below
        # arm it as soon as the job is seeded, before any run.
        triggers_block = f"    triggers {{\n        cron('{NIGHTLY_CRON}')\n    }}\n" if local else ""
        # A manual Build Now is the FAST run of every project; the nightly timer — or a tick on SLOW_TESTS — asks
        # every project with slow tests for them too (user ruling 2026-10-06), and that run gathers the statistics.
        slow_tests_head = (
            "// The nightly timer, or a tick on SLOW_TESTS, runs every project's slow tests too; Build Now the fast ones.\n"
            "def slowTests() {\n"
            "    return params.SLOW_TESTS || "
            "!currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').isEmpty()\n"
            "}\n\n"
            if local
            else ""
        )
        slow_tests_param = (
            """    parameters {
        booleanParam(
            name: 'SLOW_TESTS',
            defaultValue: false,
            description: 'If true, run the slow tests of every project too. The nightly run (01:00) does; a manual build runs the fast tests only.'
        )
    }
"""
            if local
            else ""
        )
        pipeline_script = f"""{slow_tests_head}pipeline {{
    agent none
    options {{
        disableConcurrentBuilds()
    }}
{slow_tests_param}{triggers_block}    stages {{
{stages_str}    }}
}}"""

        from xml.sax.saxutils import escape

        escaped_script = escape(pipeline_script)

        job_name = f"{base_name}.local" if local else base_name
        description = (
            f"Orchestrates the build of all local {base_name} workspace projects based on dependencies."
            if local
            else f"Orchestrates the build of all {base_name} workspace projects based on dependencies."
        )

        timer_property = (
            f"""
    <org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>
      <triggers>
        <hudson.triggers.TimerTrigger>
          <spec>{NIGHTLY_CRON}</spec>
        </hudson.triggers.TimerTrigger>
      </triggers>
    </org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>"""
            if local
            else ""
        )
        # SLOW_TESTS defined at seeding too, so Build with Parameters offers the tick before the job's first run.
        slow_switch_property = (
            """
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions>
        <hudson.model.BooleanParameterDefinition>
          <name>SLOW_TESTS</name>
          <defaultValue>false</defaultValue>
        </hudson.model.BooleanParameterDefinition>
      </parameterDefinitions>
    </hudson.model.ParametersDefinitionProperty>"""
            if local
            else ""
        )
        config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>{description}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>{timer_property}{slow_switch_property}
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{escaped_script}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
"""
        jobs_dir = self.service_data_dir / "jobs"
        project_job_dir = jobs_dir / job_name
        project_job_dir.mkdir(parents=True, exist_ok=True)
        config_file = project_job_dir / "config.xml"
        config_file.write_text(config_xml)
        click.echo(f"  - Seeded orchestrator job: {job_name}")

    def _create_nightly_check_job(self) -> None:
        """Seed `<workspace>.nightly-check`: `aisland tests nightly` each weekday morning (`NIGHTLY_CHECK_CRON`).

        It reports the orchestrator's nightly runs to `aisland/data/nightly/<date>.md`, notifies on a failure, and
        exits 1 then, so the job is red on the mornings that need a look (user choice 2026-10-06). The timer sits in
        the job's properties (armed at seeding) and in the pipeline's `triggers` (re-applied at each run).
        """
        from xml.sax.saxutils import escape

        from aisland.projects import get_workspace_name

        job_name = f"{get_workspace_name()}.nightly-check"
        pipeline_script = f"""// The morning report of the nightly runs (aisland/nightly.py): written to aisland/data/nightly/<date>.md,
// a desktop notification and a red build when a night failed, had no run, or is still building.
pipeline {{
    agent {{
        node {{
            label 'built-in'
        }}
    }}
    options {{
        disableConcurrentBuilds()
    }}
    triggers {{
        cron('{NIGHTLY_CHECK_CRON}')
    }}
    stages {{
        stage('Report the nightly runs') {{
            steps {{
                sh "${{env.WORKSPACE_ROOT}}/.venv/bin/aisland tests nightly"
            }}
        }}
    }}
}}"""
        config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>Reports the nightly runs of {get_workspace_name()}.local each weekday morning.</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>
    <org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>
      <triggers>
        <hudson.triggers.TimerTrigger>
          <spec>{NIGHTLY_CHECK_CRON}</spec>
        </hudson.triggers.TimerTrigger>
      </triggers>
    </org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{escape(pipeline_script)}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
"""
        job_dir = self.service_data_dir / "jobs" / job_name
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "config.xml").write_text(config_xml)
        click.echo(f"  - Seeded nightly check job: {job_name}")

    def scaffold_pipelines(self, project_name: Optional[str] = None, force: bool = False) -> None:
        """Generate standard Jenkinsfile, Jenkinsfile.local, and GitHub Actions for a project or all projects."""
        from aisland.projects import get_project_metadata, get_workspace_name, get_workspace_root

        workspace_root = get_workspace_root()

        if project_name is None:
            # Scaffold all projects that have a pyproject.toml — including
            # aisland itself, so its own CI surface stays in sync with the
            # template it generates for everyone else.
            for child in sorted(workspace_root.iterdir()):
                if child.is_dir() and (child / "pyproject.toml").exists():
                    self.scaffold_pipelines(project_name=child.name, force=force)
            return

        project_path = workspace_root / project_name

        if not project_path.exists() or not project_path.is_dir():
            raise AislandError(f"Project directory '{project_name}' not found at {project_path}.")

        pyproject_path = project_path / "pyproject.toml"
        if not pyproject_path.exists():
            raise AislandError(f"pyproject.toml not found in {project_name}. Cannot scaffold.")

        metadata = get_project_metadata(pyproject_path)
        pkg_name = metadata["name"]
        # Detect actual package directory.
        #   Step 1: try the literal distribution name (e.g. "confluid").
        #   Step 2: try the PEP 503 module form — distribution names allow dashes
        #           but Python import names don't, so "recordstream" -> "recordstream",
        #           "loggair" -> "loggair", etc. Try that before the heuristic
        #           fallback so we don't accidentally pick up "examples/".
        #   Step 3: scan for any inner dir with __init__.py, skipping known
        #           non-package dirs (tests, examples, notebooks, docs).
        SKIP_DIRS = {"tests", "examples", "notebooks", "docs", "scripts"}
        if not (project_path / pkg_name).exists():
            dash_stripped = pkg_name.replace("-", "")
            if (project_path / dash_stripped / "__init__.py").exists():
                pkg_name = dash_stripped
            else:
                for d in project_path.iterdir():
                    if d.is_dir() and (d / "__init__.py").exists() and d.name not in SKIP_DIRS:
                        pkg_name = d.name
                        break

        click.echo(f"Scaffolding CI/CD for {project_name} (Package: {pkg_name})...")

        # Internal dependencies — discovered, not hardcoded: every workspace project whose remote is on
        # GitHub, by its distribution name, with the repository a runner installs it from. Adding a new
        # workspace project is a zero-aisland-edit operation.
        github_projects = discover_github_projects(workspace_root)

        # Selected extras this project's CI installs: what it offers for each selection (aisland's `extras`, and
        # each `[tool.aisland.selection.<name>]` a project declares) intersected with that selection's COMMITTED
        # choice (`[workspace] <name>` of `workspace.toml`). Committed-only, for the reason the source registry below
        # is: this machine's `aisland.<name>.selected` must never reach a tracked build file, or one developer's "I
        # only use torch" would silently stop every runner from testing the Keras backend — and a runner that stops
        # installing mlflow silently stops running the tests that need it, which SKIP rather than fail. A project
        # offering nothing emits the historical `.[dev]` unchanged.
        selected_extras: list[str] = []
        for axis in sel_axes(workspace_root):
            selected_extras += sel_extras_for(
                sel_declared(pyproject_path, axis), sel_committed(workspace_root, axis.name)
            )
        # `dev` is listed first and unconditionally (CI always needs the test tooling), then
        # deduped: since 2026-10-20 the `extras` selection offers `dev` like any other extra, so a
        # workspace that selected it would otherwise emit `.[dev,dev,local]`.
        ci_extras = ",".join(dict.fromkeys(["dev"] + selected_extras))

        # A project that REGISTERS a `slow` pytest marker gets its CI test invocations SPLIT:
        # the routine run deselects the slow tests (`-m 'not slow'`) and a separate stage/job
        # runs `-m slow`, so an 8-minute end-to-end smoke test stops sitting on every push's
        # critical path while its dep-bump-drift coverage is preserved. The DECLARATION is the
        # marker registration itself (`[tool.pytest.ini_options] markers`), not a new aisland
        # knob — a project without the marker gets byte-identical output to before, so opting
        # in never churns the other projects' committed CI files. Exit code 5 ("no tests
        # matched the marker") is tolerated in the slow leg: a registered marker with nothing
        # currently marked is a valid state, not a failure.
        _pyproject_data = pyproject_toml.load(pyproject_path)
        has_slow_marker = registers_slow_marker(_pyproject_data)
        # A project whose `dev` extra lists PLAYWRIGHT gets its browser fetched right after the
        # dev install, in every artifact that runs its tests — the package alone cannot open a
        # page, and a browser-driven test would otherwise `skip` its way into a green build that
        # checks less. Same shape as the slow marker above: the dependency IS the declaration,
        # there is no aisland knob, and a project without it gets byte-identical output.
        # `--with-deps` on the GitHub runner adds the OS libraries Ubuntu lacks (sudo is available
        # there, never assumed on a Jenkins agent, where the browser cache is per user and the
        # download happens once). `aisland setup` runs the same install for a developer's venv
        # (`selection.browser_install_command`), so the two never disagree on the browser.
        needs_browser = "playwright" in dev_extra_names(_pyproject_data)
        # A `package.json` with a `test` script is the declaration that `npm test` is a run of its own (no aisland
        # knob, as the `slow` marker and the playwright dependency): each such folder gets the Node Tests stage.
        node_tests = node_test_folders(project_path)

        # CI asks ONE question per internal dependency — is it RELEASED on PyPI? — and nothing
        # about the workspace's sources (`workspace.toml`) (user ruling 2026-09-26: "it should always
        # point to released packages, and to GitHub for non-released ones").
        #
        # Released (a final, non-yanked version whose metadata points at github.com/<its owner>/ —
        # `sources.released_on_pypi`) -> the dep drops out of the pre-install loop and `.[dev]`
        # resolves the release from PyPI. Not released -> the runner installs GitHub `main`
        # (`DEFAULT_REF`). A source (`local` / `github` / `pypi`, any `ref`) describes ONE
        # machine's checkout and venv; before this rule it also decided the runner's install line,
        # so `aisland workspace configure source set` rewrote every dependent's committed CI files and a released
        # package developed locally (liquifai) was built from GitHub `main` by every consumer.
        #
        # PyPI unreachable -> `ReleaseProbeError` stops the scaffold before any file of this
        # project is written: assuming "unreleased" would flip every released dep to a GitHub line.
        def _dep_is_released(dep: str) -> bool:
            return released_on_pypi(dep, github_projects[dep].owner)

        # Collect the internal deps CI must pre-install: the closure of
        # exactly what CI's install steps resolve, nothing more. When CI installs
        # a direct dep like `confluid` with --no-deps, `.[dev]` below still needs
        # to resolve confluid's own BASE deps (e.g. loggair) — and unreleased internal
        # distribution names aren't on PyPI, so uv fails unless WE'VE ALSO
        # pre-installed those transitive deps first.
        #
        # What each node contributes (2026-09-02, replaces "walk every extras
        # group of every project", which pre-installed packages nothing would
        # ever import):
        #   - the ROOT project: its base deps PLUS the extras its CI actually
        #     installs — `.[dev,<selected axis extras>]` plus the best-effort
        #     `.[notebook]` step;
        #   - a TRANSITIVE dep: its base deps PLUS only the extras the incoming
        #     requirement named (`matrainer[keras]` walks matrainer's keras
        #     extra). Its OTHER extras are never requested by any install step
        #     (each dep installs bare, and `.[dev]` resolves only its base deps),
        #     so they must not add pre-installs — the old rule made the CI of
        #     a project depending on one other project install everything that
        #     project's dev extra reaches (torpaido through matrainer's extra,
        #     a sibling through a dev extra).
        # Specs are parsed with packaging.Requirement: the regex split it
        # replaces couldn't parse `matrainer[torch]>=0.1.0` and silently dropped
        # the spec. Topologically sorted so every package's deps install first.

        def _load_project_table(pyproject: Path) -> dict:
            """The [project] table of one pyproject.toml; {} when missing/unparseable
            (a detached submodule's directory is empty and contributes nothing)."""
            table = pyproject_toml.read(pyproject).get("project", {})
            return table if isinstance(table, dict) else {}

        def _internal_requirements(project_table: dict, extras: frozenset[str]) -> list[Requirement]:
            """The internal requirements one node contributes: its base dependencies
            plus the contents of exactly the requested extras groups."""
            specs: list[str] = list(project_table.get("dependencies") or [])
            optional = project_table.get("optional-dependencies") or {}
            for extra in extras:
                specs.extend(optional.get(extra) or [])
            reqs: list[Requirement] = []
            for spec in specs:
                try:
                    req = Requirement(spec)
                except InvalidRequirement:
                    continue
                if req.name in github_projects:
                    reqs.append(req)
            return reqs

        # Map each internal project name to its workspace dir (for locating its
        # pyproject.toml). PyPI-dashed names resolve to dashless dirs.
        def _internal_workspace_path(name: str) -> Path:
            if (workspace_root / name).is_dir():
                return workspace_root / name
            stripped = name.replace("-", "")
            return workspace_root / stripped

        # BFS over (dep, requested extras) pairs — the same dep may be reached
        # with different extras sets, and each set can name different siblings.
        root_installed_extras = frozenset(["dev", "notebook", *selected_extras])
        work: list[tuple[str, frozenset[str]]] = [
            (req.name, frozenset(req.extras))
            for req in _internal_requirements(_pyproject_data.get("project", {}), root_installed_extras)
        ]
        visited: set[tuple[str, frozenset[str]]] = set()
        seen: set[str] = set()
        while work:
            item = work.pop()
            if item in visited:
                continue
            visited.add(item)
            dep, dep_extras = item
            seen.add(dep)
            sub_table = _load_project_table(_internal_workspace_path(dep) / "pyproject.toml")
            work.extend((req.name, frozenset(req.extras)) for req in _internal_requirements(sub_table, dep_extras))

        # A project never pre-installs itself: a dependency cycle back to the root
        # (two projects requiring each other through a dev extra) would otherwise install the
        # root's own main branch over the very checkout under test.
        seen.discard(metadata["name"])

        # Topological sort so loggair is installed before confluid, etc.
        # Base deps suffice for ordering (every install runs --no-deps anyway).
        prereqs: dict[str, set[str]] = {name: set() for name in seen}
        for name in seen:
            sub_table = _load_project_table(_internal_workspace_path(name) / "pyproject.toml")
            for req in _internal_requirements(sub_table, frozenset()):
                if req.name in seen and req.name != name:
                    prereqs[name].add(req.name)

        # Kahn's algorithm.
        internal_deps: list[str] = []
        remaining = dict(prereqs)
        while remaining:
            ready = sorted(n for n, pre in remaining.items() if not pre)
            if not ready:
                # Cycle — fall back to seen order.
                internal_deps.extend(sorted(remaining))
                break
            for n in ready:
                internal_deps.append(n)
                del remaining[n]
                for pre in remaining.values():
                    pre.discard(n)

        # GH Actions — raw uv commands (valid inside `run: |` blocks).
        #
        # Install order: git-URL internal deps FIRST (--no-deps), then `-e .[dev]`.
        # Rationale: an UNRELEASED internal distribution (recordstream, torpaido, ...)
        # is not on PyPI (or only as a pre-release). If `.[dev]` ran first, uv would
        # try to resolve `recordstream>=0.1.0` from PyPI, fail ("not found in the
        # package registry"), and abort the whole install. A RELEASED one never
        # enters this loop — `.[dev]` resolves it from PyPI (see `_dep_is_released`).
        # By installing each internal dep from its git URL first (with --no-deps to
        # skip its own internal transitive deps — each is handled in its own loop
        # iteration, topologically ordered), we pre-satisfy the constraints so
        # `-e .[dev]` resolves cleanly against the already-installed packages.
        # --system installs into the runner's Python rather than requiring a venv.
        # The deps a runner installs from GitHub — every unreleased one — and the owners they belong to, which the
        # generated comments and the token step name.
        from_github = [dep for dep in internal_deps if not _dep_is_released(dep)]
        # A project that does not live on GitHub (a company's Bitbucket server) never names a project that does in its
        # CI files: they would reveal the workspace it is developed in. Its unreleased dependencies come from the wheels
        # it commits (`aisland wheels build --extra`), installed through that extra; without them it is refused.
        if from_github and project_name not in project_repositories(workspace_root):
            wheels_extra_name = _unreleased_from_wheels(project_name, pyproject_path, from_github)
            ci_extras = ",".join(dict.fromkeys(ci_extras.split(",") + [wheels_extra_name]))
            from_github = []
        owners = ", ".join(sorted({github_projects[dep].owner for dep in from_github}))

        # Detect private dependencies — if any, CI needs a GitHub App token to clone them (default
        # GITHUB_TOKEN only grants access to the current repo). is_private_repo() queries `gh api` with a
        # per-process cache; it falls back to True (assume private) on any error.
        # Only deps CI actually clones from GitHub need the token — a released dep is fetched
        # from the public index, so it must not keep the step alive on its own. One App token reaches ONE
        # owner's repositories, so private deps of two owners are refused rather than half-authorized.
        private_owners = sorted(
            {github_projects[dep].owner for dep in from_github if is_private_repo(github_projects[dep])}
        )
        if len(private_owners) > 1:
            raise AislandError(
                f"{project_name}'s CI would clone private repositories of {len(private_owners)} GitHub owners "
                f"({', '.join(private_owners)}); one app token reaches one owner's repositories. Release the "
                "dependencies of all but one owner, or make their repositories public."
            )

        token_owner = private_owners[0] if private_owners else None
        # Every file of the workspace's template set (aisland's own by default; `ci_templates`), from these facts.
        # A secret name holds letters, digits and underscores only: `acme-labs` -> `ACME_LABS_APP_ID`.
        facts = ci_templates.Facts(
            project=project_name,
            title=project_name.capitalize(),
            distribution=metadata["name"],
            package=pkg_name,
            extras=ci_extras.split(","),
            browser=PLAYWRIGHT_BROWSER if needs_browser else None,
            slow_tests=has_slow_marker,
            node_tests=node_tests,
            unreleased=[ci_templates.Dependency(dep, github_projects[dep].url, DEFAULT_REF) for dep in from_github],
            unreleased_owners=owners,
            token_owner=token_owner,
            token_secret=re.sub(r"[^A-Z0-9]", "_", token_owner.upper()) if token_owner else None,
            stage_limits=dict(STAGE_LIMITS),
            runner_limits={stage: runner_limit(stage) for stage in STAGE_LIMITS},
            actions=dict(GH_ACTIONS),
            timed_out_message=TIMED_OUT_MESSAGE,
            workspace=get_workspace_name(workspace_root),
            jenkins_url=self.url(),
        )
        rendered = ci_templates.render(ci_templates.choice(workspace_root, project_name), facts)

        jenkins_url = self.url()

        # Helper to write file safely
        def _validate_jenkinsfile(content: str, desc: str) -> bool:
            """POST the content to Jenkins' declarative linter. Returns True if the file
            validates (or if Jenkins isn't running — we can't check then). Returns False
            only when Jenkins actively rejects the file."""
            try:
                # Fetch CSRF crumb — required for all POSTs even with security disabled.
                crumb_req = urllib.request.Request(f"{jenkins_url}/crumbIssuer/api/json")
                with urllib.request.urlopen(crumb_req, timeout=3) as resp:
                    crumb_data = json.loads(resp.read().decode())
                crumb_header = {crumb_data["crumbRequestField"]: crumb_data["crumb"]}
            except (urllib.error.URLError, OSError, TimeoutError, KeyError):
                click.echo(f"  [WARN] Jenkins not reachable — skipped validation of {desc}.")
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
                click.echo(f"  [WARN] Linter call failed ({e}); skipped validation of {desc}.")
                return True

            if result.startswith("Jenkinsfile successfully validated"):
                return True
            click.echo(click.style(f"  [FAIL] {desc} did not validate:\n{result}", fg="red"))
            return False

        def _write_safe(path: Path, content: str, desc: str) -> None:
            if path.exists() and not force:
                click.echo(f"  [SKIP] {desc} already exists. Use --force to overwrite.")
                return
            # For Jenkinsfile-type content, validate against the Jenkins linter BEFORE
            # writing to disk so a broken template is caught at scaffold time, not
            # when a build fails hours later.
            if desc.startswith("Jenkinsfile"):
                if not _validate_jenkinsfile(content, desc):
                    click.echo(
                        click.style(
                            f"  [ABORT] Not writing {desc} — fix the template.",
                            fg="red",
                        )
                    )
                    raise AislandError(f"Template produced invalid {desc}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            click.echo(f"  [OK] Created {desc}")

        for relative, content in rendered.items():
            _write_safe(project_path / relative, content, relative)

    @staticmethod
    def local_job_config(project_base_name: str, script_content: str, project_path: Path) -> str:
        """The config.xml of a local job (`<project>.local`): its `Jenkinsfile.local` embedded, no commit needed.

        A pipeline's `parameters` are defined by its FIRST run, and Jenkins drops a parameter a job does not define —
        so a pipeline declaring SLOW_TESTS gets it defined here already, or the orchestrator's first nightly run would
        ask a new job for its slow tests in vain.
        """
        from xml.sax.saxutils import escape

        slow_switch = (
            """
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions>
        <hudson.model.BooleanParameterDefinition>
          <name>SLOW_TESTS</name>
          <defaultValue>false</defaultValue>
        </hudson.model.BooleanParameterDefinition>
      </parameterDefinitions>
    </hudson.model.ParametersDefinitionProperty>"""
            if "name: 'SLOW_TESTS'" in script_content
            else ""
        )
        return f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>Local development pipeline for {project_base_name}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>{slow_switch}
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{escape(script_content)}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
  <customWorkspace>{project_path.absolute()}</customWorkspace>
</flow-definition>
"""

    def init_pipelines(self, project_name: Optional[str] = None, local: bool = False) -> None:
        """Seed Jenkins with Multi-branch pipelines for projects discovered in the workspace."""
        self.setup()
        # setup_plugins returns True if it actually installed/updated something
        plugins_changed = self.setup_plugins()

        workspace_root = get_workspace_root()

        if project_name:
            project_path = workspace_root / project_name
            if not project_path.exists() or not project_path.is_dir():
                raise AislandError(f"Project directory '{project_name}' not found at {project_path}.")
            if not local and not (project_path / "Jenkinsfile").exists():
                raise AislandError(f"Project '{project_name}' does not have a Jenkinsfile at its root.")
            if local and not (project_path / "Jenkinsfile.local").exists():
                raise AislandError(f"Project '{project_name}' does not have a Jenkinsfile.local at its root.")
            projects = [project_path]
        else:
            if local:
                projects = [p for p in workspace_root.iterdir() if p.is_dir() and (p / "Jenkinsfile.local").exists()]
            else:
                projects = [p for p in workspace_root.iterdir() if p.is_dir() and (p / "Jenkinsfile").exists()]

        if not projects:
            click.echo("No projects with the required Jenkinsfile found to initialize.")
            return

        jobs_dir = self.service_data_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        click.echo(f"Initializing Jenkins pipelines for {len(projects)} project(s)...")

        for project_path in projects:
            project_base_name: Optional[str] = project_path.name
            if project_base_name is None:
                continue

            project: str = f"{project_base_name}.local" if local else project_base_name
            project_job_dir = jobs_dir / project
            project_job_dir.mkdir(exist_ok=True)

            if local:
                # For local jobs, embed the Jenkinsfile.local content directly to avoid needing commits
                jenkinsfile_path = project_path / "Jenkinsfile.local"
                if jenkinsfile_path.exists():
                    config_xml = self.local_job_config(project_base_name, jenkinsfile_path.read_text(), project_path)
                    config_file = project_job_dir / "config.xml"
                    config_file.write_text(config_xml)
                    click.echo(f"  - Seeded local job (embedded script): {project}")
                    continue
                else:
                    failures.warn(f"  - Warning: {jenkinsfile_path} not found for {project}. Skipping.")
                    continue

            # Standard Multibranch Pipeline for remote jobs
            remote_url = self._get_git_remote(project_path)
            if not remote_url:
                click.echo(f"  - Skipping {project}: Could not determine Git remote.")
                continue
            cred_id = self._find_credentials(remote_url)
            script_path = "Jenkinsfile"

            # Simplified Multibranch Pipeline config.xml template
            config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject plugin="workflow-multibranch">
  <properties/>
  <folderViews class="jenkins.branch.MultiBranchProjectViewHolder" plugin="branch-api">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </folderViews>
  <healthMetrics/>
  <icon class="jenkins.branch.MetadataActionFolderIcon" plugin="branch-api">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </icon>
  <orphanedItemStrategy
      class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy"
      plugin="cloudbees-folder">
    <pruneDeadBranches>true</pruneDeadBranches>
    <daysToKeep>-1</daysToKeep>
    <numToKeep>-1</numToKeep>
  </orphanedItemStrategy>
  <triggers>
    <com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger plugin="cloudbees-folder">
      <spec>* * * * *</spec>
      <interval>300000</interval>
    </com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger>
  </triggers>
  <disabled>false</disabled>
  <sources class="jenkins.branch.MultiBranchProject$BranchSourceList" plugin="branch-api">
    <data>
      <jenkins.branch.BranchSource>
        <source class="jenkins.plugins.git.GitSCMSource" plugin="git">
          <id>{project}-remote</id>
          <remote>{remote_url}</remote>
          <credentialsId>{cred_id}</credentialsId>
          <traits>
            <jenkins.plugins.git.traits.BranchDiscoveryTrait/>
          </traits>
        </source>
        <strategy class="jenkins.branch.DefaultBranchPropertyStrategy">
          <properties class="empty-list"/>
        </strategy>
      </jenkins.branch.BranchSource>
    </data>
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </sources>
  <factory class="org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
    <scriptPath>{script_path}</scriptPath>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>
"""
            config_file = project_job_dir / "config.xml"
            config_file.write_text(config_xml)
            click.echo(f"  - Seeded job: {project} (Remote: {remote_url}, CredID: {cred_id})")

        # Create the orchestrator job
        initialized_names = [p.name for p in projects if p.name]
        self._create_orchestrator_job(local, initialized_names)
        if local:
            self._create_nightly_check_job()

        # Two failure modes to avoid when injecting globalProperties:
        #   1. Jenkins is running → our config.xml edits get clobbered by
        #      Jenkins's in-memory model on its next save.
        #   2. Jenkins has never been started (clean reset) → config.xml
        #      doesn't exist yet, and setup_global_properties early-returns
        #      silently, leaving env.WORKSPACE_ROOT null in every build
        #      (manifests as `null/.venv/bin/python3: No such file`).
        # Solution: warm-start Jenkins to materialize config.xml, then stop
        # it before mutating the file, then cold-start for real.
        if not self.is_running():
            click.echo("Warm-starting Jenkins to generate baseline config.xml...")
            self.launch(background=True)
            config_file = self.service_data_dir / "config.xml"
            deadline = time.time() + 90
            while not config_file.exists() and time.time() < deadline:
                time.sleep(1)
            if not config_file.exists():
                failures.warn("  - Warning: config.xml did not appear within 90s.")
        self.stop()

        self.setup_dashboard_view()
        self.setup_global_properties()

        if local:
            if click.confirm("Reduce Jenkins security for faster local access? (Disables CSRF, allows anonymous read)"):
                self.reduce_security()

        click.echo("Starting Jenkins to load the new plugins, pipelines, and globalProperties...")
        self.launch(background=True)
        if plugins_changed:
            click.echo("Jenkins plugins updated.")
        click.echo("Jenkins started in the background.")

    def setup_dashboard_view(self, name: Optional[str] = None) -> None:
        """Create a Build Monitor View for the Ecosystem branching workflow."""
        from aisland.projects import get_workspace_name

        base_name = get_workspace_name()
        if name is None:
            name = f"{base_name.capitalize()} Ecosystem Dashboard"

        config_file = self.service_data_dir / "config.xml"
        if not config_file.exists():
            click.echo("  - Jenkins config.xml not found. Skipping view setup until first run.")
            return

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()
        except (OSError, ET.ParseError) as e:
            failures.report(f"  - Error parsing config.xml: {e}")
            return

        # Find or create the 'views' container
        views = root.find("views")
        if views is None:
            views = ET.SubElement(root, "views")
            # If views was missing, also add the 'all' view to keep standard behavior
            all_view = ET.SubElement(views, "hudson.model.AllView")
            ET.SubElement(all_view, "owner").set("class", "hudson")
            ET.SubElement(all_view, "owner").set("reference", "../../..")
            ET.SubElement(all_view, "name").text = "all"
            ET.SubElement(all_view, "filterExecutors").text = "false"
            ET.SubElement(all_view, "filterQueue").text = "false"
            ET.SubElement(all_view, "properties").set("class", "hudson.model.View$PropertyList")

        # Find or create the view.
        # First pass: look for an exact name match.
        # Second pass: if not found, look for ANY existing "<Anything> Ecosystem Dashboard"
        # view and RENAME it in place. This handles workspace rename (e.g. the repo is
        # renamed on GitHub, so get_workspace_name() returns a new string) without
        # stranding the old view or creating a duplicate.
        dashboard_view = None
        for v in views:
            if v.findtext("name") == name:
                dashboard_view = v
                break
        if dashboard_view is None:
            for v in views:
                existing = v.findtext("name") or ""
                if existing.endswith(" Ecosystem Dashboard"):
                    click.echo(
                        f"  - Renaming stale dashboard view '{existing}' -> '{name}' " f"(workspace was renamed)."
                    )
                    name_elem = v.find("name")
                    if name_elem is not None:
                        name_elem.text = name
                    title_elem = v.find("title")
                    if title_elem is not None:
                        title_elem.text = name
                    dashboard_view = v
                    break

        if dashboard_view is None:
            click.echo(
                f"  - Creating '{name}' view (branches: main, dev/main, *.local) " f"with Build Monitor View plugin..."
            )
            dashboard_view = ET.SubElement(
                views,
                "com.smartcodeltd.jenkinsci.plugins.buildmonitor.BuildMonitorView",
            )
            dashboard_view.set("plugin", "build-monitor-plugin")
            ET.SubElement(dashboard_view, "owner").set("class", "hudson")
            ET.SubElement(dashboard_view, "owner").set("reference", "../../..")
            ET.SubElement(dashboard_view, "name").text = name
            ET.SubElement(dashboard_view, "filterExecutors").text = "false"
            ET.SubElement(dashboard_view, "filterQueue").text = "false"
            ET.SubElement(dashboard_view, "properties").set("class", "hudson.model.View$PropertyList")
            ET.SubElement(dashboard_view, "jobNames").set("class", "hudson.util.CaseInsensitiveComparator")
            ET.SubElement(dashboard_view, "jobFilters")
            ET.SubElement(dashboard_view, "columns").set("class", "empty-list")
            ET.SubElement(dashboard_view, "recurse").text = "true"
            ET.SubElement(dashboard_view, "title").text = name
            config = ET.SubElement(dashboard_view, "config")
            ET.SubElement(config, "displayCommitters").text = "true"
            ET.SubElement(config, "displayLowPriorityJobs").text = "false"
            ET.SubElement(config, "displayLastBuildConsoleOutput").text = "true"
            ET.SubElement(config, "order").set("class", "com.smartcodeltd.jenkinsci.plugins.buildmonitor.order.ByName")

        # Always ensure the regex filter is present and correct
        job_filters = dashboard_view.find("jobFilters")
        if job_filters is None:
            job_filters = ET.SubElement(dashboard_view, "jobFilters")

        regex_filter = job_filters.find("hudson.views.RegExJobFilter")
        if regex_filter is None:
            regex_filter = ET.SubElement(job_filters, "hudson.views.RegExJobFilter")
            regex_filter.set("plugin", "view-job-filters")
            ET.SubElement(regex_filter, "includeExcludeTypeString").text = "includeMatched"
            ET.SubElement(regex_filter, "valueTypeString").text = "NAME"
            ET.SubElement(regex_filter, "regex").text = "^(main|dev/main|.*\\.local)$"
            ET.SubElement(regex_filter, "matchCanary").text = "false"
        else:
            regex_elem = regex_filter.find("regex")
            if regex_elem is None:
                regex_elem = ET.SubElement(regex_filter, "regex")
            regex_elem.text = "^(main|dev/main|.*\\.local)$"

        # Revert primary view to 'all' to ensure standard overview is default
        primary_view = root.find("primaryView")
        if primary_view is not None:
            primary_view.text = "all"

        # Robust write
        tree.write(config_file, encoding="UTF-8", xml_declaration=True)

    def setup_global_properties(self) -> None:
        """Add WORKSPACE_ROOT and other workspace-wide settings to Jenkins global properties."""
        config_file = self.service_data_dir / "config.xml"
        if not config_file.exists():
            return

        from aisland.projects import get_workspace_root

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()
        except (OSError, ET.ParseError) as e:
            failures.report(f"  - Error parsing config.xml for global properties: {e}")
            return

        # Find or create globalNodeProperties
        node_props = root.find("globalNodeProperties")
        if node_props is None:
            node_props = ET.SubElement(root, "globalNodeProperties")

        # Find or create EnvironmentVariablesNodeProperty
        env_vars_prop = node_props.find("hudson.slaves.EnvironmentVariablesNodeProperty")
        if env_vars_prop is None:
            env_vars_prop = ET.SubElement(node_props, "hudson.slaves.EnvironmentVariablesNodeProperty")

        # Clear existing envVars
        env_vars = env_vars_prop.find("envVars")
        if env_vars is not None:
            env_vars_prop.remove(env_vars)

        # Create envVars with custom serialization (tree-map)
        env_vars = ET.SubElement(env_vars_prop, "envVars")
        env_vars.set("serialization", "custom")
        ET.SubElement(env_vars, "unserializable-parents")
        tree_map = ET.SubElement(env_vars, "tree-map")

        # Add default/comparator
        default_elem = ET.SubElement(tree_map, "default")
        ET.SubElement(default_elem, "comparator").set("class", "hudson.util.CaseInsensitiveComparator")

        workspace_root = get_workspace_root()
        dotenv_path = workspace_root / ".env"
        dotenv_vars = supervisor.read_workspace_env(workspace_root)  # the one reader: what the shell reads
        if dotenv_vars:
            logger.debug("read %d key(s) from %s", len(dotenv_vars), dotenv_path)
        else:
            click.echo("  - No workspace .env found; skipping injection.")

        # Computed keys come first so a typo'd .env can't override the workspace
        # path or the forced-Node24 toggle. ** dotenv_vars adds the remainder.
        env_data = {
            "WORKSPACE_ROOT": str(workspace_root),
            "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": "true",
            **{
                k: v
                for k, v in dotenv_vars.items()
                if k not in ("WORKSPACE_ROOT", "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24")
            },
        }

        # int size
        ET.SubElement(tree_map, "int").text = str(len(env_data))

        computed_keys = {"WORKSPACE_ROOT", "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"}
        for key, value in env_data.items():
            ET.SubElement(tree_map, "string").text = key
            ET.SubElement(tree_map, "string").text = value
            if key in computed_keys:
                click.echo(f"  - Added global Jenkins property: {key}={value}")
            else:
                click.echo(f"  - Added global Jenkins property: {key}=<from .env>")

        tree.write(config_file, encoding="UTF-8", xml_declaration=True)

    def reduce_security(self) -> None:
        """Modify config.xml to reduce security for local development."""
        config_file = self.service_data_dir / "config.xml"
        if not config_file.exists():
            return

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()
        except (OSError, ET.ParseError) as e:
            failures.report(f"  - Error parsing config.xml for security reduction: {e}")
            return

        if click.confirm("Disable Jenkins security entirely? (No login required, most fast/unsecure)"):
            use_security = root.find("useSecurity")
            if use_security is not None:
                use_security.text = "false"
                click.echo("  - Jenkins security disabled.")
        else:
            # 1. Allow Anonymous Read Access
            auth_strategy = root.find("authorizationStrategy")
            if auth_strategy is not None:
                deny_anon = auth_strategy.find("denyAnonymousReadAccess")
                if deny_anon is not None:
                    deny_anon.text = "false"
                    click.echo("  - Enabled anonymous read access.")

            # 2. Disable CSRF Protection (Crumb Issuer)
            crumb = root.find("crumbIssuer")
            if crumb is not None:
                root.remove(crumb)
                click.echo("  - Disabled CSRF protection (Crumb Issuer removed).")

        tree.write(config_file, encoding="UTF-8", xml_declaration=True)
