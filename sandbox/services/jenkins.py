import urllib.request
import click
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from sandbox.manager import BIN_DIR, BaseService


class JenkinsService(BaseService):
    """Jenkins CI/CD Server."""

    URL = "https://get.jenkins.io/war-stable/latest/jenkins.war"
    PLUGINS = [
        "build-monitor-plugin",
        "view-job-filters",
        "dashboard-view"
    ]

    def __init__(self) -> None:
        super().__init__("jenkins")
        self.war_file = BIN_DIR / "jenkins.war"

    def setup(self) -> None:
        """Download Jenkins binary."""
        if not self.war_file.exists():
            click.echo(f"Downloading Jenkins from {self.URL}...")
            try:
                BIN_DIR.mkdir(exist_ok=True)
                urllib.request.urlretrieve(self.URL, self.war_file)
                click.echo("Download complete.")
            except Exception as e:
                click.echo(f"Error downloading Jenkins: {e}", err=True)
                sys.exit(1)

    def setup_plugins(self) -> None:
        """Download Jenkins plugins."""
        plugins_dir = self.service_data_dir / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        
        click.echo("Verifying Jenkins plugins...")
        for plugin in self.PLUGINS:
            plugin_file = plugins_dir / f"{plugin}.jpi"
            if not plugin_file.exists():
                click.echo(f"  - Downloading {plugin}...")
                url = f"https://updates.jenkins.io/latest/{plugin}.hpi"
                try:
                    urllib.request.urlretrieve(url, plugin_file)
                except Exception as e:
                    click.echo(f"    - Error downloading {plugin}: {e}", err=True)

    def launch(self, port: int = 8080, background: bool = False) -> None:
        """Start the Jenkins server."""
        self.setup()
        self.setup_plugins()
        
        cmd = ["java", "-jar", str(self.war_file), f"--httpPort={port}"]
        env = {"JENKINS_HOME": str(self.service_data_dir)}
        
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
                check=True
            )
            return result.stdout.strip()
        except Exception:
            return None

    def _find_credentials(self, remote_url: str) -> str:
        """Find matching credentials in Jenkins configuration based on URL fuzzy matching."""
        config_file = self.service_data_dir / "credentials.xml"
        if not config_file.exists():
            return ""

        try:
            tree = ET.parse(config_file)
            root = tree.getroot()
            
            # Extract all credential IDs and their descriptions/usernames
            creds = []
            # Check both UsernamePassword and SSH Private Key credentials
            for entry in root.findall(".//com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl"):
                creds.append({
                    "id": entry.findtext("id"),
                    "username": entry.findtext("username"),
                    "description": entry.findtext("description") or ""
                })
            for entry in root.findall(".//com.cloudbees.jenkins.plugins.sshcredentials.impl.BasicSSHUserPrivateKey"):
                creds.append({
                    "id": entry.findtext("id"),
                    "username": entry.findtext("username"),
                    "description": entry.findtext("description") or ""
                })

            remote_lower = remote_url.lower()
            
            # 1. Direct match with username (e.g. gertbehi)
            for cred in creds:
                if cred["username"] and cred["username"].lower() in remote_lower:
                    return cred["id"]
            
            # 2. Match with part of the URL (e.g. gearlux)
            for cred in creds:
                if "gearlux" in remote_lower and "gearlux" in cred["id"].lower():
                    return cred["id"]
                if "gearlux" in remote_lower and "gearlux" in cred["description"].lower():
                    return cred["id"]

            # 3. Default to the first gearlux-related credential found if any
            for cred in creds:
                if "gearlux" in cred["id"].lower() or "gearlux" in cred["description"].lower():
                    return cred["id"]

        except Exception as e:
            click.echo(f"  - Error parsing credentials.xml: {e}")
        
        return ""

    def init_pipelines(self) -> None:
        """Seed Jenkins with Multi-branch pipelines for all projects discovered in the workspace."""
        workspace_root = Path(__file__).resolve().parents[3]
        projects = [p for p in workspace_root.iterdir() if p.is_dir() and (p / "Jenkinsfile").exists()]
        
        jobs_dir = self.service_data_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        click.echo(f"Initializing Jenkins pipelines for {len(projects)} projects...")
        
        for project_path in projects:
            project = project_path.name
            project_job_dir = jobs_dir / project
            project_job_dir.mkdir(exist_ok=True)
            
            remote_url = self._get_git_remote(project_path)
            if not remote_url:
                click.echo(f"  - Skipping {project}: Could not determine Git remote.")
                continue

            # Determine credentialsId via fuzzy matching
            cred_id = self._find_credentials(remote_url)

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
  <orphanedItemStrategy class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy" plugin="cloudbees-folder">
    <pruneDeadBranches>true</pruneDeadBranches>
    <daysToKeep>-1</daysToKeep>
    <numToKeep>-1</numToKeep>
  </orphanedItemStrategy>
  <triggers/>
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
    <scriptPath>Jenkinsfile</scriptPath>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>
"""
            config_file = project_job_dir / "config.xml"
            config_file.write_text(config_xml)
            click.echo(f"  - Seeded job: {project} (Remote: {remote_url}, CredID: {cred_id})")
        
        self.setup_dashboard_view()
        click.echo("Jenkins pipelines and dashboard initialized. Please restart Jenkins if it was already running.")

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
        
        views = root.find("views")
        if views is None:
            views = ET.SubElement(root, "views")
            
        # Check if the view already exists
        view_exists = False
        for v in views:
            if v.findtext("name") == name:
                view_exists = True
                break

        if not view_exists:
            click.echo(f"  - Creating '{name}' view (branches: main, dev/main)...")
            
            # Add Build Monitor View
            bm_view = ET.SubElement(views, "com.smartcodeltd.jenkinsci.plugins.buildmonitor.BuildMonitorView")
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
        else:
            click.echo(f"  - Dashboard view '{name}' already exists.")
        
        # Revert primary view to 'all' to ensure standard overview is default
        primary_view = root.find("primaryView")
        if primary_view is not None:
            primary_view.text = "all"

        # Robust write
        tree.write(config_file, encoding="UTF-8", xml_declaration=True)
