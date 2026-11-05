"""Workspace invariant: an internal dependency spec must accept the version its project declares.

The workspace installs its own projects from local checkouts, so a version
specifier naming a sibling is only ever satisfied by the version that sibling's
``pyproject.toml`` declares. When the two drift, nothing warns at edit time —
the failure surfaces as a resolver refusal in the middle of ``aisland setup``,
after an arbitrary prefix of the graph has already been installed, leaving the
venv half-updated with stale editable metadata that still imports fine.

Two real drifts motivated this file (2026-08-25):

* ``recordstream`` released as the pre-release ``0.1.0a1``. Under PEP 440 a
  plain ``>=0.1.0`` excludes every pre-release of ``0.1.0``, so 24 specs across
  five projects became unsatisfiable. The BASE dependencies had been migrated to
  ``>=0.1.0a1``; the ones inside ``optional-dependencies`` were missed — which is
  why the check below walks extras, not just ``project.dependencies``.
* ``app`` capped ``liquifai>=0.1.0,<0.2`` while ``liquifai`` moved to
  ``0.2.0``, so installing app evicted the local editable checkout and
  replaced it with the last PyPI build that fit the cap.

Deliberately NOT flagged: a dependency written with no specifier at all (a bare
``"recordstream"``). It makes no version claim, so there is no claim to
contradict — the resolver takes whatever the workspace provides. Only a spec
that names versions is held to accepting the local one.

Projects whose source is ``github`` / ``pypi`` are deinit'd to an empty
directory (``aisland workspace configure source``), so they declare no local version
and specs naming them are resolved against a real index instead. Those are
skipped rather than guessed at.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Dict, List, NamedTuple

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from aisland.workspace.projects import discover_projects, get_project_metadata, get_workspace_root


class Violation(NamedTuple):
    """One dependency spec that its own workspace cannot satisfy."""

    project: str  # the project declaring the dependency
    spec: str  # the raw spec string, e.g. "recordstream[keras]>=0.1.0"
    dependency: str  # the workspace project it names
    local_version: str  # the version that project's pyproject.toml declares

    def __str__(self) -> str:
        return f"{self.project}: {self.spec!r} rejects the workspace's {self.dependency} {self.local_version}"


def _declared_versions(pyprojects: List[Path]) -> Dict[str, str]:
    """Map project name -> the version its own pyproject.toml declares."""
    versions: Dict[str, str] = {}
    for path in pyprojects:
        with open(path, "rb") as handle:
            project = tomllib.load(handle).get("project", {})
        name, version = project.get("name"), project.get("version")
        if name and version:
            versions[name] = version
    return versions


def find_unsatisfiable_specs(workspace_root: Path) -> List[Violation]:
    """Every internal dependency spec that rejects the depended-on project's own version.

    ``get_project_metadata`` flattens ``project.dependencies`` together with every
    ``optional-dependencies`` group, so extras are covered by construction.
    """
    pyprojects = discover_projects(workspace_root)
    versions = _declared_versions(pyprojects)

    violations: List[Violation] = []
    for path in pyprojects:
        meta = get_project_metadata(path)
        for raw in meta["dependencies"]:
            try:
                requirement = Requirement(raw)
            except Exception:  # a malformed spec is a different test's problem
                continue
            if not len(requirement.specifier):
                continue  # no version claim -> nothing to contradict
            local = versions.get(requirement.name)
            if local is None or requirement.name == meta["name"]:
                continue  # external dependency, detached source, or self-reference
            if not requirement.specifier.contains(Version(local)):
                violations.append(Violation(meta["name"], raw, requirement.name, local))
    return sorted(violations)


# --------------------------------------------------------------------------
# Synthetic fixtures — one tmp workspace per case, so a case names exactly the
# shape it is about.
# --------------------------------------------------------------------------

_TEMPLATE = """\
[project]
name = "{name}"
version = "{version}"
dependencies = [{deps}]

[project.optional-dependencies]
torch = [{extras}]
"""


def _workspace(tmp_path: Path, *projects: tuple) -> Path:
    """Build <tmp>/<name>/pyproject.toml for each (name, version, deps, extras)."""
    for name, version, deps, extras in projects:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "pyproject.toml").write_text(
            _TEMPLATE.format(
                name=name,
                version=version,
                deps=", ".join(f'"{d}"' for d in deps),
                extras=", ".join(f'"{e}"' for e in extras),
            )
        )
    return tmp_path


def test_a_prerelease_floor_accepts_the_prerelease(tmp_path: Path) -> None:
    """PRO case: `>=0.1.0a1` is how a dependent names a pre-release sibling."""
    root = _workspace(
        tmp_path,
        ("recordstream", "0.1.0a1", [], []),
        ("navigaitor", "0.1.0", ["recordstream>=0.1.0a1"], []),
    )
    assert find_unsatisfiable_specs(root) == []


def test_a_plain_floor_rejects_a_prerelease_sibling(tmp_path: Path) -> None:
    """CON case: the exact drift that broke `aisland setup` — PEP 440 puts 0.1.0a1 below 0.1.0."""
    root = _workspace(
        tmp_path,
        ("recordstream", "0.1.0a1", [], []),
        ("navigaitor", "0.1.0", ["recordstream>=0.1.0"], []),
    )
    assert find_unsatisfiable_specs(root) == [Violation("navigaitor", "recordstream>=0.1.0", "recordstream", "0.1.0a1")]


def test_specs_inside_an_extra_are_checked_too(tmp_path: Path) -> None:
    """The base dep was migrated and the extra was not — the miss this file exists to catch."""
    root = _workspace(
        tmp_path,
        ("recordstream", "0.1.0a1", [], []),
        ("sonair", "0.1.0", ["recordstream>=0.1.0a1"], ["recordstream[torch]>=0.1.0"]),
    )
    assert [v.spec for v in find_unsatisfiable_specs(root)] == ["recordstream[torch]>=0.1.0"]


def test_an_upper_cap_below_the_sibling_is_a_violation(tmp_path: Path) -> None:
    """CON case: app's `liquifai<0.2` against a liquifai that moved to 0.2.0."""
    root = _workspace(
        tmp_path,
        ("liquifai", "0.2.0", [], []),
        ("app", "0.1.0", ["liquifai>=0.1.0,<0.2"], []),
    )
    assert find_unsatisfiable_specs(root) == [Violation("app", "liquifai>=0.1.0,<0.2", "liquifai", "0.2.0")]


def test_a_cap_that_still_covers_the_sibling_is_fine(tmp_path: Path) -> None:
    """PRO case: capping a provisional API is allowed — the cap just has to include reality."""
    root = _workspace(
        tmp_path,
        ("liquifai", "0.2.0", [], []),
        ("app", "0.1.0", ["liquifai>=0.2.0,<0.3"], []),
    )
    assert find_unsatisfiable_specs(root) == []


def test_a_dependency_with_no_specifier_makes_no_claim(tmp_path: Path) -> None:
    """A bare name is not a version claim, so a pre-release sibling does not contradict it."""
    root = _workspace(
        tmp_path,
        ("recordstream", "0.1.0a1", [], []),
        ("annotaide", "0.1.0", ["recordstream"], []),
    )
    assert find_unsatisfiable_specs(root) == []


def test_external_and_detached_dependencies_are_left_alone(tmp_path: Path) -> None:
    """`loggair` is pypi-sourced (empty dir, no local version) and `torch` is not ours."""
    root = _workspace(
        tmp_path,
        ("navigaitor", "0.1.0", ["loggair>=99.0", "torch>=2.0"], []),
    )
    (tmp_path / "loggair").mkdir()  # deinit'd submodule: directory exists, pyproject does not
    assert find_unsatisfiable_specs(root) == []


@pytest.mark.real_workspace
def test_the_real_workspace_has_no_unsatisfiable_internal_specs() -> None:
    """The integration pin: `aisland setup` can resolve the whole graph from local checkouts."""
    violations = find_unsatisfiable_specs(get_workspace_root())
    assert violations == [], "\n".join(["unsatisfiable internal dependency specs:"] + [str(v) for v in violations])
