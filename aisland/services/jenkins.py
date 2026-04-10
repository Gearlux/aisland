import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List, Optional, cast

import click

from aisland.manager import BIN_DIR, BaseService


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

    def launch(self, port: int = 8080, background: bool = False) -> None:
        """Start the Jenkins server."""
        self.setup()
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
        env = {
            "JENKINS_HOME": str(self.service_data_dir),
            "PATH": f"{venv_bin}:{os.environ.get('PATH', '')}",
        }

        self.start(cmd, env=env, background=background)

    def status(self) -> None:
        """Show extended Jenkins status."""
        super().status()
        admin_pass_file = self.service_data_dir / "secrets/initialAdminPassword"
        if self.is_running() and admin_pass_file.exists():
            password = admin_pass_file.read_text().strip()
            click.echo(f"    Initial Admin Password: {password}")

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

        url = "http://localhost:8080"

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
        from aisland.manager import get_install_order, get_workspace_root

        workspace_root = get_workspace_root()
        install_order = get_install_order(workspace_root)

        initialized_names = {p for p in initialized_projects}

        depths: dict[str, int] = {}
        for p in install_order:
            name = p["name"]
            if name not in initialized_names:
                continue

            deps = []
            for dep in p["dependencies"]:
                dep_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
                if dep_name in initialized_names and dep_name != name:
                    deps.append(dep_name)

            max_dep_depth = -1
            for d in deps:
                max_dep_depth = max(max_dep_depth, depths.get(d, -1))

            depths[name] = max_dep_depth + 1

        phases: dict[int, list[str]] = {}
        for name, d in depths.items():
            phases.setdefault(d, []).append(name)

        stages_str = ""
        for depth in sorted(phases.keys()):
            phase_projects = phases[depth]
            if not phase_projects:
                continue

            stages_str += f"        stage('Phase {depth}') {{\n            parallel {{\n"
            for proj in phase_projects:
                job_name = f"{proj}.local" if local else f"{proj}/main"
                stages_str += f"                stage('Build {proj}') {{\n"
                stages_str += f"                    steps {{ build job: '{job_name}', wait: true, propagate: true }}\n"
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

        from aisland.manager import get_workspace_name

        escaped_script = escape(pipeline_script)

        base_name = get_workspace_name()
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

    def init_pipelines(self, project_name: Optional[str] = None, local: bool = False) -> None:
        """Seed Jenkins with Multi-branch pipelines for projects discovered in the workspace."""
        self.setup()
        was_running = self.is_running()
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

        self.setup_dashboard_view()
        self.setup_global_properties()

        if local:
            if click.confirm("Reduce Jenkins security for faster local access? (Disables CSRF, allows anonymous read)"):
                self.reduce_security()

        if was_running:
            # We always restart after seeding to ensure jobs and plugins are loaded correctly
            self.safe_restart()
            if plugins_changed:
                click.echo("Jenkins plugins updated and restart initiated.")
            else:
                click.echo("Jenkins pipelines seeded and restart initiated.")
        else:
            click.echo("Starting Jenkins to load the new plugins and pipelines...")
            self.launch(background=True)
            click.echo("Jenkins started in the background.")

    def setup_dashboard_view(self, name: str = "Gearlux Ecosystem Dashboard") -> None:
        """Create a Build Monitor View for the Gearlux Ecosystem branching workflow."""
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

        # Check if the view already exists
        view_exists = False
        for v in views:
            if v.findtext("name") == name:
                view_exists = True
                break

        if not view_exists:
            click.echo(f"  - Creating '{name}' view (branches: main, dev/main) " f"with Build Monitor View plugin...")

            # Add Build Monitor View
            bm_view = ET.SubElement(
                views,
                "com.smartcodeltd.jenkinsci.plugins.buildmonitor.BuildMonitorView",
            )
            bm_view.set("plugin", "build-monitor-plugin")

            ET.SubElement(bm_view, "owner").set("class", "hudson")
            ET.SubElement(bm_view, "owner").set("reference", "../../..")
            ET.SubElement(bm_view, "name").text = name
            ET.SubElement(bm_view, "filterExecutors").text = "false"
            ET.SubElement(bm_view, "filterQueue").text = "false"
            ET.SubElement(bm_view, "properties").set("class", "hudson.model.View$PropertyList")
            ET.SubElement(bm_view, "jobNames").set("class", "hudson.util.CaseInsensitiveComparator")

            job_filters = ET.SubElement(bm_view, "jobFilters")

            # Regex Filter for standard Gearlux branches: main and dev/main
            regex_filter = ET.SubElement(job_filters, "hudson.views.RegExJobFilter")
            regex_filter.set("plugin", "view-job-filters")
            ET.SubElement(regex_filter, "includeExcludeTypeString").text = "includeMatched"
            ET.SubElement(regex_filter, "valueTypeString").text = "NAME"
            ET.SubElement(regex_filter, "regex").text = "^(main|dev/main)$"
            ET.SubElement(regex_filter, "matchCanary").text = "false"

            ET.SubElement(bm_view, "columns").set("class", "empty-list")
            ET.SubElement(bm_view, "recurse").text = "true"
            ET.SubElement(bm_view, "title").text = name

            config = ET.SubElement(bm_view, "config")
            ET.SubElement(config, "displayCommitters").text = "true"
            ET.SubElement(config, "displayLowPriorityJobs").text = "false"
            ET.SubElement(config, "displayLastBuildConsoleOutput").text = "true"
            ET.SubElement(config, "order").set("class", "com.smartcodeltd.jenkinsci.plugins.buildmonitor.order.ByName")

            # Revert primary view to 'all' to ensure standard overview is default
            primary_view = root.find("primaryView")
            if primary_view is not None:
                primary_view.text = "all"

            # Robust write
            tree.write(config_file, encoding="UTF-8", xml_declaration=True)
        else:
            click.echo(f"  - Dashboard view '{name}' already exists.")

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

        # In recent Jenkins, envVars is often serialized as a custom TreeMap
        env_vars = env_vars_prop.find("envVars")
        if env_vars is not None:
            env_vars_prop.remove(env_vars)

        env_vars = ET.SubElement(env_vars_prop, "envVars")
        env_vars.set("serialization", "custom")
        ET.SubElement(env_vars, "unserializable-parents")
        tree_map = ET.SubElement(env_vars, "tree-map")

        # Add Comparator
        default_elem = ET.SubElement(tree_map, "default")
        ET.SubElement(default_elem, "comparator").set("class", "hudson.util.CaseInsensitiveComparator")

        workspace_root = get_workspace_root()
        env_data = {
            "WORKSPACE_ROOT": str(workspace_root),
            "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": "true",
        }

        # Number of entries
        ET.SubElement(tree_map, "int").text = str(len(env_data))

        # Add key-value pairs
        for key, value in env_data.items():
            ET.SubElement(tree_map, "string").text = key
            ET.SubElement(tree_map, "string").text = value
            click.echo(f"  - Added global Jenkins property: {key}={value}")

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
