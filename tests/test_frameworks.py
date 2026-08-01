"""Tests for the ML-framework selection (`aisland framework`, aisland/frameworks.py).

Covers the four things a framework choice has to keep consistent:
- the declaration model (`[tool.aisland] frameworks`) and the declaration ∩ selection rule;
- the two configuration layers (committed file, per-machine `.git/config`) and the
  unset-vs-empty distinction the setup prompt hangs on;
- `resolve_for_setup`'s precedence (explicit flag > recorded selection > prompt/non-interactive);
- the CI scaffolder reading the COMMITTED layer ONLY — the invariant that keeps one developer's
  local narrowing out of every runner.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from aisland import frameworks as f

_PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"

[project.optional-dependencies]
dev = ["pytest"]
{extras}

[tool.aisland]
frameworks = [{declared}]
"""


def _project(root: Path, name: str, declared: list[str], extras: list[str] | None = None) -> Path:
    """A minimal project directory declaring `declared` as its framework extras."""
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    extra_lines = "\n".join(f'{e} = ["some-package"]' for e in (extras if extras is not None else declared))
    (project / "pyproject.toml").write_text(
        _PYPROJECT.format(name=name, extras=extra_lines, declared=", ".join(f'"{d}"' for d in declared))
    )
    return project


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A git repo (the per-machine layer is `git config --local`) with three projects."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _project(tmp_path, "recordstream", ["torch"])
    _project(tmp_path, "sonair", ["lightning", "keras"], extras=["lightning", "keras", "jax"])
    _project(tmp_path, "confluid", [])  # declares nothing — must stay unaffected
    return tmp_path


# --------------------------------------------------------------------------- #
# What a project declares
# --------------------------------------------------------------------------- #


class TestDeclaration:
    def test_reads_the_tool_aisland_table(self, workspace: Path) -> None:
        assert f.declared_frameworks(workspace / "sonair" / "pyproject.toml") == ["lightning", "keras"]

    def test_a_project_without_the_table_declares_nothing(self, workspace: Path) -> None:
        (workspace / "confluid" / "pyproject.toml").write_text('[project]\nname = "confluid"\n')

        assert f.declared_frameworks(workspace / "confluid" / "pyproject.toml") == []

    def test_a_missing_or_unreadable_file_declares_nothing(self, tmp_path: Path) -> None:
        """Never raise while reading a declaration — a detached (`pypi`-sourced) project has no
        pyproject at all, and setup walks every project before it can know which."""
        assert f.declared_frameworks(tmp_path / "nope" / "pyproject.toml") == []
        broken = tmp_path / "pyproject.toml"
        broken.write_text("this is not toml {[")
        assert f.declared_frameworks(broken) == []

    def test_available_is_the_union_across_projects(self, workspace: Path) -> None:
        assert f.available_frameworks(workspace) == ["keras", "lightning", "torch"]

    def test_declarations_omit_projects_that_declare_none(self, workspace: Path) -> None:
        assert set(f.declarations(workspace)) == {"recordstream", "sonair"}


# --------------------------------------------------------------------------- #
# declaration ∩ selection
# --------------------------------------------------------------------------- #


class TestExtrasFor:
    def test_is_the_intersection(self) -> None:
        assert f.extras_for("sonair", ["lightning", "keras"], ["keras", "torch"]) == ["keras"]

    def test_order_follows_the_declaration_not_the_selection(self) -> None:
        """So the emitted install target is stable however the names were typed."""
        assert f.extras_for("sonair", ["lightning", "keras"], ["keras", "lightning"]) == ["lightning", "keras"]

    def test_a_project_declaring_nothing_gets_nothing(self) -> None:
        assert f.extras_for("confluid", [], ["torch", "keras"]) == []

    def test_an_unset_selection_yields_nothing(self) -> None:
        """`None` must never be read as 'everything' here — callers that mean all pass all."""
        assert f.extras_for("sonair", ["lightning"], None) == []

    def test_install_target_composes_dev_and_frameworks(self, tmp_path: Path) -> None:
        assert f.install_target(tmp_path, True, ["lightning", "keras"]) == f"{tmp_path}[dev,lightning,keras]"
        assert f.install_target(tmp_path, True, []) == f"{tmp_path}[dev]"
        assert f.install_target(tmp_path, False, []) == str(tmp_path)


# --------------------------------------------------------------------------- #
# The two layers
# --------------------------------------------------------------------------- #


class TestLayers:
    def test_unset_is_none_and_distinct_from_empty(self, workspace: Path) -> None:
        """The distinction the setup prompt hangs on: nobody has chosen vs chose nothing."""
        assert f.read_committed_frameworks(workspace) is None

        f.write_committed_frameworks(workspace, [])
        assert f.read_committed_frameworks(workspace) == []

    def test_committed_round_trip_with_provenance(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch", "keras"])
        text = (workspace / f.FRAMEWORKS_FILE).read_text()

        assert f.read_committed_frameworks(workspace) == ["torch", "keras"]
        assert "AUTO-GENERATED" in text and "aisland framework set" in text
        assert tomllib.loads(text)["frameworks"] == ["torch", "keras"]

    def test_override_wins_over_committed(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch", "lightning"])
        f.set_local_override(workspace, ["keras"])

        assert f.effective_frameworks(workspace) == ["keras"]

    def test_clearing_the_override_falls_back_to_committed(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch"])
        f.set_local_override(workspace, ["keras"])
        f.set_local_override(workspace, None)

        assert f.local_override(workspace) is None
        assert f.effective_frameworks(workspace) == ["torch"]

    def test_an_empty_override_is_honoured_not_ignored(self, workspace: Path) -> None:
        """ "I want no frameworks on this machine" must beat a committed selection."""
        f.write_committed_frameworks(workspace, ["torch"])
        f.set_local_override(workspace, [])

        assert f.effective_frameworks(workspace) == []

    def test_setting_the_override_does_not_touch_the_committed_file(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch"])
        before = (workspace / f.FRAMEWORKS_FILE).read_bytes()
        f.set_local_override(workspace, ["keras"])

        assert (workspace / f.FRAMEWORKS_FILE).read_bytes() == before


# --------------------------------------------------------------------------- #
# Parsing an answer
# --------------------------------------------------------------------------- #


class TestParseSelection:
    AVAILABLE = ["keras", "lightning", "torch"]

    def test_blank_selects_everything(self) -> None:
        """A fresh setup must produce a workspace that works before it is optimised."""
        assert f.parse_selection("", self.AVAILABLE) == self.AVAILABLE

    @pytest.mark.parametrize("answer", ["torch keras", "torch,keras", " torch , keras "])
    def test_names_in_any_separator(self, answer: str) -> None:
        assert f.parse_selection(answer, self.AVAILABLE) == ["torch", "keras"]

    def test_one_based_indices(self) -> None:
        assert f.parse_selection("1,3", self.AVAILABLE) == ["keras", "torch"]

    def test_unknown_tokens_are_dropped_not_fatal(self) -> None:
        """This runs mid-`aisland setup`; aborting a provisioning run over a typo costs more."""
        assert f.parse_selection("torch nosuchframework 99", self.AVAILABLE) == ["torch"]

    def test_duplicates_collapse(self) -> None:
        assert f.parse_selection("torch torch 3", self.AVAILABLE) == ["torch"]


# --------------------------------------------------------------------------- #
# What setup does
# --------------------------------------------------------------------------- #


class TestResolveForSetup:
    def test_an_explicit_flag_wins_and_is_recorded(self, workspace: Path) -> None:
        """So `--framework` is a one-time flag, not something to repeat every run."""
        chosen, changed = f.resolve_for_setup(workspace, ["keras"], non_interactive=False)

        assert chosen == ["keras"] and changed
        assert f.read_committed_frameworks(workspace) == ["keras"]

    def test_a_recorded_selection_is_used_without_prompting(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch"])
        chosen, changed = f.resolve_for_setup(workspace, [], non_interactive=False)

        assert chosen == ["torch"] and not changed

    def test_a_recorded_override_beats_the_committed_file(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch"])
        f.set_local_override(workspace, ["keras"])

        assert f.resolve_for_setup(workspace, [], non_interactive=False)[0] == ["keras"]

    def test_non_interactive_with_nothing_recorded_selects_all(self, workspace: Path) -> None:
        """A headless setup must not hang, and must not leave a workspace that cannot train."""
        chosen, changed = f.resolve_for_setup(workspace, [], non_interactive=True)

        assert chosen == ["keras", "lightning", "torch"] and changed
        assert f.read_committed_frameworks(workspace) == chosen

    def test_a_recorded_empty_selection_is_not_re_prompted(self, workspace: Path) -> None:
        """Empty means chosen; only `None` means unchosen. A prompt here would be a nag loop."""
        f.write_committed_frameworks(workspace, [])
        chosen, changed = f.resolve_for_setup(workspace, [], non_interactive=False)

        assert chosen == [] and not changed

    def test_a_workspace_declaring_no_frameworks_is_inert(self, tmp_path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        _project(tmp_path, "confluid", [])

        assert f.resolve_for_setup(tmp_path, [], non_interactive=False) == ([], False)
        assert not (tmp_path / f.FRAMEWORKS_FILE).exists()


# --------------------------------------------------------------------------- #
# The CI half — committed layer ONLY
# --------------------------------------------------------------------------- #


class TestScaffoldReadsTheCommittedLayerOnly:
    """The same invariant `aisland source` documents: a per-machine choice must never reach a
    tracked build file. Here the failure would be silent and severe — one developer narrowing to
    torch locally would stop every runner from testing the Keras backend."""

    def _ci_extras(self, workspace: Path, project: str) -> str:
        """Reproduce the scaffolder's computation (jenkins.py: `ci_extras`)."""
        declared = f.declared_frameworks(workspace / project / "pyproject.toml")
        committed = f.read_committed_frameworks(workspace)
        return ",".join(["dev"] + f.extras_for(project, declared, committed))

    def test_committed_selection_reaches_ci(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch", "lightning", "keras"])

        assert self._ci_extras(workspace, "sonair") == "dev,lightning,keras"
        assert self._ci_extras(workspace, "recordstream") == "dev,torch"

    def test_a_machine_override_does_not(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch", "lightning", "keras"])
        f.set_local_override(workspace, [])  # this machine wants nothing

        assert self._ci_extras(workspace, "sonair") == "dev,lightning,keras"

    def test_a_project_declaring_nothing_emits_the_historical_dev(self, workspace: Path) -> None:
        f.write_committed_frameworks(workspace, ["torch", "keras"])

        assert self._ci_extras(workspace, "confluid") == "dev"

    def test_an_unset_committed_selection_emits_the_historical_dev(self, workspace: Path) -> None:
        """Before any `aisland framework set`, every generated file must be byte-unchanged."""
        assert self._ci_extras(workspace, "sonair") == "dev"


class TestScaffolderUsesThisModule:
    """A pin on the wiring itself: the emitted files must carry the extras, not just the helper."""

    def test_the_real_scaffolder_imports_the_committed_reader(self) -> None:
        """CI must read the COMMITTED layer — never `effective`, which folds in the override."""
        from aisland import selection
        from aisland.services import jenkins

        assert jenkins.sel_committed is selection.read_committed
        assert jenkins.sel_extras_for is selection.extras_for
        assert jenkins.sel_declared is selection.declared
        # The negative half is the point: an override must not be able to reach a tracked file.
        assert jenkins.sel_committed is not selection.effective

    def test_the_scaffolder_covers_every_axis(self) -> None:
        """A new axis must reach CI without editing the scaffolder again."""
        from aisland.selection import AXES, FRAMEWORKS, TRACKERS
        from aisland.services import jenkins

        assert jenkins.AXES is AXES
        assert set(AXES) == {FRAMEWORKS, TRACKERS}

    def test_every_emission_site_uses_ci_extras(self) -> None:
        """Guards against a new install line being added with a hardcoded `.[dev]`."""
        source = Path(jenkins_file()).read_text()

        assert "pip install -e .[dev]" not in source
        assert '.[dev]"' not in source


def jenkins_file() -> str:
    from aisland.services import jenkins

    return jenkins.__file__
