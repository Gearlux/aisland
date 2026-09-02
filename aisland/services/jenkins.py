import functools
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List, Optional, cast

import click
from packaging.requirements import InvalidRequirement, Requirement

from aisland.manager import BIN_DIR, BaseService
from aisland.selection import AXES
from aisland.selection import declared as sel_declared
from aisland.selection import extras_for as sel_extras_for
from aisland.selection import read_committed as sel_committed
from aisland.sources import DEFAULT_REF, read_committed_sources, source_for_dependency

# Match the Gearlux GitHub URL forms (SSH and HTTPS), capturing the repo name.
# Trailing ``.git`` is optional. Anchored so a repo path like "Gearlux/x/y" can't
# accidentally match — Gearlux repos are always top-level under the org.
_GEARLUX_URL_RE = re.compile(r"(?:git@github\.com:|https://github\.com/)Gearlux/([^/]+?)(?:\.git)?$")
_SUBMODULE_KEY_RE = re.compile(r"^submodule\.(.+)\.url$")


def discover_gearlux_projects(workspace_root: Path) -> list[str]:
    """Return every sibling project's PyPI distribution name that is a Gearlux GitHub submodule.

    Walks ``<workspace_root>/.gitmodules`` for entries whose URL is on
    ``github.com:Gearlux/`` (SSH or HTTPS form), then reads each submodule's
    ``pyproject.toml`` for the ``[project] name`` field — the canonical
    distribution name, which equals both the PyPI key and the GitHub repo
    name. Submodules without a pyproject.toml or with an unparseable one are
    silently skipped (they are not Python projects aisland needs to install).

    Replaces a previously hard-coded list. The whole point: when a new sibling
    project is added (a new submodule + pyproject), CI scaffolding picks it up
    on the next ``aisland jenkins scaffold`` run with no aisland-side edit.

    A DETACHED project (``github`` / ``pypi`` source, ``aisland/sources.py``) has an EMPTY
    directory and therefore no ``pyproject.toml`` to read — so its name is taken from the
    committed registry's ``distribution`` field instead, which exists precisely because a
    deinit'd directory can no longer be asked. Omitting it would silently drop the dep from the
    scaffolder's internal set: harmless for ``pypi`` (which is dropped anyway), but for
    ``github`` it would erase the very git-URL pin the source exists to express, leaving CI to
    resolve the PyPI release instead of the requested ref — or to fail outright when the project
    is not published at all.

    Returns:
        Alphabetically sorted list. Topological ordering is computed by the
        caller from each project's own deps, not here.
    """
    from aisland.sources import read_committed_sources

    detached_names = [
        source.distribution or project for project, source in read_committed_sources(workspace_root).items()
    ]

    gitmodules = workspace_root / ".gitmodules"
    if not gitmodules.exists():
        return sorted(set(detached_names))

    try:
        cp = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(gitmodules),
                "--get-regexp",
                r"submodule\..*\.url",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return sorted(set(detached_names))

    names: list[str] = list(detached_names)
    for line in cp.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        cfg_key, url = parts
        if not _GEARLUX_URL_RE.match(url.strip()):
            continue
        path_match = _SUBMODULE_KEY_RE.match(cfg_key)
        if not path_match:
            continue
        pyproject = workspace_root / path_match.group(1) / "pyproject.toml"
        if not pyproject.exists():
            continue
        try:
            with open(pyproject, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        dist_name = (data.get("project") or {}).get("name")
        if isinstance(dist_name, str) and dist_name:
            # Match the ``-manager`` strip convention used elsewhere in this file
            # (search for ``metadata["name"].replace("-manager", "")``). aisland's
            # pyproject distribution is ``aisland-manager`` but the repo, and what
            # consumers actually list as a dep, is ``aisland``.
            names.append(dist_name.removesuffix("-manager"))
    # Dedup: a detached project may ALSO still have a checkout on the machine running the
    # scaffold (a `local` per-machine override), in which case both passes contribute its name.
    return sorted(set(names))


@functools.lru_cache(maxsize=None)
def is_private_gearlux_repo(name: str) -> bool:
    """Return True iff ``Gearlux/<name>`` is a private GitHub repository.

    Queries ``gh api repos/Gearlux/<name> --jq .private`` with a 5 s timeout.
    Falls back to True (assume private) on any error — gh missing, not
    authenticated, network down, repo doesn't exist, etc. The result is
    cached for the process so a single scaffold run makes at most one API
    call per distinct dep name.

    Why the conservative fallback: a previous version defaulted to False
    ("assume public") so projects with only public deps wouldn't carry a
    useless token-minting step. That backfired when a workstation without
    ``gh`` regenerated CI for projects that DO have private deps — every
    re-scaffold silently stripped the ``Mint GitHub App token`` step from
    every ``ci.yml``, and the breakage only surfaced on the next CI run.
    Defaulting to True keeps the token step in place by default; the cost
    on a runner that's missing ``GEARLUX_APP_*`` secrets is one no-op step,
    not a broken build. Set the secrets at the org level once and forget.
    """
    try:
        cp = subprocess.run(
            ["gh", "api", f"repos/Gearlux/{name}", "--jq", ".private"],
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


def _parse_dotenv(env_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from a workspace ``.env`` file.

    Mirrors the loose semantics of ``project.bashrc`` (``export $(grep -v '^#' .env | xargs)``):
    skip blank and comment lines, split on the first ``=``, strip surrounding quotes.
    Returns an empty dict when the file is absent so callers can no-op silently.
    """
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def pyproject_to_workspace_dir(pyproject_name: str, initialized_set: set) -> str:
    """Map a pyproject distribution name to its workspace directory name.

    Handles three conventions in use across acme:
      1. Identity (most projects): ``confluid`` -> ``confluid``.
      2. Dashed PyPI name -> dashless dir: ``loggair`` -> ``loggair``,
         ``recordstream`` -> ``recordstream``.
      3. ``-manager`` suffix dropped: ``aisland-manager`` -> ``aisland``.

    Returns the input unchanged if no rule matches (caller decides whether
    that unknown name is a problem).
    """
    if pyproject_name in initialized_set:
        return pyproject_name
    dashless = pyproject_name.replace("-", "")
    if dashless in initialized_set:
        return dashless
    if pyproject_name.endswith("-manager"):
        trimmed = pyproject_name[: -len("-manager")]
        if trimmed in initialized_set:
            return trimmed
    return pyproject_name


class JenkinsService(BaseService):
    """Jenkins CI/CD Server."""

    URL = "https://get.jenkins.io/war-stable/latest/jenkins.war"
    REPO = "jenkinsci/plugin-installation-manager-tool"

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
        headers = {"User-Agent": "Gearlux-AI-Agent"}
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                if not isinstance(data, dict):
                    return {}
                return cast(dict[Any, Any], data)
        except Exception:
            # Silence this error if we already have a jar file, as we can still proceed.
            if not self.manager_jar.exists():
                click.echo(
                    "  - Error fetching latest Jenkins Plugin Manager info from GitHub API.",
                    err=True,
                )
            return {}

    def setup(self) -> None:
        """Download Jenkins binary and the latest plugin manager."""
        BIN_DIR.mkdir(exist_ok=True)

        if not self.war_file.exists():
            click.echo(f"Downloading Jenkins from {self.URL}...")
            try:
                urllib.request.urlretrieve(self.URL, self.war_file)
                click.echo("  - Jenkins download complete.")
            except Exception as e:
                click.echo(f"Error downloading Jenkins: {e}", err=True)
                sys.exit(1)

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
                        urllib.request.urlretrieve(download_url, latest_jar_path)
                        click.echo(f"  - Plugin Manager {tag} download complete.")
                        # Remove old versions
                        for old_jar in BIN_DIR.glob("jenkins-plugin-manager-*.jar"):
                            if old_jar != latest_jar_path:
                                old_jar.unlink()
                    except Exception as e:
                        click.echo(f"Error downloading Plugin Manager: {e}", err=True)

                self.manager_jar = latest_jar_path

        if not self.manager_jar.exists():
            click.echo(
                "  - Error: Plugin Manager could not be discovered or downloaded.",
                err=True,
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
            click.echo("  - All required Jenkins plugins are already installed.")
            return False

        plugins_dir = self.service_data_dir / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)

        # Create plugins.txt for the manager
        plugins_txt = self.service_data_dir / "plugins.txt"
        plugins_txt.write_text("\n".join(self.PLUGINS))

        import subprocess

        cmd = [
            "java",
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
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            if "Installed" in result.stdout or "Done" in result.stdout:
                click.echo("  - Jenkins plugins and dependencies synchronized.")
            return True
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error installing plugins: {e.stderr}", err=True)
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
                except Exception:
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
            except Exception:
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

    def launch(self, port: int = 9080, background: bool = False) -> None:
        """Start the Jenkins server."""
        self.setup()
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
        cmd = ["java", *jvm_args, "-jar", str(self.war_file), f"--httpPort={port}"]
        workspace_root = Path(__file__).resolve().parents[3]
        venv_bin = workspace_root / ".venv" / "bin"
        # BIN_DIR (aisland/bin) hosts symlinks to userspace-installed tools like
        # node/npm/npx (see manager._install_userspace_tool). Putting it on
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

    def info(self) -> None:
        """Show detailed Jenkins configuration."""
        super().info()
        default_port = 9080
        ports = self.get_ports()
        url_port = ports[0] if ports else default_port
        click.echo(f"  URL           : http://localhost:{url_port}")
        click.echo(f"  JENKINS_HOME  : {self.service_data_dir}")
        click.echo(f"  WAR file      : {self.war_file}")
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
        import subprocess

        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=project_path,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except Exception:
            return None

    def _find_credentials(self, remote_url: str) -> str:
        """Find matching credentials in Jenkins configuration."""
        remote_lower = remote_url.lower()
        is_github = "github" in remote_lower
        expected_id = "aisland-github" if is_github else None

        config_file = self.service_data_dir / "credentials.xml"
        if not config_file.exists():
            if expected_id:
                click.echo(
                    f"  - Warning: credentials.xml missing. You MUST add your Git credential with ID '{expected_id}'.",
                    err=True,
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
                    click.echo(
                        f"  - Warning: Credential with ID '{expected_id}' not found in credentials.xml! "
                        f"You MUST name your Jenkins credential exactly '{expected_id}'.",
                        err=True,
                    )
                return expected_id

            # Fallback for non-github: return the first available if any
            if cred_ids:
                return cred_ids[0]

        except Exception as e:
            click.echo(f"  - Error parsing credentials.xml: {e}")

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
                import shutil

                click.echo(f"Removing Jenkins pipeline for project '{project_name}'...")
                shutil.rmtree(project_job_dir)
                click.echo(f"  - Removed: {project_name}")
            else:
                click.echo(
                    f"  - Error: Jenkins job for project '{project_name}' not found.",
                    err=True,
                )
        else:
            import shutil

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

        url = "http://localhost:9080"

        if admin_pass_file.exists():
            password = admin_pass_file.read_text().strip()
            import base64

            auth = base64.b64encode(f"admin:{password}".encode()).decode()
            auth_str = f"Basic {auth}"

            # Get Crumb
            crumb_headers = {}
            try:
                crumb_url = f"{url}/crumbIssuer/api/json"
                req = urllib.request.Request(crumb_url)
                req.add_header("Authorization", auth_str)
                with urllib.request.urlopen(req, timeout=5) as response:
                    import json

                    data = json.loads(response.read().decode())
                    crumb_headers[data["crumbRequestField"]] = data["crumb"]
            except Exception:
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
            except Exception as e:
                click.echo(f"  - Safe Restart API failed: {e}. Falling back to process restart.")
        else:
            click.echo("  - Missing API credentials. Falling back to process restart.")

        self.stop()
        self.launch(background=True)

    def _create_orchestrator_job(self, local: bool, initialized_projects: List[str]) -> None:
        """Create an orchestrator job that builds all discovered projects in topological order."""
        from aisland.manager import get_install_order, get_workspace_name, get_workspace_root

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
                    stages_str += f"                            build job: '{job_name}', wait: true, propagate: true\n"
                    stages_str += "                        }\n"
                    stages_str += "                    }\n"
                else:
                    stages_str += (
                        f"                    steps {{ build job: '{job_name}', wait: true, propagate: true }}\n"
                    )
                stages_str += "                }\n"
            stages_str += "            }\n        }\n"

        pipeline_script = f"""pipeline {{
    agent {{
        node {{
            label 'built-in'
        }}
    }}
    options {{
        disableConcurrentBuilds()
    }}
    stages {{
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

        config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>{description}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>
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

    def scaffold_pipelines(self, project_name: Optional[str] = None, force: bool = False) -> None:
        """Generate standard Jenkinsfile, Jenkinsfile.local, and GitHub Actions for a project or all projects."""
        from aisland.manager import get_project_metadata, get_workspace_root

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
            click.echo(
                f"Error: Project directory '{project_name}' not found at {project_path}.",
                err=True,
            )
            return

        pyproject_path = project_path / "pyproject.toml"
        if not pyproject_path.exists():
            click.echo(
                f"Error: pyproject.toml not found in {project_name}. Cannot scaffold.",
                err=True,
            )
            return

        metadata = get_project_metadata(pyproject_path)
        pkg_name = metadata["name"].replace("-manager", "")
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

        # Internal Gearlux Dependencies — discovered, not hardcoded.
        # Walks .gitmodules at the workspace root, includes every sibling
        # submodule hosted at github.com:Gearlux/* that has a pyproject.toml,
        # and returns each project's PyPI distribution name. Adding a new
        # workspace project is now a zero-aisland-edit operation.
        gearlux_projects = discover_gearlux_projects(workspace_root)

        # Selected extras this project's CI installs: what it DECLARES on each axis
        # (`[tool.aisland] frameworks` / `trackers`) intersected with that axis's COMMITTED
        # selection (the `frameworks` / `trackers` keys of `config.toml`). Committed-only, for the
        # reason the source registry below is: a per-machine `.git/config aisland.frameworks`
        # override must never reach a tracked build file, or one developer's "I only use torch"
        # would silently stop every runner from testing the Keras backend. The same holds for
        # trackers — a runner that stops installing mlflow silently stops running the tests that
        # need it, and they SKIP rather than fail. A project declaring neither emits the
        # historical `.[dev]` unchanged.
        selected_extras: list[str] = []
        for axis in AXES:
            selected_extras += sel_extras_for(
                project_name,
                sel_declared(pyproject_path, axis),
                sel_committed(workspace_root, axis),
            )
        ci_extras = ",".join(["dev"] + selected_extras)

        # A project that REGISTERS a `slow` pytest marker gets its CI test invocations SPLIT:
        # the routine run deselects the slow tests (`-m 'not slow'`) and a separate stage/job
        # runs `-m slow`, so an 8-minute end-to-end smoke test stops sitting on every push's
        # critical path while its dep-bump-drift coverage is preserved. The DECLARATION is the
        # marker registration itself (`[tool.pytest.ini_options] markers`), not a new aisland
        # knob — a project without the marker gets byte-identical output to before, so opting
        # in never churns the other projects' committed CI files. Exit code 5 ("no tests
        # matched the marker") is tolerated in the slow leg: a registered marker with nothing
        # currently marked is a valid state, not a failure.
        with open(pyproject_path, "rb") as fh:
            _pyproject_data = tomllib.load(fh)
        _markers = _pyproject_data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
        has_slow_marker = any(str(m).split(":", 1)[0].strip() == "slow" for m in _markers)
        pytest_deselect = " -m 'not slow'" if has_slow_marker else ""
        if has_slow_marker:
            slow_stage = """
        stage('Slow Tests') {
            steps {
                script {
                    // 5 = the marker is registered but nothing is currently marked — valid, not a failure.
                    def rc = sh(script: "${VENV_BIN}/pytest tests -m slow --junitxml=slow-test-report.xml", returnStatus: true)
                    if (rc != 0 && rc != 5) {
                        error("Slow tests failed (exit ${rc}).")
                    }
                }
            }
            post {
                always {
                    script {
                        if (fileExists('slow-test-report.xml')) {
                            junit allowEmptyResults: true, testResults: 'slow-test-report.xml'
                        }
                    }
                }
            }
        }
"""
        else:
            slow_stage = ""

        # Per-project sources (aisland/sources.py). CI reads ONE bit — is this dep published? —
        # and nothing else. In particular it deliberately IGNORES the configured `ref`.
        #
        # Rationale: `--ref` is a DEVELOPMENT pin ("I'm working against dev/main today"). These
        # three files are tracked and shared, so letting a dev pin decide what a runner builds
        # would (a) publish a personal choice to every consumer's CI and (b) churn 17 projects'
        # committed files on every ref change. A runner therefore always clones `DEFAULT_REF`,
        # which is also what makes `local` and `github` indistinguishable here — the kinds differ
        # only in whether a WORKSPACE checkout exists, and a runner never has one.
        #
        # A pypi-sourced dep drops out of the pre-install loop entirely: that loop exists ONLY
        # because Gearlux distribution names are unpublished (see the block below), and for a
        # published dep the premise is gone — `.[dev]` resolves it from PyPI. The explicit drop
        # matters because the dep may still be checked out on the machine running the scaffold
        # (a `local` override), which would otherwise classify it as internal and emit a git URL.
        #
        # Net effect: the only source change that rewrites a tracked CI file is flipping a dep
        # to or from `pypi`. `aisland source set --ref` regenerates nothing.
        committed_sources = read_committed_sources(workspace_root)

        def _dep_is_pypi(dep: str) -> bool:
            source = source_for_dependency(committed_sources, dep)
            return source is not None and source.kind == "pypi"

        # Collect the internal Gearlux deps CI must pre-install: the closure of
        # exactly what CI's install steps resolve, nothing more. When CI installs
        # a direct dep like `confluid` with --no-deps, `.[dev]` below still needs
        # to resolve confluid's own BASE deps (e.g. loggair) — and Gearlux
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
        #     so they must not add pre-installs — the old rule made
        #     signals-analysis's CI install torpaido/matrainer/annotaide,
        #     reachable only through signals's dev extra and matrainer's
        #     torpaido extra.
        # Specs are parsed with packaging.Requirement: the regex split it
        # replaces couldn't parse `matrainer[torch]>=0.1.0` and silently dropped
        # the spec. Topologically sorted so every package's deps install first.

        def _load_project_table(pyproject: Path) -> dict:
            """The [project] table of one pyproject.toml; {} when missing/unparseable
            (a detached submodule's directory is empty and contributes nothing)."""
            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
            except Exception:
                return {}
            table = data.get("project", {})
            return table if isinstance(table, dict) else {}

        def _gearlux_requirements(project_table: dict, extras: frozenset[str]) -> list[Requirement]:
            """The Gearlux requirements one node contributes: its base dependencies
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
                if req.name in gearlux_projects:
                    reqs.append(req)
            return reqs

        # Map each Gearlux project name to its workspace dir (for locating its
        # pyproject.toml). PyPI-dashed names resolve to dashless dirs.
        def _gearlux_workspace_path(name: str) -> Path:
            if (workspace_root / name).is_dir():
                return workspace_root / name
            stripped = name.replace("-", "")
            return workspace_root / stripped

        # BFS over (dep, requested extras) pairs — the same dep may be reached
        # with different extras sets, and each set can name different siblings.
        root_installed_extras = frozenset(["dev", "notebook", *selected_extras])
        work: list[tuple[str, frozenset[str]]] = [
            (req.name, frozenset(req.extras))
            for req in _gearlux_requirements(_pyproject_data.get("project", {}), root_installed_extras)
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
            sub_table = _load_project_table(_gearlux_workspace_path(dep) / "pyproject.toml")
            work.extend((req.name, frozenset(req.extras)) for req in _gearlux_requirements(sub_table, dep_extras))

        # A project never pre-installs itself: a dependency cycle back to the root
        # (annotaide <-> signals through dev extras) would otherwise install the
        # root's own main branch over the very checkout under test.
        seen.discard(metadata["name"])

        # Topological sort so loggair is installed before confluid, etc.
        # Base deps suffice for ordering (every install runs --no-deps anyway).
        prereqs: dict[str, set[str]] = {name: set() for name in seen}
        for name in seen:
            sub_table = _load_project_table(_gearlux_workspace_path(name) / "pyproject.toml")
            for req in _gearlux_requirements(sub_table, frozenset()):
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
        # Install order: git-URL Gearlux deps FIRST (--no-deps), then `-e .[dev]`.
        # Rationale: all Gearlux distribution names (loggair, recordstream, liquifai,
        # torpaido, confluid, ...) are intentionally NOT published on PyPI. If
        # `.[dev]` ran first, uv would try to resolve `loggair>=0.1.0` from PyPI,
        # fail ("not found in the package registry"), and abort the whole install.
        # By installing each Gearlux dep from its git URL first (with --no-deps to
        # skip its own Gearlux transitive deps — each is handled in its own loop
        # iteration, topologically ordered), we pre-satisfy the constraints so
        # `-e .[dev]` resolves cleanly against the already-installed packages.
        # --system installs into the runner's Python rather than requiring a venv.
        pip_install_cmds = []
        for dep in internal_deps:
            if _dep_is_pypi(dep):
                continue  # published — `.[dev]` resolves it from PyPI, which is the point.
            pip_install_cmds.append(
                f"uv pip install --system --no-deps " f"git+https://github.com/Gearlux/{dep}.git@{DEFAULT_REF}"
            )

        pip_install_str = "\n        ".join(pip_install_cmds)
        if pip_install_str:
            pip_install_str = (
                "\n        # Internal Gearlux dependencies — installed FIRST with --no-deps so"
                "\n        # `-e .[dev]` below finds them pre-satisfied instead of hitting PyPI."
                "\n        " + pip_install_str
            )

        # Detect private Gearlux dependencies — if any, CI needs a GitHub App token
        # to clone them (default GITHUB_TOKEN only grants access to the current repo).
        # is_private_gearlux_repo() queries `gh api` with a per-process cache; it
        # falls back to False (assume public) on any error, so this is safe to call
        # in environments without gh CLI configured.
        # Only deps CI actually clones from GitHub need the token — a pypi-sourced dep is fetched
        # from the public index, so it must not keep the step alive on its own.
        needs_app_token = any(is_private_gearlux_repo(dep) for dep in internal_deps if not _dep_is_pypi(dep))

        if needs_app_token:
            app_token_step = """    - name: Mint GitHub App token for private Gearlux deps
      id: app-token
      uses: actions/create-github-app-token@v1
      with:
        app-id: ${{ secrets.GEARLUX_APP_ID }}
        private-key: ${{ secrets.GEARLUX_APP_PRIVATE_KEY }}
        owner: Gearlux
    - name: Configure git auth for Gearlux
      run: git config --global url."https://x-access-token:${{ steps.app-token.outputs.token }}@github.com/".insteadOf "https://github.com/"
"""
        else:
            app_token_step = ""

        # The GitHub half of the slow-marker split (see `has_slow_marker` above): a SEPARATE job
        # rather than a second step, so it runs in PARALLEL with unit-tests on its own runner —
        # the routine result lands fast while the slow leg proceeds concurrently.
        if has_slow_marker:
            slow_job = f"""
  slow-tests:
    name: Slow Tests
    needs: [quality-gates]
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
{app_token_step}    - name: Set up Python 3.12
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"
    - name: Install uv
      uses: astral-sh/setup-uv@v4
    - name: Install dependencies
      run: |
        {pip_install_str}
        uv pip install --system -e ".[{ci_extras}]"
    - name: Run Slow Tests
      run: |
        rc=0; pytest tests -m slow --junitxml=slow-test-report.xml || rc=$?
        # 5 = the marker is registered but nothing is currently marked — valid, not a failure.
        if [ $rc -ne 0 ] && [ $rc -ne 5 ]; then exit $rc; fi
    - name: Upload Slow Test Results
      uses: actions/upload-artifact@v4
      with:
        name: slow-test-results
        path: slow-test-report.xml
      if: always()
"""
        else:
            slow_job = ""

        # Jenkinsfile (standard) — sh-wrapped, installs from GitHub via uv.
        # Install order identical to the GH Actions path: `.[dev]` first, Gearlux
        # deps LAST with --reinstall so PyPI name collisions get overwritten.
        jenkins_pip_cmds = []
        for dep in internal_deps:
            if _dep_is_pypi(dep):
                continue  # published — `.[dev]` resolves it from PyPI.
            jenkins_pip_cmds.append(
                f'sh "${{VENV_BIN}}/uv pip install --no-deps '
                f'git+https://github.com/Gearlux/{dep}.git@{DEFAULT_REF}"'
            )
        jenkins_pip_str = "\n                ".join(jenkins_pip_cmds)
        if jenkins_pip_str:
            jenkins_pip_str = (
                "\n                // Internal Gearlux dependencies — installed FIRST with --no-deps"
                "\n                // so .[dev] below finds them pre-satisfied instead of hitting PyPI"
                "\n                // (Gearlux distribution names are intentionally unpublished on PyPI)."
                "\n                " + jenkins_pip_str
            )

        # Jenkinsfile.local — sh-wrapped, editable installs from local workspace via uv.
        #
        # Order (mirrors GH Actions): local editables FIRST with --no-deps, then
        # `.[dev]`. Gearlux distribution names are unpublished on PyPI, so `.[dev]`
        # alone can't resolve them. Installing each workspace editable first
        # pre-satisfies the constraint; `.[dev]` then resolves its remaining
        # third-party deps from PyPI without touching the Gearlux ones.
        #
        # PyPI distribution names CAN have dashes ("loggair", "recordstream") but
        # filesystem / workspace directory names are conventionally dashless
        # ("loggair/", "recordstream/"). For each dep, resolve the workspace path:
        # prefer a dir matching the pyproject name exactly, otherwise try the
        # dash-stripped form. This lets a project carry a dashed PyPI name while
        # keeping its Python import and workspace dir as one unbroken word.
        def _dep_to_workspace_dir(dep_name: str) -> str:
            if (workspace_root / dep_name).is_dir():
                return dep_name
            stripped = dep_name.replace("-", "")
            if (workspace_root / stripped).is_dir():
                return stripped
            return dep_name  # fall through; scaffold error visible later

        # This loop reads NO source configuration at all — every internal dep gets the identical
        # guarded line, so Jenkinsfile.local is a function of the dependency LIST alone and is
        # never regenerated by any `aisland source` change.
        #
        # That works because the guard IS the runtime form of the question the config answers:
        # "does this machine have a checkout?" A `local` dep does, and is installed editable; a
        # `github` or `pypi` dep does not (its submodule is deinit'd), and falls back to the
        # remote. Deciding it at build time rather than at scaffold time also means a PER-MACHINE
        # override — which by design never reaches a tracked file — is handled correctly instead
        # of breaking the build on an empty directory.
        #
        # The predicate MUST be `-f <dir>/pyproject.toml`, never `-d <dir>`: `git submodule deinit`
        # empties the working tree but LEAVES THE DIRECTORY, so `-d` is true for every detached dep
        # and the editable branch fires against an empty directory —
        #   error: <dir> does not appear to be a Python project, as neither `pyproject.toml`
        #          nor `setup.py` are present in the directory
        # which is a hard build failure with the git fallback rendered unreachable. Testing for the
        # file the editable install actually consumes asks the real question.
        #
        # The fallback is `DEFAULT_REF` for every dep, including a pypi-sourced one: the local
        # pipeline's job is "does my workspace code build", and testing a published dep at `main`
        # catches breakage BEFORE it is released. The cost is that this one file builds a
        # pypi-sourced dep from git rather than from the released wheel, unlike ci.yml.
        #
        # --no-deps is CRITICAL. Without it, uv would re-resolve this target's transitive Gearlux
        # deps (e.g. confluid -> loggair), and since loggair isn't on PyPI the resolution would
        # fail. The outer dep loop is already topologically ordered (loggair before confluid
        # before liquifai, etc.) so each dep's own deps are installed by the time we get to it.
        local_pip_cmds = []
        for dep in internal_deps:
            ws_dir = _dep_to_workspace_dir(dep)
            git_url = f"git+https://github.com/Gearlux/{dep}.git@{DEFAULT_REF}"
            local_pip_cmds.append(
                f"sh \"if [ -f '${{env.WORKSPACE_ROOT}}/{ws_dir}/pyproject.toml' ]; then "
                f"${{VENV_BIN}}/uv pip install --no-deps -e '${{env.WORKSPACE_ROOT}}/{ws_dir}'; else "
                f'${{VENV_BIN}}/uv pip install --no-deps {git_url}; fi"'
            )
        local_pip_str = "\n                ".join(local_pip_cmds)
        if local_pip_str:
            local_pip_str = (
                "\n                // Internal Gearlux dependencies — installed FIRST with --no-deps"
                "\n                // so .[dev] below finds them pre-satisfied instead of hitting PyPI"
                "\n                // (Gearlux distribution names are intentionally unpublished on PyPI)."
                "\n                " + local_pip_str
            )

        # Helper to write file safely
        def _validate_jenkinsfile(content: str, desc: str) -> bool:
            """POST the content to Jenkins' declarative linter. Returns True if the file
            validates (or if Jenkins isn't running — we can't check then). Returns False
            only when Jenkins actively rejects the file."""
            import urllib.error
            import urllib.parse
            import urllib.request

            try:
                # Fetch CSRF crumb — required for all POSTs even with security disabled.
                crumb_req = urllib.request.Request("http://localhost:9080/crumbIssuer/api/json")
                with urllib.request.urlopen(crumb_req, timeout=3) as resp:
                    crumb_data = json.loads(resp.read().decode())
                crumb_header = {crumb_data["crumbRequestField"]: crumb_data["crumb"]}
            except (urllib.error.URLError, OSError, TimeoutError, KeyError):
                click.echo(f"  [WARN] Jenkins not reachable — skipped validation of {desc}.")
                return True

            try:
                body = urllib.parse.urlencode({"jenkinsfile": content}).encode()
                req = urllib.request.Request(
                    "http://localhost:9080/pipeline-model-converter/validate",
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
                    raise click.ClickException(f"Template produced invalid {desc}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            click.echo(f"  [OK] Created {desc}")

        # 1. Jenkinsfile.local
        local_content = f"""// =========================================================================
// AUTO-GENERATED FILE — DO NOT EDIT BY HAND
// Generated by: aisland jenkins scaffold {project_name}
// Source template: aisland/services/jenkins.py (JenkinsService.scaffold_pipelines)
// To regenerate:  aisland jenkins scaffold {project_name} --force
// =========================================================================
pipeline {{
    agent {{
        node {{
            label 'built-in'
            customWorkspace "${{env.WORKSPACE_ROOT}}/{project_name}"
        }}
    }}

    parameters {{
        booleanParam(
            name: 'REPORT_ALL_WARNINGS',
            defaultValue: false,
            description: 'If true, run every quality gate to completion and report all issues. Default (false) fails fast on the first failing gate.'
        )
    }}

    environment {{
        VENV_PATH = "${{env.WORKSPACE_ROOT}}/.venv"
        VENV_BIN = "${{VENV_PATH}}/bin"
        FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 = 'true'
    }}

    stages {{
        stage('Initialize') {{
            steps {{
                echo 'Validating Environment...'
                sh "${{VENV_BIN}}/python3 --version"
                // uv MUST be present in the workspace venv. It is installed by
                // `aisland setup` (which runs on initial bootstrap) and verified/
                // repaired on every subsequent call. Jenkins jobs deliberately do NOT
                // install it here — that responsibility lives with aisland so the
                // venv stays under a single owner.
                sh "${{VENV_BIN}}/uv --version"
                echo 'Installing Project in Editable Mode...'
                {local_pip_str}
                sh "${{VENV_BIN}}/uv pip install -e .[{ci_extras}]"
                // Notebook-only extras (matplotlib, jupyter kernels, etc.) live
                // in the optional `[notebook]` extra when the project ships
                // notebooks; absence is not an error.
                sh "${{VENV_BIN}}/uv pip install -e .[notebook] 2>/dev/null || echo 'No [notebook] extras declared; continuing with defaults.'"
                sh "${{VENV_BIN}}/uv pip install jupyter nbconvert"
            }}
        }}

        stage('Quality Gates') {{
            failFast true
            parallel {{
                stage('Black') {{
                    steps {{
                        script {{
                            sh "rm -f black-diff.txt black-checkstyle.xml"
                            def targets = sh(script: "for d in {pkg_name} tests examples; do if [ -d \\"\\$d\\" ] && find \\"\\$d\\" -name '*.py' | grep -q .; then printf \\"%s \\" \\"\\$d\\"; fi; done || true", returnStdout: true).trim()
                            if (targets) {{
                                def exitCode = sh(script: "bash -c 'set -o pipefail; ${{VENV_BIN}}/black --check --diff ${{targets}} 2>&1 | tee black-diff.txt'", returnStatus: true)

                                // Generate checkstyle report
                                sh \"\"\"${{VENV_BIN}}/python3 -c "
import sys, os
if not os.path.exists('black-diff.txt'): sys.exit(0)
lines = open('black-diff.txt').readlines()
with open('black-checkstyle.xml', 'w') as f:
    f.write('<?xml version=\\\\"1.0\\\\" encoding=\\\\"UTF-8\\\\"?>\\\\n<checkstyle version=\\\\"5.0\\\\">\\\\n')
    for line in lines:
        if line.startswith('would reformat '):
            path = line.replace('would reformat ', '').strip()
            f.write('  <file name=\\\\"' + path + '\\\\">\\\\n')
            f.write('    <error line=\\\\"1\\\\" severity=\\\\"warning\\\\" message=\\\\"Black would reformat this file\\\\" source=\\\\"black\\\\"/>\\\\n')
            f.write('  </file>\\\\n')
    f.write('</checkstyle>\\\\n')
" \"\"\"
                                if (exitCode != 0) {{
                                    if (params.REPORT_ALL_WARNINGS) {{
                                        unstable("Black found formatting issues. See Checkstyle report.")
                                    }} else {{
                                        error("Black found formatting issues. See Checkstyle report.")
                                    }}
                                }}
                            }} else {{
                                echo "No python files found for Black. Skipping."
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('black-checkstyle.xml')) {{
                                    recordIssues(
                                        id: 'black-{project_name}',
                                        name: 'Black Formatting ({project_name.capitalize()})',
                                        tools: [checkStyle(pattern: 'black-checkstyle.xml')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
                stage('Isort') {{
                    steps {{
                        script {{
                            sh "rm -f isort-diff.txt isort-checkstyle.xml"
                            def targets = sh(script: "for d in {pkg_name} tests examples; do if [ -d \\"\\$d\\" ] && find \\"\\$d\\" -name '*.py' | grep -q .; then printf \\"%s \\" \\"\\$d\\"; fi; done || true", returnStdout: true).trim()
                            if (targets) {{
                                def exitCode = sh(script: "bash -c 'set -o pipefail; ${{VENV_BIN}}/isort --check-only --diff ${{targets}} 2>&1 | tee isort-diff.txt'", returnStatus: true)

                                // Generate checkstyle report
                                sh \"\"\"${{VENV_BIN}}/python3 -c "
import sys, os
if not os.path.exists('isort-diff.txt'): sys.exit(0)
lines = open('isort-diff.txt').readlines()
with open('isort-checkstyle.xml', 'w') as f:
    f.write('<?xml version=\\\\"1.0\\\\" encoding=\\\\"UTF-8\\\\"?>\\\\n<checkstyle version=\\\\"5.0\\\\">\\\\n')
    for line in lines:
        if line.startswith('ERROR: '):
            path = line.split(' ')[1].strip()
            f.write('  <file name=\\\\"' + path + '\\\\">\\\\n')
            f.write('    <error line=\\\\"1\\\\" severity=\\\\"warning\\\\" message=\\\\"Isort import order issues\\\\" source=\\\\"isort\\\\"/>\\\\n')
            f.write('  </file>\\\\n')
    f.write('</checkstyle>\\\\n')
" \"\"\"
                                if (exitCode != 0) {{
                                    if (params.REPORT_ALL_WARNINGS) {{
                                        unstable("Isort found import order issues. See Checkstyle report.")
                                    }} else {{
                                        error("Isort found import order issues. See Checkstyle report.")
                                    }}
                                }}
                            }} else {{
                                echo "No python files found for Isort. Skipping."
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('isort-checkstyle.xml')) {{
                                    recordIssues(
                                        id: 'isort-{project_name}',
                                        name: 'Isort Import Order ({project_name.capitalize()})',
                                        tools: [checkStyle(pattern: 'isort-checkstyle.xml')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
                stage('Flake8') {{
                    steps {{
                        script {{
                            sh "rm -f flake8.txt"
                            def targets = sh(script: "for d in {pkg_name} tests examples; do if [ -d \\"\\$d\\" ] && find \\"\\$d\\" -name '*.py' | grep -q .; then printf \\"%s \\" \\"\\$d\\"; fi; done || true", returnStdout: true).trim()
                            if (targets) {{
                                sh "${{VENV_BIN}}/flake8 ${{targets}} --tee --output-file=flake8.txt"
                            }} else {{
                                echo "No python files found for Flake8. Skipping."
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('flake8.txt') && readFile('flake8.txt').trim()) {{
                                    recordIssues(
                                        id: 'flake8-{project_name}',
                                        name: 'Flake8 ({project_name.capitalize()})',
                                        tools: [flake8(pattern: 'flake8.txt')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
                stage('Mypy') {{
                    steps {{
                        script {{
                            sh "rm -f mypy.txt"
                            // Run from workspace root so root mypy.ini is picked up and cross-project imports resolve.
                            // tee streams errors to the Jenkins console; pipefail ensures mypy's exit code (not tee's) propagates
                            // so the stage fails visibly when types break.
                            def exitCode = sh(script: "bash -c 'set -o pipefail; cd ${{env.WORKSPACE_ROOT}} && ${{VENV_BIN}}/mypy {project_name} 2>&1 | tee ${{env.WORKSPACE_ROOT}}/{project_name}/mypy.txt'", returnStatus: true)
                            if (exitCode != 0) {{
                                if (params.REPORT_ALL_WARNINGS) {{
                                    unstable("Mypy found type errors. See console output above and Mypy report.")
                                }} else {{
                                    error("Mypy found type errors. See console output above and Mypy report.")
                                }}
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('mypy.txt') && readFile('mypy.txt').trim()) {{
                                    recordIssues(
                                        id: 'mypy-{project_name}',
                                        name: 'Mypy ({project_name.capitalize()})',
                                        tools: [myPy(pattern: 'mypy.txt')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}

        stage('Unit Tests') {{
            steps {{
                script {{
                    if (fileExists('tests') && sh(script: "find tests -name '*.py' | grep -q .", returnStatus: true) == 0) {{
                        sh "${{VENV_BIN}}/pytest tests{pytest_deselect} --junitxml=test-report.xml --cov={pkg_name} --cov-report=xml:coverage.xml --cov-report=term"
                    }} else {{
                        echo "No tests found in 'tests' directory. Skipping."
                    }}
                }}
            }}
            post {{
                always {{
                    script {{
                        if (fileExists('test-report.xml')) {{
                            junit allowEmptyResults: true, testResults: 'test-report.xml'
                        }}
                        if (fileExists('coverage.xml')) {{
                            recordCoverage(
                                id: 'coverage-{pkg_name}',
                                name: '{project_name.capitalize()} Coverage',
                                tools: [[parser: 'COBERTURA', pattern: 'coverage.xml']]
                            )
                        }}
                    }}
                }}
            }}
        }}
{slow_stage}
        stage('Verify Examples') {{
            steps {{
                echo 'Running project examples...'
                sh '''
                    for f in examples/*.py; do
                        if [ -f "$f" ]; then
                            echo "Verifying $f..."
                            ${{VENV_BIN}}/python3 "$f"
                        fi
                    done
                    # Directory examples opt in by shipping a run.py entry point;
                    # network-dependent / install-only example apps ship no run.py
                    # and are skipped by construction.
                    for f in examples/*/run.py; do
                        if [ -f "$f" ]; then
                            echo "Verifying $f..."
                            ${{VENV_BIN}}/python3 "$f"
                        fi
                    done
                '''
            }}
        }}

        stage('Verify Notebooks') {{
            when {{
                expression {{ return fileExists('notebooks') }}
            }}
            steps {{
                echo 'Executing project notebooks...'
                sh '''
                    # POSIX-clean: dash (typical /bin/sh on Linux) has no shopt/arrays.
                    # Loop runs zero times when no .ipynb matches (we check found at the end).
                    found=0
                    for nb in notebooks/*.ipynb; do
                        [ -f "$nb" ] || continue
                        found=1
                        echo "Executing $nb..."
                        ${{VENV_BIN}}/jupyter nbconvert --to notebook --execute \\
                            --output "/tmp/$(basename "$nb" .ipynb)-ci.ipynb" \\
                            "$nb"
                    done
                    [ "$found" -eq 0 ] && echo "No notebooks to verify."
                    exit 0
                '''
            }}
        }}

        stage('Verify Frontend') {{
            when {{
                expression {{ return fileExists('frontend/package.json') }}
            }}
            steps {{
                echo 'Installing frontend deps + running tests + production build...'
                dir('frontend') {{
                    sh '''
                        npm ci
                        npm run test --if-present
                        npm run build --if-present
                    '''
                }}
            }}
        }}
    }}

    post {{
        always {{
            echo '{project_name.capitalize()} Pipeline Complete.'
        }}
        success {{
            echo '{project_name.capitalize()} is healthy.'
        }}
        failure {{
            echo '{project_name.capitalize()} build failed. Please check linting or test failures.'
        }}
    }}
}}
"""
        _write_safe(project_path / "Jenkinsfile.local", local_content, "Jenkinsfile.local")

        # 2. Jenkinsfile (Standard) — aligned with Jenkinsfile.local
        standard_content = f"""// =========================================================================
// AUTO-GENERATED FILE — DO NOT EDIT BY HAND
// Generated by: aisland jenkins scaffold {project_name}
// Source template: aisland/services/jenkins.py (JenkinsService.scaffold_pipelines)
// To regenerate:  aisland jenkins scaffold {project_name} --force
// =========================================================================
pipeline {{
    agent any

    parameters {{
        booleanParam(
            name: 'REPORT_ALL_WARNINGS',
            defaultValue: false,
            description: 'If true, run every quality gate to completion and report all issues. Default (false) fails fast on the first failing gate.'
        )
    }}

    environment {{
        VENV_PATH = "${{WORKSPACE}}/.venv"
        VENV_BIN = "${{VENV_PATH}}/bin"
        FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 = 'true'
    }}

    stages {{
        stage('Initialize') {{
            steps {{
                echo 'Creating Isolated Virtual Environment...'
                sh "python3 -m venv ${{VENV_PATH}}"
                echo 'Bootstrapping uv for fast dependency resolution...'
                sh "${{VENV_BIN}}/pip install --upgrade pip uv"
                echo 'Installing Dependencies...'
                {jenkins_pip_str}
                sh "${{VENV_BIN}}/uv pip install -e .[{ci_extras}]"
                // Notebook-only extras (matplotlib, jupyter kernels, etc.) live
                // in the optional `[notebook]` extra when the project ships
                // notebooks; absence is not an error.
                sh "${{VENV_BIN}}/uv pip install -e .[notebook] 2>/dev/null || echo 'No [notebook] extras declared; continuing with defaults.'"
                sh "${{VENV_BIN}}/uv pip install jupyter nbconvert"
            }}
        }}

        stage('Quality Gates') {{
            failFast true
            parallel {{
                stage('Black') {{
                    steps {{
                        script {{
                            sh "rm -f black-diff.txt black-checkstyle.xml"
                            def targets = sh(script: "for d in {pkg_name} tests examples; do if [ -d \\"\\$d\\" ] && find \\"\\$d\\" -name '*.py' | grep -q .; then printf \\"%s \\" \\"\\$d\\"; fi; done || true", returnStdout: true).trim()
                            if (targets) {{
                                def exitCode = sh(script: "bash -c 'set -o pipefail; ${{VENV_BIN}}/black --check --diff ${{targets}} 2>&1 | tee black-diff.txt'", returnStatus: true)

                                // Generate checkstyle report
                                sh \"\"\"${{VENV_BIN}}/python3 -c "
import sys, os
if not os.path.exists('black-diff.txt'): sys.exit(0)
lines = open('black-diff.txt').readlines()
with open('black-checkstyle.xml', 'w') as f:
    f.write('<?xml version=\\\\"1.0\\\\" encoding=\\\\"UTF-8\\\\"?>\\\\n<checkstyle version=\\\\"5.0\\\\">\\\\n')
    for line in lines:
        if line.startswith('would reformat '):
            path = line.replace('would reformat ', '').strip()
            f.write('  <file name=\\\\"' + path + '\\\\">\\\\n')
            f.write('    <error line=\\\\"1\\\\" severity=\\\\"warning\\\\" message=\\\\"Black would reformat this file\\\\" source=\\\\"black\\\\"/>\\\\n')
            f.write('  </file>\\\\n')
    f.write('</checkstyle>\\\\n')
" \"\"\"
                                if (exitCode != 0) {{
                                    if (params.REPORT_ALL_WARNINGS) {{
                                        unstable("Black found formatting issues. See Checkstyle report.")
                                    }} else {{
                                        error("Black found formatting issues. See Checkstyle report.")
                                    }}
                                }}
                            }} else {{
                                echo "No python files found for Black. Skipping."
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('black-checkstyle.xml')) {{
                                    recordIssues(
                                        id: 'black-{project_name}',
                                        name: 'Black Formatting ({project_name.capitalize()})',
                                        tools: [checkStyle(pattern: 'black-checkstyle.xml')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
                stage('Isort') {{
                    steps {{
                        script {{
                            sh "rm -f isort-diff.txt isort-checkstyle.xml"
                            def targets = sh(script: "for d in {pkg_name} tests examples; do if [ -d \\"\\$d\\" ] && find \\"\\$d\\" -name '*.py' | grep -q .; then printf \\"%s \\" \\"\\$d\\"; fi; done || true", returnStdout: true).trim()
                            if (targets) {{
                                def exitCode = sh(script: "bash -c 'set -o pipefail; ${{VENV_BIN}}/isort --check-only --diff ${{targets}} 2>&1 | tee isort-diff.txt'", returnStatus: true)

                                // Generate checkstyle report
                                sh \"\"\"${{VENV_BIN}}/python3 -c "
import sys, os
if not os.path.exists('isort-diff.txt'): sys.exit(0)
lines = open('isort-diff.txt').readlines()
with open('isort-checkstyle.xml', 'w') as f:
    f.write('<?xml version=\\\\"1.0\\\\" encoding=\\\\"UTF-8\\\\"?>\\\\n<checkstyle version=\\\\"5.0\\\\">\\\\n')
    for line in lines:
        if line.startswith('ERROR: '):
            path = line.split(' ')[1].strip()
            f.write('  <file name=\\\\"' + path + '\\\\">\\\\n')
            f.write('    <error line=\\\\"1\\\\" severity=\\\\"warning\\\\" message=\\\\"Isort import order issues\\\\" source=\\\\"isort\\\\"/>\\\\n')
            f.write('  </file>\\\\n')
    f.write('</checkstyle>\\\\n')
" \"\"\"
                                if (exitCode != 0) {{
                                    if (params.REPORT_ALL_WARNINGS) {{
                                        unstable("Isort found import order issues. See Checkstyle report.")
                                    }} else {{
                                        error("Isort found import order issues. See Checkstyle report.")
                                    }}
                                }}
                            }} else {{
                                echo "No python files found for Isort. Skipping."
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('isort-checkstyle.xml')) {{
                                    recordIssues(
                                        id: 'isort-{project_name}',
                                        name: 'Isort Import Order ({project_name.capitalize()})',
                                        tools: [checkStyle(pattern: 'isort-checkstyle.xml')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
                stage('Flake8') {{
                    steps {{
                        script {{
                            sh "rm -f flake8.txt"
                            def targets = sh(script: "for d in {pkg_name} tests examples; do if [ -d \\"\\$d\\" ] && find \\"\\$d\\" -name '*.py' | grep -q .; then printf \\"%s \\" \\"\\$d\\"; fi; done || true", returnStdout: true).trim()
                            if (targets) {{
                                sh "${{VENV_BIN}}/flake8 ${{targets}} --tee --output-file=flake8.txt"
                            }} else {{
                                echo "No python files found for Flake8. Skipping."
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('flake8.txt') && readFile('flake8.txt').trim()) {{
                                    recordIssues(
                                        id: 'flake8-{project_name}',
                                        name: 'Flake8 ({project_name.capitalize()})',
                                        tools: [flake8(pattern: 'flake8.txt')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
                stage('Mypy') {{
                    steps {{
                        script {{
                            sh "rm -f mypy.txt"
                            // tee streams errors to the Jenkins console; pipefail ensures mypy's exit code (not tee's)
                            // propagates so the stage fails visibly when types break.
                            def exitCode = sh(script: "bash -c 'set -o pipefail; ${{VENV_BIN}}/mypy . 2>&1 | tee mypy.txt'", returnStatus: true)
                            if (exitCode != 0) {{
                                if (params.REPORT_ALL_WARNINGS) {{
                                    unstable("Mypy found type errors. See console output above and Mypy report.")
                                }} else {{
                                    error("Mypy found type errors. See console output above and Mypy report.")
                                }}
                            }}
                        }}
                    }}
                    post {{
                        always {{
                            script {{
                                if (fileExists('mypy.txt') && readFile('mypy.txt').trim()) {{
                                    recordIssues(
                                        id: 'mypy-{project_name}',
                                        name: 'Mypy ({project_name.capitalize()})',
                                        tools: [myPy(pattern: 'mypy.txt')]
                                    )
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}

        stage('Unit Tests') {{
            steps {{
                script {{
                    if (fileExists('tests') && sh(script: "find tests -name '*.py' | grep -q .", returnStatus: true) == 0) {{
                        sh "${{VENV_BIN}}/pytest tests{pytest_deselect} --junitxml=test-report.xml --cov={pkg_name} --cov-report=xml:coverage.xml --cov-report=term"
                    }} else {{
                        echo "No tests found in 'tests' directory. Skipping."
                    }}
                }}
            }}
            post {{
                always {{
                    script {{
                        if (fileExists('test-report.xml')) {{
                            junit allowEmptyResults: true, testResults: 'test-report.xml'
                        }}
                        if (fileExists('coverage.xml')) {{
                            recordCoverage(
                                id: 'coverage-{pkg_name}',
                                name: '{project_name.capitalize()} Coverage',
                                tools: [[parser: 'COBERTURA', pattern: 'coverage.xml']]
                            )
                        }}
                    }}
                }}
            }}
        }}
{slow_stage}
        stage('Verify Examples') {{
            steps {{
                echo 'Running project examples...'
                sh '''
                    for f in examples/*.py; do
                        if [ -f "$f" ]; then
                            echo "Verifying $f..."
                            ${{VENV_BIN}}/python3 "$f"
                        fi
                    done
                    # Directory examples opt in by shipping a run.py entry point;
                    # network-dependent / install-only example apps ship no run.py
                    # and are skipped by construction.
                    for f in examples/*/run.py; do
                        if [ -f "$f" ]; then
                            echo "Verifying $f..."
                            ${{VENV_BIN}}/python3 "$f"
                        fi
                    done
                '''
            }}
        }}

        stage('Verify Notebooks') {{
            when {{
                expression {{ return fileExists('notebooks') }}
            }}
            steps {{
                echo 'Executing project notebooks...'
                sh '''
                    # POSIX-clean: dash (typical /bin/sh on Linux) has no shopt/arrays.
                    # Loop runs zero times when no .ipynb matches (we check found at the end).
                    found=0
                    for nb in notebooks/*.ipynb; do
                        [ -f "$nb" ] || continue
                        found=1
                        echo "Executing $nb..."
                        ${{VENV_BIN}}/jupyter nbconvert --to notebook --execute \\
                            --output "/tmp/$(basename "$nb" .ipynb)-ci.ipynb" \\
                            "$nb"
                    done
                    [ "$found" -eq 0 ] && echo "No notebooks to verify."
                    exit 0
                '''
            }}
        }}

        stage('Verify Frontend') {{
            when {{
                expression {{ return fileExists('frontend/package.json') }}
            }}
            steps {{
                echo 'Installing frontend deps + running tests + production build...'
                dir('frontend') {{
                    sh '''
                        npm ci
                        npm run test --if-present
                        npm run build --if-present
                    '''
                }}
            }}
        }}
    }}

    post {{
        always {{
            echo '{project_name.capitalize()} Pipeline Complete.'
        }}
        success {{
            echo '{project_name.capitalize()} is healthy.'
        }}
        failure {{
            echo '{project_name.capitalize()} build failed. Please check linting or test failures.'
        }}
    }}
}}
"""
        _write_safe(project_path / "Jenkinsfile", standard_content, "Jenkinsfile")

        # 3. GitHub Actions (.github/workflows/ci.yml)
        gh_content = f"""# =========================================================================
# AUTO-GENERATED FILE — DO NOT EDIT BY HAND
# Generated by: aisland jenkins scaffold {project_name}
# Source template: aisland/services/jenkins.py (JenkinsService.scaffold_pipelines)
# To regenerate:  aisland jenkins scaffold {project_name} --force
# =========================================================================
name: {project_name.capitalize()} CI

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  quality-gates:
    name: Quality Gates
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
{app_token_step}    - name: Set up Python 3.12
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"
    - name: Install uv
      uses: astral-sh/setup-uv@v4
    - name: Install dependencies
      run: |
        {pip_install_str}
        uv pip install --system -e ".[{ci_extras}]"
    - name: Run Isort
      run: isort --check-only .
    - name: Run Black
      run: black --check .
    - name: Run Flake8
      run: flake8 .
    - name: Run Mypy
      run: mypy .

  unit-tests:
    name: Unit Tests & Coverage
    needs: [quality-gates]
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
{app_token_step}    - name: Set up Python 3.12
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"
    - name: Install uv
      uses: astral-sh/setup-uv@v4
    - name: Install dependencies
      run: |
        {pip_install_str}
        uv pip install --system -e ".[{ci_extras}]"
    - name: Run Tests
      run: |
        if [ -d tests ] && find tests -name '*.py' | grep -q .; then
          pytest tests{pytest_deselect} --junitxml=test-report.xml --cov={pkg_name} --cov-report=xml --cov-report=term
        else
          echo "No tests found. Skipping."
        fi
    - name: Upload Test Results
      uses: actions/upload-artifact@v4
      with:
        name: test-results
        path: test-report.xml
      if: always()
{slow_job}
  verify-examples:
    name: Verify Examples
    needs: [unit-tests]
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
{app_token_step}    - name: Set up Python 3.12
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"
    - name: Install uv
      uses: astral-sh/setup-uv@v4
    - name: Install dependencies
      run: |
        {pip_install_str}
        uv pip install --system -e ".[{ci_extras}]"
    - name: Run Examples
      run: |
        for f in examples/*.py; do
          if [ -f "$f" ]; then
            echo "Verifying $f..."
            python3 "$f"
          fi
        done
        # Directory examples opt in by shipping a run.py entry point;
        # network-dependent / install-only example apps ship no run.py
        # and are skipped by construction.
        for f in examples/*/run.py; do
          if [ -f "$f" ]; then
            echo "Verifying $f..."
            python3 "$f"
          fi
        done

  verify-notebooks:
    name: Verify Notebooks
    needs: [unit-tests]
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
{app_token_step}    - name: Set up Python 3.12
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"
    - name: Install uv
      uses: astral-sh/setup-uv@v4
    - name: Install dependencies
      run: |
        {pip_install_str}
        uv pip install --system -e ".[{ci_extras}]"
        # Notebook-only dependencies live in the optional `[notebook]` extra
        # when a project ships notebooks; absence is not an error.
        uv pip install --system -e ".[notebook]" 2>/dev/null || echo "No [notebook] extras declared; continuing with defaults."
        uv pip install --system jupyter nbconvert
    - name: Execute Notebooks
      run: |
        if [ ! -d notebooks ]; then
          echo "No notebooks directory. Skipping."
          exit 0
        fi
        shopt -s nullglob
        nbs=(notebooks/*.ipynb)
        if [ ${{#nbs[@]}} -eq 0 ]; then
          echo "No notebooks to verify."
          exit 0
        fi
        for nb in "${{nbs[@]}}"; do
          echo "Executing $nb..."
          jupyter nbconvert --to notebook --execute \\
            --output "/tmp/$(basename "$nb" .ipynb)-ci.ipynb" \\
            "$nb"
        done

  verify-frontend:
    name: Verify Frontend
    needs: [unit-tests]
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - name: Detect frontend
      id: detect
      run: |
        if [ -f frontend/package.json ]; then
          echo "present=true" >> "$GITHUB_OUTPUT"
        else
          echo "present=false" >> "$GITHUB_OUTPUT"
          echo "No frontend/package.json. Skipping."
        fi
    - name: Set up Node.js
      if: steps.detect.outputs.present == 'true'
      uses: actions/setup-node@v4
      with:
        node-version: "20"
    - name: Install frontend dependencies
      if: steps.detect.outputs.present == 'true'
      working-directory: frontend
      run: npm ci
    - name: Run frontend tests
      if: steps.detect.outputs.present == 'true'
      working-directory: frontend
      run: npm run test --if-present
    - name: Build frontend
      if: steps.detect.outputs.present == 'true'
      working-directory: frontend
      run: npm run build --if-present
"""
        _write_safe(
            project_path / ".github" / "workflows" / "ci.yml",
            gh_content,
            "GitHub CI Workflow",
        )

    def init_pipelines(self, project_name: Optional[str] = None, local: bool = False) -> None:
        """Seed Jenkins with Multi-branch pipelines for projects discovered in the workspace."""
        self.setup()
        # setup_plugins returns True if it actually installed/updated something
        plugins_changed = self.setup_plugins()

        workspace_root = Path(__file__).resolve().parents[3]

        if project_name:
            project_path = workspace_root / project_name
            if not project_path.exists() or not project_path.is_dir():
                click.echo(
                    f"  - Error: Project directory '{project_name}' not found at {project_path}.",
                    err=True,
                )
                return
            if not local and not (project_path / "Jenkinsfile").exists():
                click.echo(
                    f"  - Error: Project '{project_name}' does not have a Jenkinsfile at its root.",
                    err=True,
                )
                return
            if local and not (project_path / "Jenkinsfile.local").exists():
                click.echo(
                    f"  - Error: Project '{project_name}' does not have a Jenkinsfile.local at its root.",
                    err=True,
                )
                return
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
                    script_content = jenkinsfile_path.read_text()
                    # Escape XML entities in the script
                    from xml.sax.saxutils import escape

                    escaped_script = escape(script_content)

                    config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>Local development pipeline for {project_base_name}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{escaped_script}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
  <customWorkspace>{project_path.absolute()}</customWorkspace>
</flow-definition>
"""
                    config_file = project_job_dir / "config.xml"
                    config_file.write_text(config_xml)
                    click.echo(f"  - Seeded local job (embedded script): {project}")
                    continue
                else:
                    click.echo(f"  - Warning: {jenkinsfile_path} not found for {project}. Skipping.")
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
                click.echo("  - Warning: config.xml did not appear within 90s.")
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
        from aisland.manager import get_workspace_name

        base_name = get_workspace_name()
        if name is None:
            name = f"{base_name.capitalize()} Ecosystem Dashboard"

        config_file = self.service_data_dir / "config.xml"
        if not config_file.exists():
            click.echo("  - Jenkins config.xml not found. Skipping view setup until first run.")
            return

        import xml.etree.ElementTree as ET

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()
        except Exception as e:
            click.echo(f"  - Error parsing config.xml: {e}")
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

        import xml.etree.ElementTree as ET

        from aisland.manager import get_workspace_root

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()
        except Exception as e:
            click.echo(f"  - Error parsing config.xml for global properties: {e}")
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
        dotenv_vars = _parse_dotenv(dotenv_path)
        if dotenv_vars:
            click.echo(f"  - Loaded {len(dotenv_vars)} key(s) from {dotenv_path}")
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

        import xml.etree.ElementTree as ET

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()
        except Exception as e:
            click.echo(f"  - Error parsing config.xml for security reduction: {e}")
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
