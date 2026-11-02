"""The local Jenkins service: its WAR, the Java it runs on, its plugins, its credentials, starting and stopping it.

The jobs aisland seeds into it are :mod:`aisland.services.jenkins_jobs`; the CI files those jobs run are
:mod:`aisland.ci_scaffold`'s.
"""

import base64
import json
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

import click

from aisland import failures, provision
from aisland.downloads import TIMEOUT, download
from aisland.errors import AislandError
from aisland.installers import install_brew_packages
from aisland.paths import BIN_DIR
from aisland.projects import get_workspace_root
from aisland.services.base import BaseService
from aisland.services.jenkins_java import JenkinsJava, choose_java, find_runtimes, newest_versioned_formula

logger = logging.getLogger(__name__)


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

    def _get_latest_manager_info(self) -> Tuple[Dict[str, Any], Optional[str]]:  # Any: GitHub's release JSON
        """The plugin manager's latest release from GitHub's API, and why there is none when it cannot be read. Whether
        that fails the run is :meth:`setup`'s question: a manager downloaded before still serves."""
        api_url = f"https://api.github.com/repos/{self.REPO}/releases/latest"
        headers = {"User-Agent": "aisland"}
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                data = json.loads(response.read().decode())
        except (OSError, ValueError) as error:  # no answer, or no JSON
            return {}, f"GitHub's API did not answer ({error})"
        if not isinstance(data, dict):
            return {}, "GitHub's API answered no release"
        return data, None

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
        installed: Set[str] = set()
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
        info, unread = self._get_latest_manager_info()
        if unread is not None and self.manager_jar.exists():
            logger.debug("the plugin manager's latest release is not known (%s); %s serves", unread, self.manager_jar)
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
            cause = f": {unread}" if unread is not None else ""
            failures.report(f"  - Error: Plugin Manager could not be discovered or downloaded{cause}.")

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
