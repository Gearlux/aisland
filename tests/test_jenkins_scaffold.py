"""Regression tests for `aisland jenkins scaffold` over the full workspace.

Specifically: scaffolding all projects MUST include aisland itself (regression
for the historical `and child.name != "aisland"` filter that silently skipped
aisland's own CI surface).
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, List, Optional

import click
import pytest

from aisland import locations
from aisland.ci import ci_scaffold
from aisland.ci.ci_scaffold import STAGE_LIMITS, TIMED_OUT_MESSAGE
from tests.records import NO_JENKINS


def _make_pyproject(dir_: Path, name: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    # The scaffolder probes for an inner package dir with __init__.py.
    pkg = dir_ / name.replace("-", "")
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").touch()


def test_scaffold_all_projects_includes_aisland(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When scaffolding all projects, aisland MUST be in the list of recursed
    targets — previously it was filtered out by `child.name != "aisland"`.
    """
    # Build a fake workspace with three sibling projects, one of which is aisland.
    for name in ("aisland", "matrainer", "traidwind"):
        _make_pyproject(tmp_path / name, name)

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)

    # Capture the project names the scaffolder recurses into instead of
    # actually generating files (the file-writing path is exercised
    # end-to-end by ad-hoc CLI runs).
    visited: List[str] = []
    original = ci_scaffold.scaffold

    def _spy(project_name: Any, force: bool, jenkins_url: str) -> None:
        if project_name is None:
            # Fall through to the real "iterate all projects" branch.
            return original(None, force, jenkins_url)
        visited.append(project_name)

    monkeypatch.setattr(ci_scaffold, "scaffold", _spy)

    ci_scaffold.scaffold(None, force=False, jenkins_url=NO_JENKINS)

    assert "aisland" in visited, "aisland must not be filtered out of the all-projects loop"
    assert {"aisland", "matrainer", "traidwind"} <= set(visited)


def test_scaffold_emits_directory_example_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The Verify-Examples step MUST also execute directory examples
    (``examples/*/run.py``) in ALL THREE generated files — the opt-in glob that
    runs e.g. confluid's ``examples/ml_experiments/run.py`` while skipping
    installable apps that ship no run.py (network-dependent examples).
    """
    _make_pyproject(tmp_path / "proj", "proj")

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)

    # The Jenkins linter is unreachable in tests -> warn-and-skip, files still written.
    ci_scaffold.scaffold("proj", force=True, jenkins_url=NO_JENKINS)

    generated = {
        "Jenkinsfile": tmp_path / "proj" / "Jenkinsfile",
        "Jenkinsfile.local": tmp_path / "proj" / "Jenkinsfile.local",
        "ci.yml": tmp_path / "proj" / ".github" / "workflows" / "ci.yml",
    }
    for desc, path in generated.items():
        assert path.exists(), f"{desc} was not generated"
        content = path.read_text()
        assert "examples/*.py" in content, f"{desc} lost the flat-example loop"
        assert "examples/*/run.py" in content, f"{desc} must run directory examples via examples/*/run.py"


def test_the_regenerate_header_is_a_command_the_cli_accepts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The header is the ONLY regeneration instruction an operator or agent has, so it must run.

    It used to say `aisland jenkins scaffold --project <name> --force`, which fails with
    `Error: No such option: --project` — the project is a positional argument. This parses the
    emitted line with the REAL click command rather than string-matching, so the pin tracks the
    CLI's signature instead of restating it.
    """
    _make_pyproject(tmp_path / "proj", "proj")

    from aisland.cli.services import jenkins_scaffold

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    ci_scaffold.scaffold("proj", force=True, jenkins_url=NO_JENKINS)

    generated = [
        tmp_path / "proj" / "Jenkinsfile",
        tmp_path / "proj" / "Jenkinsfile.local",
        tmp_path / "proj" / ".github" / "workflows" / "ci.yml",
    ]
    for path in generated:
        line = next(ln for ln in path.read_text().splitlines() if "To regenerate:" in ln)
        argv = line.split("To regenerate:")[1].split()

        assert argv[:3] == ["aisland", "jenkins", "scaffold"], f"{path.name}: unexpected command: {argv}"
        # The command's own parser is the authority on what the rest means.
        with jenkins_scaffold.make_context("scaffold", argv[3:], resilient_parsing=False) as ctx:
            assert ctx.params == {"project": "proj", "force": True}, f"{path.name}: {ctx.params}"


def _make_pyproject_with_slow_marker(dir_: Path, name: str) -> None:
    _make_pyproject(dir_, name)
    (dir_ / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\n\n'
        "[tool.pytest.ini_options]\n"
        "markers = [\"slow: heavy end-to-end tests; deselect with -m 'not slow'.\"]\n",
        encoding="utf-8",
    )


def _make_pyproject_with_playwright(dir_: Path, name: str, slow_marker: bool = False) -> None:
    _make_pyproject(dir_, name)
    marker = (
        "\n[tool.pytest.ini_options]\nmarkers = [\"slow: heavy end-to-end tests; deselect with -m 'not slow'.\"]\n"
        if slow_marker
        else ""
    )
    (dir_ / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\n\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest>=7.0.0,<9.0.0", "playwright>=1.40.0,<2.0.0"]\n' + marker,
        encoding="utf-8",
    )


def _with_node_tests(dir_: Path) -> None:
    """A `frontend/` folder whose `package.json` has a `test` script: the project's declaration of a Node test run."""
    (dir_ / "frontend").mkdir(parents=True, exist_ok=True)
    (dir_ / "frontend" / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n', encoding="utf-8")


def _make_pyproject_with_slow_marker_and_node_tests(dir_: Path, name: str) -> None:
    _make_pyproject_with_slow_marker(dir_, name)
    _with_node_tests(dir_)


def _scaffolded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make: Any) -> dict[str, str]:

    make(tmp_path / "proj", "proj")
    (tmp_path / ".gitmodules").write_text("", encoding="utf-8")
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    ci_scaffold.scaffold("proj", force=True, jenkins_url=NO_JENKINS)
    return {
        "Jenkinsfile": (tmp_path / "proj" / "Jenkinsfile").read_text(encoding="utf-8"),
        "Jenkinsfile.local": (tmp_path / "proj" / "Jenkinsfile.local").read_text(encoding="utf-8"),
        "ci.yml": (tmp_path / "proj" / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
    }


class TestTheSlowMarkerSplitsTheTestInvocations:
    """A registered `slow` pytest marker IS the declaration: the routine run deselects the slow
    tests and a separate stage/job runs them, so a long end-to-end smoke test leaves every
    push's critical path while its dep-bump-drift coverage is preserved. A project WITHOUT the
    marker gets byte-identical output to before — opting in never churns the fleet's files."""

    def test_a_registered_slow_marker_splits_all_three_artifacts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)
        for name, content in files.items():
            assert "-m 'not slow'" in content, f"{name}: the routine run must deselect the slow tests"
            assert "-m slow" in content, f"{name}: the slow leg is missing"
        assert "stage('Slow Tests')" in files["Jenkinsfile"]
        assert "stage('Slow Tests')" in files["Jenkinsfile.local"]
        assert "slow-tests:" in files["ci.yml"]
        # The GitHub slow leg runs PARALLEL to unit-tests (same dependency), not after it.
        assert files["ci.yml"].count("needs: [quality-gates]") >= 2

    def test_exit_code_5_is_tolerated_in_the_slow_leg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A registered marker with nothing currently marked is a valid state, not a failure."""
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)
        assert "rc != 0 && rc != 5" in files["Jenkinsfile"]
        assert "rc != 0 && rc != 5" in files["Jenkinsfile.local"]
        assert "-ne 5" in files["ci.yml"]

    def test_a_project_without_the_marker_is_untouched(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject)
        for name, content in files.items():
            assert "not slow" not in content, f"{name}: an unmarked project must not deselect anything"
            assert "Slow Tests" not in content and "slow-tests:" not in content, name
            assert "SLOW_TESTS" not in content, name

    def test_the_local_pipeline_runs_its_slow_tests_only_when_asked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """User ruling 2026-10-06: a manual build runs the fast tests; the nightly orchestrator run passes
        `SLOW_TESTS=true` (its timer, `NIGHTLY_CRON`), and that run gathers the slow statistics."""
        local = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)["Jenkinsfile.local"]
        declared = local[local.index("name: 'SLOW_TESTS'") :]
        assert declared[: declared.index(")")].count("defaultValue: false") == 1
        stage = local[local.index("stage('Slow Tests')") :]
        assert stage.index(
            "when {\n                expression { return params.SLOW_TESTS }\n            }"
        ) < stage.index("steps {")

    def test_the_push_pipelines_keep_running_their_slow_tests(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The CON pin: a push to GitHub still runs the slow leg — only the local pipeline waits for the night."""
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)
        for name in ("Jenkinsfile", "ci.yml"):
            assert "SLOW_TESTS" not in files[name], name


class TestAPlaywrightDevDependencyInstallsItsBrowser:
    """Listing `playwright` under the `dev` extra IS the declaration (the slow marker's shape —
    no aisland knob): every RUNNER artifact that runs the tests fetches chromium right after the
    dev install, so a browser-driven test runs in CI instead of skipping into a green build that
    checks less. `Jenkinsfile.local` fetches nothing — `aisland setup` already installed the
    browser into the venv it tests. A project without the dependency gets byte-identical output."""

    def test_the_runner_artifacts_fetch_chromium_after_the_dev_install(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_playwright)
        content = files["Jenkinsfile"]
        assert content.count('sh "${VENV_BIN}/playwright install chromium"') == 1
        assert content.index("pip install -e .[dev]") < content.index("playwright install chromium")
        assert "playwright" not in files["Jenkinsfile.local"], "the local pipeline tests the venv setup provisioned"
        ci = files["ci.yml"]
        assert ci.count("playwright install --with-deps chromium") == 1, "the unit-tests job, and only it"
        at = ci.index("playwright install --with-deps chromium")
        assert ci.rindex('uv pip install --system -e ".[dev]"', 0, at) > ci.rindex(
            "- name: Install dependencies", 0, at
        )
        run = ci.index("pytest tests", at)
        assert "Install dependencies" not in ci[at:run], "the browser lands in the job that runs pytest"

    def test_a_slow_leg_fetches_it_too(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Both GitHub jobs that run pytest need the browser — the routine one and the slow one."""
        files = _scaffolded(monkeypatch, tmp_path, lambda d, n: _make_pyproject_with_playwright(d, n, slow_marker=True))
        assert files["ci.yml"].count("playwright install --with-deps chromium") == 2
        assert files["Jenkinsfile"].count("playwright install chromium") == 1, "one venv serves every stage"
        assert "playwright" not in files["Jenkinsfile.local"]

    def test_a_project_without_it_is_untouched(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject)
        for name, content in files.items():
            assert "playwright" not in content, name


def _make_gearlux_workspace(tmp_path: Path, projects: dict[str, str]) -> None:
    """A fake workspace where every project is a Gearlux submodule.

    Writes a ``.gitmodules`` whose every entry is on ``github.com:Gearlux/`` (so
    ``discover_github_projects`` classifies each name as internal) plus one
    directory per project carrying the given pyproject.toml text and an inner
    package dir.
    """
    lines = []
    for name in projects:
        lines += [f'[submodule "{name}"]', f"\tpath = {name}", f"\turl = git@github.com:Gearlux/{name}.git"]
    (tmp_path / ".gitmodules").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, pyproject in projects.items():
        dir_ = tmp_path / name
        dir_.mkdir(parents=True, exist_ok=True)
        (dir_ / "pyproject.toml").write_text(pyproject, encoding="utf-8")
        pkg = dir_ / name.replace("-", "")
        pkg.mkdir(exist_ok=True)
        (pkg / "__init__.py").touch()


def _scaffold_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, projects: dict[str, str]) -> dict[str, str]:
    """Scaffold the project named ``app`` inside a Gearlux workspace; return the RUNNER artifacts.

    ``Jenkinsfile.local`` is left out on purpose: it installs nothing, so it has no closure —
    ``TestTheLocalPipelineInstallsNothing`` pins that.
    """

    _make_gearlux_workspace(tmp_path, projects)
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr("aisland.ci.ci_scaffold.is_private_repo", lambda repository: False)
    ci_scaffold.scaffold("app", force=True, jenkins_url=NO_JENKINS)
    return {
        "Jenkinsfile": (tmp_path / "app" / "Jenkinsfile").read_text(encoding="utf-8"),
        "ci.yml": (tmp_path / "app" / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
    }


def _venv_check_script(jenkinsfile_local: str, python: str, workspace_root: Path) -> str:
    """The emitted venv-check ``sh`` block, unwrapped from Groovy with Jenkins' substitutions applied."""
    start = jenkinsfile_local.index('sh """', jenkinsfile_local.index("stage('Initialize')")) + len('sh """')
    script = jenkinsfile_local[start : jenkinsfile_local.index('"""', start)]
    return script.replace("${VENV_BIN}/python3", python).replace("${env.WORKSPACE_ROOT}", str(workspace_root))


class TestTheLocalPipelineInstallsNothing:
    """`Jenkinsfile.local` tests a checkout against the workspace venv exactly as `aisland setup`
    left it. Installing is setup's job: on 2026-09-13 the local Initialize stage spent up to
    1430 s re-resolving ~80 packages (`uv pip install -e .[dev]`, then `.[notebook]`) to install
    nothing but the project itself, and every build queued on the shared `.venv` lock. The runner
    files (`Jenkinsfile`, `ci.yml`) have no workspace venv and keep installing."""

    _PROJECTS = {
        "app": (
            '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["libmid>=0.1.0"]\n\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest", "playwright>=1.40.0,<2.0.0"]\n'
            'notebook = ["jupyter"]\n'
        ),
        "libmid": '[project]\nname = "libmid"\nversion = "0.0.0"\n',
    }

    def _local(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
        _scaffold_app(monkeypatch, tmp_path, self._PROJECTS)
        return (tmp_path / "app" / "Jenkinsfile.local").read_text(encoding="utf-8")

    def test_the_local_pipeline_runs_no_install_step(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        local = self._local(monkeypatch, tmp_path)
        for forbidden in ("pip install", "/uv", "git+https://", "playwright install", "WORKSPACE_ROOT}/libmid"):
            assert forbidden not in local, f"Jenkinsfile.local still carries {forbidden!r}"

    def test_the_runner_files_still_install(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The CON pin: a runner has no workspace venv, so removing the local installs must not
        reach the files a runner executes."""
        files = _scaffold_app(monkeypatch, tmp_path, self._PROJECTS)
        assert 'sh "${VENV_BIN}/uv pip install -e .[dev]"' in files["Jenkinsfile"]
        assert 'uv pip install --system -e ".[dev]"' in files["ci.yml"]
        for name, content in files.items():
            assert "Gearlux/libmid.git" in content, name

    @staticmethod
    def _install_record(site: Path, source: Path) -> dict[str, str]:
        """A venv's view of an editable install of ``app`` from ``source``: the dist-info with the
        `direct_url.json` every PEP 660 install writes. Returns the env that puts it on sys.path."""
        info = site / "app-0.0.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: app\nVersion: 0.0.0\n", encoding="utf-8")
        record = {"url": source.resolve().as_uri(), "dir_info": {"editable": True}}
        (info / "direct_url.json").write_text(json.dumps(record), encoding="utf-8")
        return {**os.environ, "PYTHONPATH": str(site)}

    def _run_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]
    ) -> "subprocess.CompletedProcess[str]":
        script = _venv_check_script(self._local(monkeypatch, tmp_path), sys.executable, tmp_path)
        # From INSIDE the checkout, as Jenkins runs it (customWorkspace).
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=tmp_path / "app", env=env)

    def test_the_venv_check_passes_for_the_installed_checkout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env = self._install_record(tmp_path / "site", tmp_path / "app")
        run = self._run_check(monkeypatch, tmp_path, env)
        assert run.returncode == 0, run.stderr
        assert f"app -> {(tmp_path / 'app').resolve()}" in run.stdout

    def test_the_venv_check_refuses_a_venv_without_the_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        run = self._run_check(monkeypatch, tmp_path, env)
        assert run.returncode == 1
        assert f"app is not installed from {(tmp_path / 'app').resolve()} (found: not installed)" in run.stderr
        assert "run `aisland setup`" in run.stderr

    def test_the_venv_check_refuses_an_install_from_another_copy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The silent case: the project IS installed, but from somewhere else — the gates would run
        against code that is not the checkout under test."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        env = self._install_record(tmp_path / "site", elsewhere)
        run = self._run_check(monkeypatch, tmp_path, env)
        assert run.returncode == 1
        assert f"(found: {elsewhere.resolve().as_uri()})" in run.stderr

    def test_a_stale_egg_info_in_the_checkout_does_not_shadow_the_venv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Why the check runs `python3 -P`: Jenkins runs inside the checkout, and without -P the
        working directory is first on sys.path, so a leftover `app.egg-info` there (an old
        setuptools build — a real workspace checkout carries one) is found before the venv's record
        and a correctly-installed project fails with "found: an install with no source URL"."""
        egg = tmp_path / "app" / "app.egg-info"
        egg.mkdir(parents=True)
        (egg / "PKG-INFO").write_text("Metadata-Version: 2.1\nName: app\nVersion: 0.0.0\n", encoding="utf-8")
        env = self._install_record(tmp_path / "site", tmp_path / "app")
        run = self._run_check(monkeypatch, tmp_path, env)
        assert run.returncode == 0, run.stderr


class TestTheInternalDepClosureFollowsWhatCIInstalls:
    """The pre-install closure mirrors exactly what CI installs, nothing more.

    CI installs each internal dep bare (``--no-deps`` + the root's ``.[dev,…]``,
    which resolves only the dep's BASE dependencies), so a transitive dep's
    extras are never requested and must not add pre-installs — the old
    walk-every-extras-group rule made the CI of a project depending on one other project install
    everything that project's dev extra reaches (torpaido through matrainer's extra, a sibling
    through a dev extra) and made annotaide's CI pre-install
    annotaide ITSELF from git@main. The ROOT is different: its dev/notebook and
    selected framework/tracker extras ARE installed, so their internal deps stay
    in. An extras-carrying spec (``libmid[gpu]``) is a real request: the named
    extra's contents are walked for that dep — the old regex split couldn't even
    parse the bracket form and silently dropped the whole spec.
    """

    def test_a_transitive_deps_extras_do_not_add_preinstalls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The shape that motivated the rule: app -> libmid, whose dev extra names libextra.

        CI installs libmid bare, so libmid's dev extra is never requested and
        libextra must not be pre-installed.
        """
        files = _scaffold_app(
            monkeypatch,
            tmp_path,
            {
                "app": '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["libmid>=0.1.0"]\n',
                "libmid": (
                    '[project]\nname = "libmid"\nversion = "0.0.0"\n\n'
                    "[project.optional-dependencies]\n"
                    'dev = ["libextra>=0.1.0", "pytest"]\n'
                ),
                "libextra": '[project]\nname = "libextra"\nversion = "0.0.0"\n',
            },
        )
        for name, content in files.items():
            assert "Gearlux/libmid.git" in content, f"{name}: the declared dep must be pre-installed"
            assert "Gearlux/libextra.git" not in content, f"{name}: a transitive dep's dev extra is never installed"

    def test_the_roots_own_dev_and_notebook_extras_are_walked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A root whose own extras name internal deps (the CON pin): CI installs the ROOT's `.[dev]` and the
        best-effort `.[notebook]`, so internal deps named there stay pre-installed —
        while an extra CI never installs (`viz`) adds nothing.
        """
        files = _scaffold_app(
            monkeypatch,
            tmp_path,
            {
                "app": (
                    '[project]\nname = "app"\nversion = "0.0.0"\n\n'
                    "[project.optional-dependencies]\n"
                    'dev = ["devdep>=0.1.0", "pytest"]\n'
                    'notebook = ["nbdep>=0.1.0", "jupyter"]\n'
                    'viz = ["vizdep>=0.1.0"]\n'
                ),
                "devdep": '[project]\nname = "devdep"\nversion = "0.0.0"\n',
                "nbdep": '[project]\nname = "nbdep"\nversion = "0.0.0"\n',
                "vizdep": '[project]\nname = "vizdep"\nversion = "0.0.0"\n',
            },
        )
        for name, content in files.items():
            assert "Gearlux/devdep.git" in content, f"{name}: the dev extra IS installed by CI"
            assert "Gearlux/nbdep.git" in content, f"{name}: the notebook extra IS installed (best-effort step)"
            assert "Gearlux/vizdep.git" not in content, f"{name}: an uninstalled extra must not add pre-installs"

    def test_an_extras_carrying_spec_resolves_and_walks_the_named_extra(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`libmid[gpu]>=0.1.0` with NO bare spec beside it: libmid must still be
        pre-installed (the old regex stripped the spec to `libmid[gpu]`, matching
        nothing), and the REQUESTED extra's internal content (gpudep) comes with it —
        while libmid's unrequested `docs` extra adds nothing.
        """
        files = _scaffold_app(
            monkeypatch,
            tmp_path,
            {
                "app": '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["libmid[gpu]>=0.1.0"]\n',
                "libmid": (
                    '[project]\nname = "libmid"\nversion = "0.0.0"\n\n'
                    "[project.optional-dependencies]\n"
                    'gpu = ["gpudep>=0.1.0"]\n'
                    'docs = ["docsdep>=0.1.0"]\n'
                ),
                "gpudep": '[project]\nname = "gpudep"\nversion = "0.0.0"\n',
                "docsdep": '[project]\nname = "docsdep"\nversion = "0.0.0"\n',
            },
        )
        for name, content in files.items():
            assert "Gearlux/libmid.git" in content, f"{name}: a bracket spec must resolve to its dep"
            assert "Gearlux/gpudep.git" in content, f"{name}: the requested extra's content is installed by CI"
            assert "Gearlux/docsdep.git" not in content, f"{name}: an unrequested extra adds nothing"

    def test_a_project_never_preinstalls_itself(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The annotaide shape: a dependency cycle reaching back to the root must not
        make the root's CI install ITS OWN main branch over the checkout under test.
        """
        files = _scaffold_app(
            monkeypatch,
            tmp_path,
            {
                "app": '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["libmid>=0.1.0"]\n',
                "libmid": '[project]\nname = "libmid"\nversion = "0.0.0"\ndependencies = ["app>=0.1.0"]\n',
            },
        )
        for name, content in files.items():
            assert "Gearlux/libmid.git" in content, name
            assert "Gearlux/app.git" not in content, f"{name}: a project must never pre-install itself"

    def test_transitive_base_deps_are_still_fully_walked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The reason the closure exists at all: `.[dev]` must resolve libmid's own
        base deps, and libdeep is not on PyPI — so base deps stay transitively
        collected, topologically ordered (libdeep installs before libmid).
        """
        files = _scaffold_app(
            monkeypatch,
            tmp_path,
            {
                "app": '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["libmid>=0.1.0"]\n',
                "libmid": '[project]\nname = "libmid"\nversion = "0.0.0"\ndependencies = ["libdeep>=0.1.0"]\n',
                "libdeep": '[project]\nname = "libdeep"\nversion = "0.0.0"\n',
            },
        )
        for name, content in files.items():
            assert "Gearlux/libdeep.git" in content, f"{name}: transitive base deps must stay in the closure"
            assert content.index("Gearlux/libdeep.git") < content.index(
                "Gearlux/libmid.git"
            ), f"{name}: a dep installs before its consumer"

    def test_the_roots_selected_axis_extras_are_walked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A selected framework extra is part of `.[dev,<selected>]`, so its internal
        deps are pre-installed — an axis extra the committed selection omits is not.
        """
        (tmp_path / "workspace.toml").write_text('[workspace]\nframework = ["keras"]\n', encoding="utf-8")
        files = _scaffold_app(
            monkeypatch,
            tmp_path,
            {
                "app": (
                    '[project]\nname = "app"\nversion = "0.0.0"\n\n'
                    "[project.optional-dependencies]\n"
                    'keras = ["kerasdep>=0.1.0"]\n'
                    'torch = ["torchdep>=0.1.0"]\n\n'
                    "[tool.aisland.selection.framework]\n"
                    'question = "Which ML frameworks?"\n'
                    'choices = ["keras", "torch"]\n'
                ),
                "kerasdep": '[project]\nname = "kerasdep"\nversion = "0.0.0"\n',
                "torchdep": '[project]\nname = "torchdep"\nversion = "0.0.0"\n',
            },
        )
        for name, content in files.items():
            assert "Gearlux/kerasdep.git" in content, f"{name}: a selected axis extra IS installed by CI"
            assert "Gearlux/torchdep.git" not in content, f"{name}: an unselected axis extra is not"


class TestTheWorkflowActionsRunOnNode24:
    """Every `uses:` line a generated `ci.yml` emits comes from ONE table, `GH_ACTIONS`, pinned to
    major tags whose `action.yml` declares `using: node24` (2026-09-26: GitHub annotated every
    run "Node.js 20 is deprecated … being forced to run on Node.js 24" for checkout@v4,
    setup-python@v5 and upload-artifact@v4). One table means a bump is one line, and this pin
    catches a template line that bypasses it."""

    @staticmethod
    def _every_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
        """A ci.yml carrying every optional job and step: the slow leg, the Node tests AND the app-token step."""

        marker = "\n[tool.pytest.ini_options]\nmarkers = [\"slow: heavy tests; deselect with -m 'not slow'.\"]\n"
        _make_gearlux_workspace(
            tmp_path,
            {
                "lib": '[project]\nname = "lib"\nversion = "0.0.0"\n',
                "app": '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["lib>=0.0.0"]\n' + marker,
            },
        )
        monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
        monkeypatch.setattr("aisland.ci.ci_scaffold.is_private_repo", lambda repository: True)
        _with_node_tests(tmp_path / "app")
        ci_scaffold.scaffold("app", force=True, jenkins_url=NO_JENKINS)
        return (tmp_path / "app" / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def test_every_uses_line_comes_from_the_one_table(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from aisland.ci.ci_scaffold import GH_ACTIONS

        ci = self._every_job(monkeypatch, tmp_path)
        used = {line.split("uses:", 1)[1].strip() for line in ci.splitlines() if "uses:" in line}
        assert used == set(GH_ACTIONS.values()), used ^ set(GH_ACTIONS.values())
        assert "slow-tests:" in ci and "node-tests:" in ci and "create-github-app-token" in ci  # every optional path

    def test_the_table_names_the_node24_majors(self) -> None:
        from aisland.ci.ci_scaffold import GH_ACTIONS

        assert GH_ACTIONS == {
            "checkout": "actions/checkout@v7",
            "setup-python": "actions/setup-python@v7",
            "setup-uv": "astral-sh/setup-uv@v7",
            "upload-artifact": "actions/upload-artifact@v7",
            "create-github-app-token": "actions/create-github-app-token@v3",
            "setup-node": "actions/setup-node@v7",
        }


@pytest.mark.parametrize("make", [_make_pyproject, _make_pyproject_with_slow_marker], ids=["plain", "slow marker"])
def test_every_coverage_run_measures_branches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make: Any) -> None:
    """Workspace rule "Keep Branch Coverage On": each file's coverage run measures branches, not lines only."""
    files = _scaffolded(monkeypatch, tmp_path, make)
    for name in ("Jenkinsfile", "Jenkinsfile.local", "ci.yml"):
        runs = [line for line in files[name].splitlines() if "--cov=" in line]
        assert runs, name
        assert all("--cov-branch" in line for line in runs), (name, runs)


class TestALongStageHasATimeLimit:
    """A stage that runs the project's code gets a time limit in `Jenkinsfile.local` (user choice 2026-10-15): past
    it the stage FAILS naming itself and the limit, so one hung test never holds the nightly orchestrator again
    (annotaide.local #37's Unit Tests waited 109 min on a test ignoring SIGINT, another project's Slow
    Tests ran 519 min, and acme.local #36 waited on both). The limits live in ONE table, `STAGE_LIMITS`, and every
    project gets the same; Initialize and the quality gates run tools, not the project's code, and get none; the
    push pipelines get none here. A timed-out pytest writes no report, and Jenkins would record the previous build's
    file in its place (annotaide.local #37 recorded #36's 2 859 cases), so each pytest call first removes the files
    it writes. Measured on a probe job with a 1 min limit around a shell ignoring SIGINT: the stage FAILED at 80 s,
    the build FAILURE, the console ending `ERROR: Unit Tests timed out after 1 min - its limit in …`."""

    def test_every_long_stage_runs_inside_its_limit_from_the_one_table(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        local = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker_and_node_tests)["Jenkinsfile.local"]
        assert set(STAGE_LIMITS) == {"Unit Tests", "Slow Tests", "Verify Examples", "Verify Notebooks", "Node Tests"}
        for stage, minutes in STAGE_LIMITS.items():
            assert isinstance(minutes, int) and minutes > 0, stage
            wrapper = f"limited('{stage}', {minutes}) {{"
            assert local.count(wrapper) == 1, stage
            opened = local.index(f"stage('{stage}')")
            assert opened < local.index("steps {", opened) < local.index(wrapper, opened), stage
        assert local.count("limited(") == len(STAGE_LIMITS) + 1, "the five calls and the definition, nothing else"
        assert "limited('Initialize'" not in local and "limited('Quality Gates'" not in local

    def test_the_helper_fails_the_stage_naming_it_and_its_limit_and_keeps_an_early_abort(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        local = _scaffolded(monkeypatch, tmp_path, _make_pyproject)["Jenkinsfile.local"]
        helper = local[local.index("def limited(String stage, int minutes, Closure body)") : local.index("pipeline {")]
        assert "timeout(time: minutes, unit: 'MINUTES')" in helper
        assert "catch (org.jenkinsci.plugins.workflow.steps.FlowInterruptedException" in helper
        # Abort clicked before the limit is rethrown and stays an abort; only the limit's own interruption fails.
        assert "if (System.currentTimeMillis() - started < minutes * 60000L)" in helper
        assert helper.index("throw interrupted") < helper.index("error(")
        assert f'error("{TIMED_OUT_MESSAGE}")' in helper
        assert TIMED_OUT_MESSAGE.startswith("${stage} timed out after ${minutes} min")
        # The provenance header stays the first thing in the file (root Generator Provenance rule).
        assert local.index("To regenerate:") < local.index("def limited(")

    def test_the_helpers_comment_says_what_it_does_and_carries_no_history(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Every project's `Jenkinsfile.local` carries the helper — a company's repository among them: its comment
        names no build, project or date."""
        local = _scaffolded(monkeypatch, tmp_path, _make_pyproject)["Jenkinsfile.local"]
        comment = local[local.index("// A long stage's time limit") : local.index("def limited(")]
        assert "past it the stage fails naming itself and the limit" in comment
        assert not re.search(r"\d{4}-\d{2}-\d{2}|#\d+|\.local\b", comment), comment

    def test_a_project_without_slow_tests_gets_no_slow_limit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        local = _scaffolded(monkeypatch, tmp_path, _make_pyproject)["Jenkinsfile.local"]
        assert "limited('Slow Tests'" not in local and "limited('Node Tests'" not in local
        assert local.count("limited(") == 4, "three calls and the definition"

    def test_the_push_jenkinsfile_carries_no_limit(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The CON pin: the push Jenkinsfile's limits stay a follow-up (aisland/TASKS.md)."""
        content = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)["Jenkinsfile"]
        assert "limited(" not in content and "timeout(" not in content

    def test_each_github_job_running_the_projects_code_has_three_times_its_jenkins_limit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A GitHub runner is slower than this machine, whose runs `STAGE_LIMITS` was measured on: each job gets
        RUNNER_LIMIT_FACTOR (3) times its stage's limit (user pick 2026-10-28), instead of GitHub's six hours. The
        quality gates run tools, not the project's code, and get none, as in Jenkins."""
        import yaml

        from aisland.ci.ci_scaffold import RUNNER_LIMIT_FACTOR, STAGE_LIMITS

        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker_and_node_tests)
        jobs = yaml.safe_load(files["ci.yml"])["jobs"]
        stages = {
            "unit-tests": "Unit Tests",
            "slow-tests": "Slow Tests",
            "verify-examples": "Verify Examples",
            "verify-notebooks": "Verify Notebooks",
            "node-tests": "Node Tests",
        }
        assert RUNNER_LIMIT_FACTOR == 3
        assert {job: jobs[job].get("timeout-minutes") for job in stages} == {
            job: STAGE_LIMITS[stage] * 3 for job, stage in stages.items()
        }
        assert "timeout-minutes" not in jobs["quality-gates"]

    def test_each_pytest_call_first_removes_the_files_it_writes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Both Jenkins workspaces persist between builds (the local one IS the checkout); a GitHub runner starts
        empty."""
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject_with_slow_marker)
        for name in ("Jenkinsfile", "Jenkinsfile.local"):
            content = files[name]
            unit = content.index("stage('Unit Tests')")
            cleared = content.index("sh 'rm -f test-report.xml coverage.xml'", unit)
            assert cleared < content.index("pytest tests", unit), name
            slow = content.index("stage('Slow Tests')")
            cleared = content.index("sh 'rm -f slow-test-report.xml'", slow)
            assert cleared < content.index("pytest tests -m slow", slow), name
        assert "rm -f" not in files["ci.yml"]


class TestAPackageJsonWithATestScriptGetsNodeTests:
    """A `package.json` whose `scripts.test` is set is the project's declaration that `npm test` is a test run of its
    own (user pick 2026-10-31: the editor extension's tests ran in no CI), the same shape as the `slow` marker: no
    aisland knob. Each such folder gets `npm ci` (or `npm install` without a lock file) and `npm test` in a Node Tests
    stage of all three files, the venv first on the PATH so a test starting the project's Python finds it; a project
    without one gets none of it."""

    def test_each_folder_runs_its_install_and_its_tests_in_all_three_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def make(dir_: Path, name: str) -> None:
            _make_pyproject(dir_, name)
            _with_node_tests(dir_)
            (dir_ / "editors" / "vscode").mkdir(parents=True)
            (dir_ / "editors" / "vscode" / "package.json").write_text('{"scripts": {"test": "node --test out/"}}')

        files = _scaffolded(monkeypatch, tmp_path, make)
        install = "if [ -f package-lock.json ]; then npm ci; else npm install; fi"
        stages = {}
        for name in ("Jenkinsfile", "Jenkinsfile.local"):
            content = files[name]
            stage = content[content.index("stage('Node Tests')") : content.index("stage('Verify Examples')")]
            assert 'withEnv(["PATH+VENV=${VENV_BIN}"])' in stage, name
            assert stage.index("dir('editors/vscode')") < stage.index("dir('frontend')"), name
            assert stage.count("sh 'npm test'") == 2, name
            assert content.index("stage('Unit Tests')") < content.index("stage('Node Tests')"), name
            stages[name] = stage
        assert stages["Jenkinsfile"].count(f"sh '{install}'") == 2
        # The local pipeline installs nothing (user instruction 2026-09-13): it tests the checkout's packages, and a
        # folder without them stops the stage naming the command.
        local = stages["Jenkinsfile.local"]
        assert "npm ci" not in local and "npm install;" not in local
        assert (
            local.count(
                'if [ ! -d node_modules ]; then echo "proj/frontend has no node_modules - run npm install there"; exit 1; fi'
            )
            == 1
        )
        assert local.index(f"limited('Node Tests', {STAGE_LIMITS['Node Tests']})") < local.index(
            "dir('editors/vscode')"
        )

        import yaml

        job = yaml.safe_load(files["ci.yml"])["jobs"]["node-tests"]
        assert job["needs"] == ["quality-gates"]
        runs = [step for step in job["steps"] if "working-directory" in step]
        assert [(step["working-directory"], step["run"].splitlines()) for step in runs] == [
            ("editors/vscode", [install, "npm test"]),
            ("frontend", [install, "npm test"]),
        ]
        names = [step.get("name") for step in job["steps"]]
        assert names.index("Install dependencies") < names.index("Set up Node.js 24") < names.index(runs[0]["name"])
        # One blank line parts the new job and stage from the next, as between every other two.
        assert "        npm test\n\n  verify-examples:" in files["ci.yml"]
        for name in ("Jenkinsfile", "Jenkinsfile.local"):
            assert "            }\n        }\n\n        stage('Verify Examples')" in files[name], name

    def test_a_project_without_one_gets_no_node_stage(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        files = _scaffolded(monkeypatch, tmp_path, _make_pyproject)
        for name, content in files.items():
            assert "Node Tests" not in content and "npm" not in content and "setup-node" not in content, name
        # The line that includes the stage adds no line of its own: one blank line still parts Unit Tests from what
        # follows, as before the stage existed.
        for name in ("Jenkinsfile", "Jenkinsfile.local"):
            assert "            }\n        }\n\n        stage('Verify Examples')" in files[name], name
        assert "      if: always()\n\n  verify-examples:" in files["ci.yml"]


class TestWhichFoldersHoldNodeTests:
    """`node_test_folders`: the `package.json` files git sees, else a walk; only a non-empty `test` script counts."""

    @staticmethod
    def _write(root: Path, files: dict[str, str]) -> None:
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")

    def test_outside_git_the_folders_are_walked_and_only_a_test_script_counts(self, tmp_path: Path) -> None:
        from aisland.ci.ci_scaffold import node_test_folders

        self._write(
            tmp_path,
            {
                "package.json": '{"scripts": {"test": "jest"}}',
                "web/package.json": '{"scripts": {"test": "vitest run"}}',
                "web/node_modules/dep/package.json": '{"scripts": {"test": "mocha"}}',
                ".vscode-test/x/package.json": '{"scripts": {"test": "mocha"}}',
                "no-test/package.json": '{"scripts": {"build": "tsc"}}',
                "blank-test/package.json": '{"scripts": {"test": "  "}}',
                "no-scripts/package.json": '{"name": "x"}',
                "broken/package.json": "{not json",
                "a-list/package.json": "[1, 2]",
            },
        )
        assert node_test_folders(tmp_path) == [".", "web"]

    def test_in_git_an_ignored_folder_is_no_source_and_an_untracked_one_is(self, tmp_path: Path) -> None:
        from aisland.ci.ci_scaffold import node_test_folders

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        self._write(
            tmp_path,
            {
                ".gitignore": "data/\n",
                "data/plugin/package.json": '{"scripts": {"test": "mocha"}}',
                "frontend/package.json": '{"scripts": {"test": "vitest run"}}',
                "frontend/node_modules/dep/package.json": '{"scripts": {"test": "mocha"}}',
                "tools/package.json": '{"scripts": {"test": "node --test"}}',
            },
        )
        subprocess.run(["git", "-C", str(tmp_path), "add", "frontend/package.json"], check=True)
        assert node_test_folders(tmp_path) == ["frontend", "tools"]

    def test_a_folder_with_none_has_none(self, tmp_path: Path) -> None:
        from aisland.ci.ci_scaffold import node_test_folders

        assert node_test_folders(tmp_path) == []


class _Jenkins(http.server.BaseHTTPRequestHandler):
    """A Jenkins whose crumb is tied to the session it opens (a cookie), as Jenkins' own is; ``status`` answers every
    request instead when set."""

    status: Optional[int] = None
    body: bytes = b"Jenkinsfile successfully validated.\n"
    seen_cookies: List[str] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _refuse(self) -> bool:
        if self.status is None:
            return False
        self.send_response(self.status)
        self.end_headers()
        return True

    def do_GET(self) -> None:
        if self._refuse():
            return
        body = json.dumps({"crumbRequestField": "Jenkins-Crumb", "crumb": "c1"}).encode()
        self.send_response(200)
        self.send_header("Set-Cookie", "JSESSIONID=s1; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self._refuse():
            return
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).seen_cookies.append(self.headers.get("Cookie", ""))
        ok = self.headers.get("Cookie") == "JSESSIONID=s1" and self.headers.get("Jenkins-Crumb") == "c1"
        self.send_response(200 if ok else 403)
        self.end_headers()
        self.wfile.write(type(self).body if ok else b"")


@pytest.fixture
def jenkins() -> Iterator[str]:
    _Jenkins.status, _Jenkins.seen_cookies = None, []
    _Jenkins.body = b"Jenkinsfile successfully validated.\n"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Jenkins)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_linter_call_carries_the_session_its_crumb_belongs_to(jenkins: str) -> None:
    """Jenkins ties a crumb to the session it opened: the POST without the cookie was refused, and taken for valid."""
    assert ci_scaffold._validates("pipeline {}", "Jenkinsfile", jenkins)
    assert _Jenkins.seen_cookies == ["JSESSIONID=s1"]


@pytest.mark.parametrize("status", [500, 502])
def test_a_jenkins_answering_with_a_server_error_writes_nothing_as_validated(
    jenkins: str, tmp_path: Path, status: int
) -> None:
    """A 500 returned True: the file was written as validated."""
    _Jenkins.status = status
    target = tmp_path / "Jenkinsfile"
    with pytest.raises(
        click.ClickException, match=f"Jenkinsfile not validated: Jenkins answered .* with HTTP {status}"
    ):
        ci_scaffold._write(target, "pipeline {}", "Jenkinsfile", force=True, jenkins_url=jenkins)
    assert not target.exists()


@pytest.mark.parametrize("status", [401, 403])
def test_a_jenkins_that_wants_a_login_writes_the_file_saying_it_was_not_validated(
    jenkins: str, tmp_path: Path, status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Jenkins set up with a login refused every scaffold: the file is written, and the run says it is unchecked."""
    _Jenkins.status = status
    target = tmp_path / "Jenkinsfile"
    ci_scaffold._write(target, "pipeline {}", "Jenkinsfile", force=True, jenkins_url=jenkins)
    assert target.read_text() == "pipeline {}"
    err = capsys.readouterr().err
    assert f"Jenkinsfile is written NOT validated: Jenkins at {jenkins} wants a login (HTTP {status})" in err
    assert "aisland jenkins init --local" in err


def test_a_jenkinsfile_jenkins_rejects_is_refused(jenkins: str, tmp_path: Path) -> None:
    _Jenkins.body = b"Errors encountered validating Jenkinsfile:\nWorkflowScript: 1: no stages\n"
    target = tmp_path / "Jenkinsfile"
    with pytest.raises(click.ClickException, match="Template produced invalid Jenkinsfile"):
        ci_scaffold._write(target, "pipeline {}", "Jenkinsfile", force=True, jenkins_url=jenkins)
    assert not target.exists()


def test_no_jenkins_at_all_writes_the_file_saying_it_was_not_validated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "Jenkinsfile"
    ci_scaffold._write(target, "pipeline {}", "Jenkinsfile", force=True, jenkins_url=NO_JENKINS)
    assert target.read_text() == "pipeline {}"
    assert "Jenkins not reachable" in capsys.readouterr().out


# --- which folder, which order, what is measured ---------------------------------------------------------------


def test_scaffolding_every_project_passes_over_a_link_to_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A link to a folder of the workspace is a short name: `iterdir` scaffolded the project twice."""
    _make_pyproject(tmp_path / "models", "models")
    (tmp_path / "m").symlink_to(tmp_path / "models")
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    ci_scaffold.scaffold(None, force=True, jenkins_url=NO_JENKINS)
    assert (tmp_path / "models" / "Jenkinsfile").is_file()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["m", "models"]


@pytest.mark.parametrize("typed", ["models", "m"])
def test_a_project_is_found_by_its_workspace_toml_name_or_its_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, typed: str
) -> None:
    """`workspace_root / project_name` missed a row whose path is another folder, and took a link for a project."""
    _make_pyproject(tmp_path / "models-src", "models")
    (tmp_path / "workspace.toml").write_text('[project."models"]\npath = "models-src"\n')
    (tmp_path / "m").symlink_to(tmp_path / "models-src")
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    ci_scaffold.scaffold(typed, force=True, jenkins_url=NO_JENKINS)
    assert (tmp_path / "models-src" / "Jenkinsfile").is_file()
    assert (tmp_path / "m").is_symlink()


def test_the_package_folder_scan_is_the_same_on_every_file_system(tmp_path: Path) -> None:
    """The fallback scan took the first folder `iterdir` gave, an order that differs between file systems."""
    for name in ("zeta", "alpha", "tests"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").touch()
    assert ci_scaffold.package_folder(tmp_path, "the-dist") == "alpha"


def test_the_preinstalls_are_in_the_same_order_whether_a_dependency_is_checked_out_or_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The order came from the checkouts' own requirements: an emptied (detached) one contributed no edges, so the same
    workspace committed different CI files on two machines."""
    projects = {
        "app": '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["zlib", "alib"]\n',
        "zlib": '[project]\nname = "zlib"\nversion = "0.0.0"\n',
        "alib": '[project]\nname = "alib"\nversion = "0.0.0"\ndependencies = ["zlib"]\n',
    }
    (tmp_path / "here").mkdir()
    present = _scaffold_app(monkeypatch, tmp_path / "here", projects)
    detached = tmp_path / "there"
    detached.mkdir()
    _make_gearlux_workspace(detached, projects)
    (detached / "alib" / "pyproject.toml").unlink()  # a `github` source empties its folder
    (detached / "workspace.toml").write_text('[project."alib"]\nsource = "github"\n')
    monkeypatch.setattr(locations, "workspace_root", lambda: detached)
    ci_scaffold.scaffold("app", force=True, jenkins_url=NO_JENKINS)
    there = (detached / "app" / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert present["ci.yml"].index("Gearlux/alib.git") < present["ci.yml"].index("Gearlux/zlib.git")
    assert there == present["ci.yml"].replace(str(tmp_path / "here"), str(detached))


def test_a_project_shipped_inside_another_package_measures_its_own_subpackage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--cov=vision` measured the parent's name: the report listed the shim at 0 %, never the parent's code."""
    project = tmp_path / "vision-extra"
    (project / "vision" / "extra").mkdir(parents=True)
    (project / "vision" / "__init__.py").write_text(
        '__path__ = __import__("pkgutil").extend_path(__path__, __name__)\n'
    )
    (project / "vision" / "extra" / "__init__.py").write_text("")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "vision-extra"\nversion = "0.0.0"\n\n'
        '[tool.setuptools.packages.find]\ninclude = ["vision", "vision.extra*"]\n'
    )
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    ci_scaffold.scaffold("vision-extra", force=True, jenkins_url=NO_JENKINS)
    for relative in ("Jenkinsfile", "Jenkinsfile.local", ".github/workflows/ci.yml"):
        content = (project / relative).read_text()
        assert "--cov=vision.extra " in content and "--cov=vision " not in content, relative


def test_a_project_whose_parent_package_is_its_own_measures_the_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`include = ["models", "models.ops"]` with real code in `models/__init__.py` is no child project."""
    project = tmp_path / "models"
    (project / "models" / "ops").mkdir(parents=True)
    (project / "models" / "__init__.py").write_text("VERSION = 1\n")
    (project / "models" / "ops" / "__init__.py").write_text("")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "models"\nversion = "0.0.0"\n\n'
        '[tool.setuptools.packages.find]\ninclude = ["models", "models.ops"]\n'
    )
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    ci_scaffold.scaffold("models", force=True, jenkins_url=NO_JENKINS)
    assert "--cov=models " in (project / "Jenkinsfile").read_text()
