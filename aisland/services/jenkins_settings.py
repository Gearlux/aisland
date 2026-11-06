"""A Jenkins home's own settings that ``aisland jenkins init`` writes: the dashboard view of the workspace's jobs, the
workspace's environment in the global properties, and (``--local``, after a yes) anonymous read access.

Each edits the home's ``config.xml`` while Jenkins is stopped (:func:`aisland.services.jenkins_jobs.init_pipelines`
stops it), so Jenkins' in-memory model never writes over the change.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from aisland import console, envfile, failures
from aisland.workspace.projects import get_workspace_name, get_workspace_root

logger = logging.getLogger(__name__)

#: The jobs the dashboard shows: the branches the ecosystem is built from, and every job of the local pipeline.
_DASHBOARD_JOBS = "^(main|dev/main|.*\\.local)$"
_BUILD_MONITOR_VIEW = "com.smartcodeltd.jenkinsci.plugins.buildmonitor.BuildMonitorView"


def setup_dashboard_view(home: Path, name: Optional[str] = None) -> None:
    """Create a Build Monitor View for the Ecosystem branching workflow."""
    if name is None:
        name = f"{get_workspace_name().capitalize()} Ecosystem Dashboard"

    config_file = home / "config.xml"
    if not config_file.exists():
        console.echo("  - Jenkins config.xml not found. Skipping view setup until first run.")
        return
    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing config.xml: {e}")
        return

    dashboard = _dashboard_view(_views(root), name)
    _filter_dashboard_jobs(dashboard)
    # Revert primary view to 'all' to ensure standard overview is default
    primary_view = root.find("primaryView")
    if primary_view is not None:
        primary_view.text = "all"
    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def _view_basics(view: ET.Element, name: str) -> None:
    """The elements every Jenkins view starts with: its owner, name, filters and properties."""
    ET.SubElement(view, "owner").set("class", "hudson")
    ET.SubElement(view, "owner").set("reference", "../../..")
    ET.SubElement(view, "name").text = name
    ET.SubElement(view, "filterExecutors").text = "false"
    ET.SubElement(view, "filterQueue").text = "false"
    ET.SubElement(view, "properties").set("class", "hudson.model.View$PropertyList")


def _views(root: ET.Element) -> ET.Element:
    """Jenkins' ``views`` container — made with the standard ``all`` view when the config has none."""
    views = root.find("views")
    if views is None:
        views = ET.SubElement(root, "views")
        _view_basics(ET.SubElement(views, "hudson.model.AllView"), "all")
    return views


def _dashboard_view(views: ET.Element, name: str) -> ET.Element:
    """The view called ``name``; else any ``<Anything> Ecosystem Dashboard``, renamed in place; else a new one.

    The rename handles a workspace rename (the repository renamed on GitHub, so :func:`get_workspace_name` answers a new
    string) without stranding the old view or creating a duplicate.
    """
    for view in views:
        if view.findtext("name") == name:
            return view
    for view in views:
        existing = view.findtext("name") or ""
        if existing.endswith(" Ecosystem Dashboard"):
            console.echo(f"  - Renaming stale dashboard view '{existing}' -> '{name}' (workspace was renamed).")
            for field in ("name", "title"):
                element = view.find(field)
                if element is not None:
                    element.text = name
            return view
    console.echo(f"  - Creating '{name}' view (branches: main, dev/main, *.local) with Build Monitor View plugin...")
    view = ET.SubElement(views, _BUILD_MONITOR_VIEW)
    view.set("plugin", "build-monitor-plugin")
    _view_basics(view, name)
    ET.SubElement(view, "jobNames").set("class", "hudson.util.CaseInsensitiveComparator")
    ET.SubElement(view, "jobFilters")
    ET.SubElement(view, "columns").set("class", "empty-list")
    ET.SubElement(view, "recurse").text = "true"
    ET.SubElement(view, "title").text = name
    config = ET.SubElement(view, "config")
    ET.SubElement(config, "displayCommitters").text = "true"
    ET.SubElement(config, "displayLowPriorityJobs").text = "false"
    ET.SubElement(config, "displayLastBuildConsoleOutput").text = "true"
    ET.SubElement(config, "order").set("class", "com.smartcodeltd.jenkinsci.plugins.buildmonitor.order.ByName")
    return view


def _filter_dashboard_jobs(view: ET.Element) -> None:
    """Make the view's regular-expression job filter show :data:`_DASHBOARD_JOBS`, adding the filter if it lacks one."""
    job_filters = view.find("jobFilters")
    if job_filters is None:
        job_filters = ET.SubElement(view, "jobFilters")
    regex_filter = job_filters.find("hudson.views.RegExJobFilter")
    if regex_filter is None:
        regex_filter = ET.SubElement(job_filters, "hudson.views.RegExJobFilter")
        regex_filter.set("plugin", "view-job-filters")
        ET.SubElement(regex_filter, "includeExcludeTypeString").text = "includeMatched"
        ET.SubElement(regex_filter, "valueTypeString").text = "NAME"
        ET.SubElement(regex_filter, "regex").text = _DASHBOARD_JOBS
        ET.SubElement(regex_filter, "matchCanary").text = "false"
        return
    regex = regex_filter.find("regex")
    if regex is None:
        regex = ET.SubElement(regex_filter, "regex")
    regex.text = _DASHBOARD_JOBS


def setup_global_properties(home: Path) -> None:
    """Add WORKSPACE_ROOT and other workspace-wide settings to Jenkins global properties."""
    config_file = home / "config.xml"
    if not config_file.exists():
        return

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
    dotenv_vars = envfile.read(workspace_root)  # the one reader: what the shell reads
    if dotenv_vars:
        logger.debug("read %d key(s) from %s", len(dotenv_vars), dotenv_path)
    else:
        console.echo("  - No workspace .env found; skipping injection.")

    # Computed keys come first so a typo'd .env can't override the workspace
    # path or the forced-Node24 toggle. ** dotenv_vars adds the remainder.
    env_data = {
        "WORKSPACE_ROOT": str(workspace_root),
        "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": "true",
        **{k: v for k, v in dotenv_vars.items() if k not in ("WORKSPACE_ROOT", "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24")},
    }

    # int size
    ET.SubElement(tree_map, "int").text = str(len(env_data))

    computed_keys = {"WORKSPACE_ROOT", "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"}
    for key, value in env_data.items():
        ET.SubElement(tree_map, "string").text = key
        ET.SubElement(tree_map, "string").text = value
        if key in computed_keys:
            console.echo(f"  - Added global Jenkins property: {key}={value}")
        else:
            console.echo(f"  - Added global Jenkins property: {key}=<from .env>")

    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def allow_anonymous_read(home: Path) -> None:
    """Let a visitor who has not logged in read Jenkins (`denyAnonymousReadAccess` false). Nothing else is loosened:
    the login stays, and so does CSRF protection — Jenkins listens on this machine only, but a page open in your
    browser can still post to it, so neither is ever switched off."""
    config_file = home / "config.xml"
    if not config_file.exists():
        return

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing config.xml for anonymous read access: {e}")
        return

    auth_strategy = root.find("authorizationStrategy")
    deny_anon = auth_strategy.find("denyAnonymousReadAccess") if auth_strategy is not None else None
    if deny_anon is None:
        console.echo("  - Anonymous read access: config.xml has no setting for it; left as it is.")
        return
    deny_anon.text = "false"
    console.echo("  - Enabled anonymous read access.")
    tree.write(config_file, encoding="UTF-8", xml_declaration=True)
