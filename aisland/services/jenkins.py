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
        # workspace directory names (e.g. "logflow", "dataflux") — NOT the pyproject
        # distribution names (which may differ: "log-flow", "data-flux"). Build a
        # lookup that maps both forms to the canonical workspace-dir form so we can
        # match either a pyproject name or a dir name against initialized_projects.
        initialized_set = set(initialized_projects)

        def _to_dir_name(pyproject_name: str) -> str:
            """PyPI/pyproject name -> workspace dir name (handles dashed PyPI names)."""
            if pyproject_name in initialized_set:
                return pyproject_name
            dashless = pyproject_name.replace("-", "")
            if dashless in initialized_set:
                return dashless
            return pyproject_name  # unknown; pass through

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
            # Scaffold all projects that have a pyproject.toml
            for child in sorted(workspace_root.iterdir()):
                if child.is_dir() and (child / "pyproject.toml").exists() and child.name != "aisland":
                    self.scaffold_pipelines(project_name=child.name, force=force)
            return

        project_path = workspace_root / project_name

        if not project_path.exists() or not project_path.is_dir():
            click.echo(f"Error: Project directory '{project_name}' not found at {project_path}.", err=True)
            return

        pyproject_path = project_path / "pyproject.toml"
        if not pyproject_path.exists():
            click.echo(f"Error: pyproject.toml not found in {project_name}. Cannot scaffold.", err=True)
            return

        metadata = get_project_metadata(pyproject_path)
        pkg_name = metadata["name"].replace("-manager", "")
        # Detect actual package directory.
        #   Step 1: try the literal distribution name (e.g. "confluid").
        #   Step 2: try the PEP 503 module form — distribution names allow dashes
        #           but Python import names don't, so "data-flux" -> "dataflux",
        #           "log-flow" -> "logflow", etc. Try that before the heuristic
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

        # Internal Gearlux Dependencies (order matters for install).
        # PyPI names — these are what appear in pyproject `dependencies` lists and
        # (by the GitHub-repo-name = PyPI-name convention) also the GitHub repo names
        # under the Gearlux org. Four of them deliberately differ from the Python
        # import name to avoid collisions with existing PyPI packages:
        #   log-flow   (import logflow)   — squats at PyPI logflow 0.0.1
        #   data-flux  (import dataflux)  — squats at PyPI dataflux 0.1.2
        #   liquifai   (import liquifai)  — squats at PyPI liquify 0.3
        #   torpaido   (import torpaido)  — squats at PyPI torpedo 0.0.2
        gearlux_projects = [
            "log-flow",
            "confluid",
            "data-flux",
            "liquifai",
            "navigaitor",
            "signals",
            "signals-rfuav",
            "annotaide",
            "torpaido",
            "aisland",
            "marainer",
        ]

        # Private Gearlux repos require GitHub App authentication for cross-repo CI installs.
        # The default GITHUB_TOKEN only grants access to the current repo, so a GitHub App
        # token is minted and git HTTPS URLs are rewritten to include it.
        private_gearlux_repos = {
            "signals",
            "signals-rfuav",
            "marainer",
            "navigaitor",
            "annotaide",
            "aisland",
        }

        # Collect the FULL TRANSITIVE closure of internal Gearlux deps, not just
        # the direct ones. When CI installs a direct dep like `confluid` with
        # --no-deps, `.[dev]` below still needs to resolve confluid's own deps
        # (e.g. log-flow) — and Gearlux distribution names aren't on PyPI, so uv
        # fails unless WE'VE ALSO pre-installed those transitive deps first.
        #
        # Build the closure by walking each Gearlux dep's own pyproject.toml.
        # Topologically sort so every package's deps are installed before it.
        import re
        import tomllib

        def _strip_specifier(dep_str: str) -> str:
            return re.split(r"[>=< ]", dep_str)[0].strip()

        def _read_internal_deps(pyproject: Path) -> list[str]:
            """Return the direct Gearlux deps of one pyproject.toml, including all extras.

            Walks both `project.dependencies` (runtime) and every extras group in
            `project.optional-dependencies` (dev, test, notebook, ...). Reason:
            extras like dev can list sibling workspace packages (e.g. navigaitor's
            dev extra depends on signals-rfuav for tests). CI runs `.[dev]`, so
            those extras ARE installed and must be pre-satisfied by the
            --no-deps Gearlux install loop — otherwise `.[dev]` can't resolve
            them from PyPI.
            """
            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
            except Exception:
                return []
            project = data.get("project", {})
            all_specs: list[str] = list(project.get("dependencies") or [])
            for extra_name, extras in (project.get("optional-dependencies") or {}).items():
                all_specs.extend(extras or [])
            return [_strip_specifier(d) for d in all_specs if _strip_specifier(d) in gearlux_projects]

        # Map each Gearlux project name to its workspace dir (for locating its
        # pyproject.toml). PyPI-dashed names resolve to dashless dirs.
        def _gearlux_workspace_path(name: str) -> Path:
            if (workspace_root / name).is_dir():
                return workspace_root / name
            stripped = name.replace("-", "")
            return workspace_root / stripped

        # BFS over Gearlux deps starting from this project, accumulating all
        # reachable Gearlux packages. Start from BOTH runtime and extras deps
        # (see `_read_internal_deps` rationale): CI's `.[dev]` step installs
        # every extras group, so extras must be pre-satisfied too.
        direct = _read_internal_deps(pyproject_path)

        seen: set[str] = set()
        stack = list(direct)
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            sub_pyproject = _gearlux_workspace_path(dep) / "pyproject.toml"
            if sub_pyproject.exists():
                for sub_dep in _read_internal_deps(sub_pyproject):
                    if sub_dep not in seen:
                        stack.append(sub_dep)

        # Topological sort so log-flow is installed before confluid, etc.
        # Build adjacency: dep -> set(prerequisites)
        prereqs: dict[str, set[str]] = {name: set() for name in seen}
        for name in seen:
            sub_pyproject = _gearlux_workspace_path(name) / "pyproject.toml"
            if sub_pyproject.exists():
                for sub_dep in _read_internal_deps(sub_pyproject):
                    if sub_dep in seen:
                        prereqs[name].add(sub_dep)

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
        # Rationale: all Gearlux distribution names (log-flow, data-flux, liquifai,
        # torpaido, confluid, ...) are intentionally NOT published on PyPI. If
        # `.[dev]` ran first, uv would try to resolve `log-flow>=0.1.0` from PyPI,
        # fail ("not found in the package registry"), and abort the whole install.
        # By installing each Gearlux dep from its git URL first (with --no-deps to
        # skip its own Gearlux transitive deps — each is handled in its own loop
        # iteration, topologically ordered), we pre-satisfy the constraints so
        # `-e .[dev]` resolves cleanly against the already-installed packages.
        # --system installs into the runner's Python rather than requiring a venv.
        pip_install_cmds = []
        for dep in internal_deps:
            pip_install_cmds.append(f"uv pip install --system --no-deps git+https://github.com/Gearlux/{dep}.git@main")

        pip_install_str = "\n        ".join(pip_install_cmds)
        if pip_install_str:
            pip_install_str = (
                "\n        # Internal Gearlux dependencies — installed FIRST with --no-deps so"
                "\n        # `-e .[dev]` below finds them pre-satisfied instead of hitting PyPI."
                "\n        " + pip_install_str
            )

        # Detect private Gearlux dependencies — if any, CI needs a GitHub App token
        # to clone them (default GITHUB_TOKEN only grants access to the current repo).
        needs_app_token = any(dep in private_gearlux_repos for dep in internal_deps)

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

        # Jenkinsfile (standard) — sh-wrapped, installs from GitHub via uv.
        # Install order identical to the GH Actions path: `.[dev]` first, Gearlux
        # deps LAST with --reinstall so PyPI name collisions get overwritten.
        jenkins_pip_cmds = []
        for dep in internal_deps:
            jenkins_pip_cmds.append(
                f'sh "${{VENV_BIN}}/uv pip install --no-deps ' f'git+https://github.com/Gearlux/{dep}.git@main"'
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
        # PyPI distribution names CAN have dashes ("log-flow", "data-flux") but
        # filesystem / workspace directory names are conventionally dashless
        # ("logflow/", "dataflux/"). For each dep, resolve the workspace path:
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

        local_pip_cmds = []
        for dep in internal_deps:
            ws_dir = _dep_to_workspace_dir(dep)
            # --no-deps is CRITICAL. Without it, uv would re-resolve this target's
            # transitive Gearlux deps (e.g. confluid -> log-flow), and since
            # log-flow isn't on PyPI the resolution would fail. The outer dep loop
            # is already topologically ordered (log-flow before confluid before
            # liquifai, etc.) so each dep's own deps are guaranteed installed by
            # the time we get to it.
            local_pip_cmds.append(f'sh "${{VENV_BIN}}/uv pip install --no-deps -e ${{env.WORKSPACE_ROOT}}/{ws_dir}"')
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
                    headers={**crumb_header, "Content-Type": "application/x-www-form-urlencoded"},
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
                    click.echo(click.style(f"  [ABORT] Not writing {desc} — fix the template.", fg="red"))
                    raise click.ClickException(f"Template produced invalid {desc}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            click.echo(f"  [OK] Created {desc}")

        # 1. Jenkinsfile.local
        local_content = f"""// =========================================================================
// AUTO-GENERATED FILE — DO NOT EDIT BY HAND
// Generated by: aisland jenkins scaffold --project {project_name}
// Source template: aisland/services/jenkins.py (JenkinsService.scaffold_pipelines)
// To regenerate:  aisland jenkins scaffold --project {project_name} --force
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
                sh "${{VENV_BIN}}/uv pip install -e .[dev]"
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
                                def exitCode = sh(script: "set -o pipefail; ${{VENV_BIN}}/black --check --diff ${{targets}} 2>&1 | tee black-diff.txt", returnStatus: true)

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
                                def exitCode = sh(script: "set -o pipefail; ${{VENV_BIN}}/isort --check-only --diff ${{targets}} 2>&1 | tee isort-diff.txt", returnStatus: true)

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
                            def exitCode = sh(script: "set -o pipefail; cd ${{env.WORKSPACE_ROOT}} && ${{VENV_BIN}}/mypy {project_name} 2>&1 | tee ${{env.WORKSPACE_ROOT}}/{project_name}/mypy.txt", returnStatus: true)
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
                        sh "${{VENV_BIN}}/pytest tests --junitxml=test-report.xml --cov={pkg_name} --cov-report=xml:coverage.xml --cov-report=term"
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
                    shopt -s nullglob
                    nbs=(notebooks/*.ipynb)
                    if [ ${{#nbs[@]}} -eq 0 ]; then
                        echo "No notebooks to verify."
                        exit 0
                    fi
                    for nb in "${{nbs[@]}}"; do
                        echo "Executing $nb..."
                        ${{VENV_BIN}}/jupyter nbconvert --to notebook --execute \\
                            --output /tmp/$(basename "$nb" .ipynb)-ci.ipynb \\
                            "$nb"
                    done
                '''
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
// Generated by: aisland jenkins scaffold --project {project_name}
// Source template: aisland/services/jenkins.py (JenkinsService.scaffold_pipelines)
// To regenerate:  aisland jenkins scaffold --project {project_name} --force
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
                sh "${{VENV_BIN}}/uv pip install -e .[dev]"
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
                                def exitCode = sh(script: "set -o pipefail; ${{VENV_BIN}}/black --check --diff ${{targets}} 2>&1 | tee black-diff.txt", returnStatus: true)

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
                                def exitCode = sh(script: "set -o pipefail; ${{VENV_BIN}}/isort --check-only --diff ${{targets}} 2>&1 | tee isort-diff.txt", returnStatus: true)

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
                            def exitCode = sh(script: "set -o pipefail; ${{VENV_BIN}}/mypy . 2>&1 | tee mypy.txt", returnStatus: true)
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
                        sh "${{VENV_BIN}}/pytest tests --junitxml=test-report.xml --cov={pkg_name} --cov-report=xml:coverage.xml --cov-report=term"
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
                    shopt -s nullglob
                    nbs=(notebooks/*.ipynb)
                    if [ ${{#nbs[@]}} -eq 0 ]; then
                        echo "No notebooks to verify."
                        exit 0
                    fi
                    for nb in "${{nbs[@]}}"; do
                        echo "Executing $nb..."
                        ${{VENV_BIN}}/jupyter nbconvert --to notebook --execute \\
                            --output /tmp/$(basename "$nb" .ipynb)-ci.ipynb \\
                            "$nb"
                    done
                '''
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
# Generated by: aisland jenkins scaffold --project {project_name}
# Source template: aisland/services/jenkins.py (JenkinsService.scaffold_pipelines)
# To regenerate:  aisland jenkins scaffold --project {project_name} --force
# =========================================================================
name: {project_name.capitalize()} CI

on:
  push:
    branches: [ main, dev/main ]
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
        uv pip install --system -e ".[dev]"
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
        uv pip install --system -e ".[dev]"
    - name: Run Tests
      run: |
        if [ -d tests ] && find tests -name '*.py' | grep -q .; then
          pytest tests --junitxml=test-report.xml --cov={pkg_name} --cov-report=xml --cov-report=term
        else
          echo "No tests found. Skipping."
        fi
    - name: Upload Test Results
      uses: actions/upload-artifact@v4
      with:
        name: test-results
        path: test-report.xml
      if: always()

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
        uv pip install --system -e ".[dev]"
    - name: Run Examples
      run: |
        for f in examples/*.py; do
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
        uv pip install --system -e ".[dev]"
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
            --output /tmp/$(basename "$nb" .ipynb)-ci.ipynb \\
            "$nb"
        done
"""
        _write_safe(project_path / ".github" / "workflows" / "ci.yml", gh_content, "GitHub CI Workflow")

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
        env_data = {
            "WORKSPACE_ROOT": str(workspace_root),
            "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": "true",
        }

        # int size
        ET.SubElement(tree_map, "int").text = str(len(env_data))

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
