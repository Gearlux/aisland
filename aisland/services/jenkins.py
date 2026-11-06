"""The local Jenkins service: its WAR, the Java it runs on, its plugins, its credentials, starting and stopping it.

The jobs aisland seeds into it are :mod:`aisland.services.jenkins_jobs`; the CI files those jobs run are
:mod:`aisland.ci.ci_scaffold`'s.
"""

import base64
import json
import logging
import os
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Set

import click

from aisland import console, failures, paths
from aisland.downloads import download, sha256
from aisland.errors import AislandError
from aisland.services.base import BaseService
from aisland.services.jenkins_java import JenkinsJava, choose_java, find_runtimes, newest_versioned_formula
from aisland.setup import provision
from aisland.setup.installers import install_brew_packages
from aisland.workspace.projects import get_workspace_root

logger = logging.getLogger(__name__)

#: How long the plugin manager may take to download and resolve every plugin: minutes on a first `jenkins init`.
PLUGIN_INSTALL_SECONDS = 30 * 60


class JenkinsService(BaseService):
    """Jenkins CI/CD Server."""

    #: The Jenkins release aisland runs, a long-term-support WAR pinned by version AND sha256 (jenkins.io publishes the
    #: digest beside every WAR, as ``jenkins.war.sha256``). A download that does not match is refused. To move to a
    #: newer release, change both from ``https://get.jenkins.io/war-stable/<version>/jenkins.war.sha256``.
    WAR_VERSION = "2.555.2"
    WAR_SHA256 = "dfda09578c5c1b18f28d50ec362868305f572c3b7aef8b90d00f7a9436a62867"
    URL = f"https://get.jenkins.io/war-stable/{WAR_VERSION}/jenkins.war"
    #: The plugin installation manager aisland runs, pinned the same way: its GitHub release publishes the digest
    #: beside the jar (``jenkins-plugin-manager-<version>.jar.sha256``). Move both together.
    MANAGER_VERSION = "2.15.0"
    MANAGER_SHA256 = "a86853ec2e2933f37a4b471ba65099b61e03c87a80c2ef8fe2315eb135672d43"
    MANAGER_URL = (
        "https://github.com/jenkinsci/plugin-installation-manager-tool/releases/download/"
        f"{MANAGER_VERSION}/jenkins-plugin-manager-{MANAGER_VERSION}.jar"
    )
    #: The port Jenkins starts on unless `--port` says otherwise; everything else asks :meth:`url`.
    DEFAULT_PORT = 9080
    #: The one address Jenkins listens on: this machine's own. Jenkins' default is every interface, which on a shared
    #: network would offer its script console (code run as you) and the `.env` values its jobs see to anyone near.
    LISTEN_ADDRESS = "127.0.0.1"

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
        self.war_file = paths.bin_dir() / "jenkins.war"
        self.manager_jar = paths.bin_dir() / f"jenkins-plugin-manager-{self.MANAGER_VERSION}.jar"

    def ensure_war(self) -> bool:
        """Download the pinned Jenkins WAR unless one is already in the bin dir; return whether it is there now. A WAR
        already in the bin dir is used as it is."""
        return _fetch_pinned(f"Jenkins {self.WAR_VERSION}", self.URL, self.war_file, self.WAR_SHA256)

    def ensure_manager(self) -> bool:
        """Download the pinned plugin manager unless it is already in the bin dir; return whether it is there now. Once
        it is, the jars of other versions an older aisland downloaded are removed."""
        if not _fetch_pinned(
            f"Jenkins Plugin Manager {self.MANAGER_VERSION}", self.MANAGER_URL, self.manager_jar, self.MANAGER_SHA256
        ):
            return False
        for other in self.manager_jar.parent.glob("jenkins-plugin-manager-*.jar"):
            if other != self.manager_jar:
                other.unlink()
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
        console.echo(choice.describe())
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
        console.echo("==> Choosing the Java Jenkins runs on...")
        if not self.ensure_war():
            console.echo("  - Skipped: without the Jenkins WAR there is no asking which Java it runs on.")
            return
        choice = self.resolve_java()
        installed: Set[str] = set()
        while choice.runtime is None:
            formula = choice.formula or newest_versioned_formula()
            if formula is None or formula in installed:
                break
            console.echo(f"  - {choice.problem()}.")
            install_brew_packages({formula})
            installed.add(formula)
            choice = self.resolve_java()
        if choice.runtime is None:
            console.echo(choice.refusal())
            return
        console.echo(f"  - {choice.describe()}.")

    def setup(self) -> None:
        """Download the Jenkins WAR (once the user agrees) and the plugin manager. A failed download is reported: the
        command exits 1 (aisland.failures)."""
        if self.ensure_war_asking():
            self.ensure_manager()

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
            console.echo(f"  - Missing plugins: {', '.join(missing)}")
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
            console.echo("  - Downloading necessary Jenkins plugins...")
            logger.debug("running %s", " ".join(cmd))
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=PLUGIN_INSTALL_SECONDS)
            if "Installed" in result.stdout or "Done" in result.stdout:
                console.echo("  - Jenkins plugins and dependencies synchronized.")
            return True
        except subprocess.CalledProcessError as e:
            failures.report(f"  - Error installing plugins: {e.stderr}")
            return False
        except subprocess.TimeoutExpired:
            failures.report(
                f"  - Error installing plugins: the plugin manager gave no result in {PLUGIN_INSTALL_SECONDS} s"
            )
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
            console.echo("\n" + "=" * 65)
            console.echo(" Jenkins Initial Setup / Missing Credentials ".center(65, "="))
            console.echo("=" * 65)
            console.echo("No Git credentials found. To allow Jenkins to pull code:")
            console.echo("  1. Open Jenkins at " + click.style(f"http://localhost:{port}", fg="blue", bold=True))
            if self.admin_password_file.exists():
                console.echo(f"  2. Unlock it with the password in {self.admin_password_file}")
            console.echo("  3. Navigate to: Manage Jenkins -> Credentials -> System ->")
            console.echo("     Global credentials -> Add Credentials")
            console.echo("  4. Add your Git username/password or SSH key.")
            console.echo(
                "     "
                + click.style(
                    "CRITICAL: You MUST set the ID field to 'aisland-github'",
                    fg="red",
                    bold=True,
                )
            )
            console.echo(
                "  5. Run " + click.style("aisland jenkins init", fg="green") + " to install plugins and populate jobs."
            )
            console.echo("=" * 65 + "\n")

    def launch(self, port: int = DEFAULT_PORT, background: bool = False) -> None:
        """Start the Jenkins server."""
        self.setup()
        java = self.java_asking()
        console.echo(f"Jenkins: http://localhost:{port}")
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
        cmd = [
            str(java),
            *jvm_args,
            "-jar",
            str(self.war_file),
            f"--httpPort={port}",
            f"--httpListenAddress={self.LISTEN_ADDRESS}",
        ]
        workspace_root = get_workspace_root()
        venv_bin = workspace_root / ".venv" / "bin"
        # The bin folder (aisland/bin) hosts symlinks to userspace-installed tools like
        # node/npm/npx (see installers.install_userspace_tool). Putting it on
        # Jenkins' PATH means every job inherits these without per-Jenkinsfile
        # plumbing. Order: venv first (its `python`/`uv` shadow any system ones),
        # then aisland-vendored tools, then whatever the parent shell exposed.
        env = {
            "JENKINS_HOME": str(self.service_data_dir),
            "PATH": f"{venv_bin}:{paths.bin_dir()}:{os.environ.get('PATH', '')}",
        }

        # /login answers 200 as soon as the Jenkins web UI is up (even while
        # the setup wizard is pending), which is exactly "ready" for aisland.
        self.start(cmd, env=env, background=background, readiness={"port": port, "path": "/login"})

    @property
    def admin_password_file(self) -> Path:
        """The file Jenkins writes its initial admin password to. aisland names the file and never prints the password:
        a command's output reaches terminals' scrollback, CI logs and, through `aisland-mcp`, an AI client."""
        return self.service_data_dir / "secrets" / "initialAdminPassword"

    def status(self) -> None:
        """Show extended Jenkins status."""
        super().status()
        if self.is_running() and self.admin_password_file.exists():
            console.echo(f"    Initial admin password: in {self.admin_password_file}")

    def url(self) -> str:
        """Where Jenkins answers: the port it runs on, else :attr:`DEFAULT_PORT` (it is not running, or not here)."""
        ports = self.get_ports()
        return f"http://localhost:{ports[0] if ports else self.DEFAULT_PORT}"

    def info(self) -> None:
        """Show detailed Jenkins configuration."""
        super().info()
        console.echo(f"  URL           : {self.url()}")
        console.echo(f"  JENKINS_HOME  : {self.service_data_dir}")
        console.echo(f"  WAR file      : {self.war_file}")
        if self.war_file.exists():
            console.echo(f"  Java          : {self.resolve_java().describe()}")
        console.echo(f"  Plugin manager: {self.manager_jar}")
        console.echo(f"  Plugins dir   : {self.service_data_dir / 'plugins'}")
        console.echo(f"  Jobs dir      : {self.service_data_dir / 'jobs'}")
        if self.admin_password_file.exists():
            console.echo("  Admin user    : admin")
            console.echo(f"  Admin password: in {self.admin_password_file}")
        else:
            console.echo("  Admin password: (not yet generated — start Jenkins to create it)")
        creds_file = self.service_data_dir / "credentials.xml"
        console.echo(f"  Credentials   : {creds_file} " + ("(present)" if creds_file.exists() else "(not configured)"))

    def safe_restart(self) -> None:
        """Issue a safeRestart command to Jenkins."""
        if not self.is_running():
            return

        console.echo("  - Issuing Safe Restart to Jenkins (waiting for active jobs)...")
        url = self.url()

        if self.admin_password_file.exists():
            password = self.admin_password_file.read_text().strip()
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
                console.echo("  - Safe Restart initiated. Jenkins will reload soon.")
                return
            except (OSError, ValueError) as e:  # no answer, a refusal (HTTPError is an OSError), no JSON
                console.echo(f"  - Safe Restart API failed: {e}. Falling back to process restart.")
        else:
            console.echo("  - Missing API credentials. Falling back to process restart.")

        self.stop()
        self.launch(background=True)


def _fetch_pinned(what: str, url: str, target: Path, digest: str) -> bool:
    """``url`` downloaded to ``target`` unless a file is there already; whether it is there now.

    The download lands in a ``.part`` file beside the target and moves into place only once its sha256 is ``digest``,
    so a refused or interrupted download leaves nothing a later run would take for a finished one. A failure is
    reported (the command exits 1) and nothing is installed."""
    if target.exists():
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    console.echo(f"Downloading {what} from {url}...")
    part = target.with_name(target.name + ".part")
    try:
        download(url, part)
        actual = sha256(part)
        if actual != digest:
            part.unlink()
            failures.report(
                f"Error downloading {what}: {url} has the sha256 {actual}, not the pinned {digest}; nothing was installed."
            )
            return False
        os.replace(part, target)
    except OSError as e:
        part.unlink(missing_ok=True)
        failures.report(f"Error downloading {what}: {e}")
        return False
    console.echo(f"  - {what} download complete.")
    return True
