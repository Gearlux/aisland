"""Tests for workspace ``.env`` -> Jenkins globalNodeProperties injection.

``setup_global_properties`` is the single hook that pushes workspace-wide
environment variables (e.g. ``WORKSPACE_ROOT``) into Jenkins's ``config.xml``
so every build sees them. Pre-fix, only the two computed keys were injected
and the workspace ``.env`` (``DATA_ROOT`` etc.) was invisible to pipelines.

These tests pin the new behavior: ``.env`` is parsed and merged in alongside
the computed keys, with computed keys winning on conflict and comment/blank
lines being ignored.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aisland import locations
from aisland.services import jenkins_jobs
from aisland.services.jenkins import JenkinsService

_MINIMAL_CONFIG_XML = """<?xml version='1.1' encoding='UTF-8'?>
<hudson>
  <version>2.0</version>
</hudson>
"""


def _read_envvars(config_file: Path) -> dict[str, str]:
    """Parse the envVars tree-map back into a flat KV dict."""
    tree = ET.parse(config_file)
    tree_map = tree.getroot().find(".//tree-map")
    assert tree_map is not None
    # Layout: <int>N</int> then 2N <string> children (key,value pairs).
    strings = [el.text or "" for el in tree_map.findall("string")]
    assert len(strings) % 2 == 0
    return dict(zip(strings[::2], strings[1::2]))


# The `.env` reader's cases: tests/test_workspace_env.py (Jenkins reads it with supervisor.read_workspace_env).


# --------------------------------------------------------------------------- #
# setup_global_properties integration
# --------------------------------------------------------------------------- #


def _make_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[JenkinsService, Path]:
    """Build a JenkinsService whose service_data_dir + workspace root live under tmp_path."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # get_workspace_root walks up looking for TASKS.md; give it one to find.
    (workspace / "TASKS.md").write_text("# tasks\n", encoding="utf-8")
    monkeypatch.setattr(locations, "workspace_root", lambda: workspace)

    svc = JenkinsService()
    jenkins_dir = tmp_path / "jenkins"
    jenkins_dir.mkdir()
    svc.service_data_dir = jenkins_dir
    (jenkins_dir / "config.xml").write_text(_MINIMAL_CONFIG_XML, encoding="utf-8")
    return svc, workspace


def test_setup_global_properties_injects_dotenv_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workspace = _make_service(tmp_path, monkeypatch)
    (workspace / ".env").write_text(
        "# the workspace data store\n" "DATA_ROOT=/tmp/data\n" "\n" 'HF_HOME="/tmp/hf"\n',
        encoding="utf-8",
    )

    jenkins_jobs.setup_global_properties(svc.service_data_dir)

    envvars = _read_envvars(svc.service_data_dir / "config.xml")
    assert envvars["WORKSPACE_ROOT"] == str(workspace)
    assert envvars["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] == "true"
    assert envvars["DATA_ROOT"] == "/tmp/data"
    assert envvars["HF_HOME"] == "/tmp/hf"


def test_setup_global_properties_no_dotenv_only_computed_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc, workspace = _make_service(tmp_path, monkeypatch)
    # No .env in workspace.

    jenkins_jobs.setup_global_properties(svc.service_data_dir)

    envvars = _read_envvars(svc.service_data_dir / "config.xml")
    assert envvars == {
        "WORKSPACE_ROOT": str(workspace),
        "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": "true",
    }


def test_setup_global_properties_computed_keys_win_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd .env that re-declares WORKSPACE_ROOT must not override the computed value."""
    svc, workspace = _make_service(tmp_path, monkeypatch)
    (workspace / ".env").write_text(
        "WORKSPACE_ROOT=/should/not/win\n" "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=false\n" "DATA_ROOT=/tmp/data\n",
        encoding="utf-8",
    )

    jenkins_jobs.setup_global_properties(svc.service_data_dir)

    envvars = _read_envvars(svc.service_data_dir / "config.xml")
    assert envvars["WORKSPACE_ROOT"] == str(workspace)
    assert envvars["FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"] == "true"
    assert envvars["DATA_ROOT"] == "/tmp/data"


def test_setup_global_properties_envvars_int_size_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The leading <int> count in the tree-map must match the number of KV pairs."""
    svc, workspace = _make_service(tmp_path, monkeypatch)
    (workspace / ".env").write_text("A=1\nB=2\nC=3\n", encoding="utf-8")

    jenkins_jobs.setup_global_properties(svc.service_data_dir)

    tree = ET.parse(svc.service_data_dir / "config.xml")
    tree_map = tree.getroot().find(".//tree-map")
    assert tree_map is not None
    size_el = tree_map.find("int")
    assert size_el is not None and size_el.text is not None
    declared = int(size_el.text)
    # WORKSPACE_ROOT + FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 + A + B + C = 5
    assert declared == 5
    assert len(tree_map.findall("string")) == 2 * declared
