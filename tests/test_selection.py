"""Tests for the shared selection machinery and the TRACKER axis (aisland/selection.py).

`test_frameworks.py` covers the framework axis through its facade; this covers what the
generic module adds:
- the two axes are INDEPENDENT — separate keys in the one committed `config.toml`, separate
  git keys, separate vocabularies, and a write to one leaves every other section intact;
- an install target carries BOTH axes' selected extras, so changing one never drops the other;
- the tracker axis behaves like the framework one (layers, unset-vs-empty, precedence).
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from aisland import selection as s
from aisland.config import CONFIG_FILE
from aisland.config import update as update_config
from aisland.selection import EXTRAS, FRAMEWORKS, TRACKERS

_PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"

[project.optional-dependencies]
dev = ["pytest"]
{extras}

[tool.aisland]
{tables}
"""


def _project(root: Path, name: str, frameworks: list[str] = [], trackers: list[str] = []) -> Path:
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    extras = "\n".join(f'{e} = ["some-package"]' for e in [*frameworks, *trackers])
    tables = []
    if frameworks:
        tables.append("frameworks = [" + ", ".join(f'"{d}"' for d in frameworks) + "]")
    if trackers:
        tables.append("trackers = [" + ", ".join(f'"{d}"' for d in trackers) + "]")
    (project / "pyproject.toml").write_text(_PYPROJECT.format(name=name, extras=extras, tables="\n".join(tables)))
    return project


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _project(tmp_path, "matrainer", frameworks=["torch", "lightning"], trackers=["mlflow", "aim"])
    _project(tmp_path, "recordstream", frameworks=["torch"])
    _project(tmp_path, "confluid")  # declares nothing on either axis
    return tmp_path


# --------------------------------------------------------------------------- #
# the axes are independent
# --------------------------------------------------------------------------- #


def test_the_axes_use_separate_keys_in_one_file() -> None:
    """Separate KEYS, not separate files, since the workspace merged its config into one."""
    assert FRAMEWORKS.plural == "frameworks" and TRACKERS.plural == "trackers"
    assert FRAMEWORKS.config_key == "aisland.frameworks"
    assert TRACKERS.config_key == "aisland.trackers"
    assert FRAMEWORKS.table_path == ("tool", "aisland", "frameworks")
    assert TRACKERS.table_path == ("tool", "aisland", "trackers")


def test_each_axis_sees_only_its_own_vocabulary(workspace: Path) -> None:
    assert s.available(workspace, FRAMEWORKS) == ["lightning", "torch"]
    assert s.available(workspace, TRACKERS) == ["aim", "mlflow"]


def test_selecting_one_axis_leaves_the_other_unset(workspace: Path) -> None:
    """The failure this prevents: picking a training engine installing a tracking client."""
    s.write_committed(workspace, ["mlflow"], TRACKERS)
    assert s.read_committed(workspace, TRACKERS) == ["mlflow"]
    assert s.read_committed(workspace, FRAMEWORKS) is None


def test_a_project_declaring_neither_axis_is_unaffected(workspace: Path) -> None:
    pyproject = workspace / "confluid" / "pyproject.toml"
    assert s.declared(pyproject, FRAMEWORKS) == []
    assert s.declared(pyproject, TRACKERS) == []
    assert s.all_extras(workspace, pyproject, "confluid") == []


def test_declarations_are_read_per_axis(workspace: Path) -> None:
    assert s.declarations(workspace, TRACKERS) == {"matrainer": ["mlflow", "aim"]}
    assert set(s.declarations(workspace, FRAMEWORKS)) == {"matrainer", "recordstream"}


# --------------------------------------------------------------------------- #
# both axes reach the install target
# --------------------------------------------------------------------------- #


def test_all_extras_spans_both_axes(workspace: Path) -> None:
    s.write_committed(workspace, ["lightning"], FRAMEWORKS)
    s.write_committed(workspace, ["mlflow", "aim"], TRACKERS)
    pyproject = workspace / "matrainer" / "pyproject.toml"
    assert s.all_extras(workspace, pyproject, "matrainer") == ["lightning", "mlflow", "aim"]


def test_all_extras_follows_declaration_order_not_selection_order(workspace: Path) -> None:
    s.write_committed(workspace, [], FRAMEWORKS)
    s.write_committed(workspace, ["aim", "mlflow"], TRACKERS)  # reversed on purpose
    pyproject = workspace / "matrainer" / "pyproject.toml"
    assert s.all_extras(workspace, pyproject, "matrainer") == ["mlflow", "aim"]


def test_all_extras_honours_the_per_machine_override(workspace: Path) -> None:
    s.write_committed(workspace, ["mlflow", "aim"], TRACKERS)
    s.set_local_override(workspace, ["aim"], TRACKERS)
    pyproject = workspace / "matrainer" / "pyproject.toml"
    assert s.all_extras(workspace, pyproject, "matrainer") == ["aim"]


def test_install_target_spells_both_axes_once(workspace: Path) -> None:
    target = s.install_target(workspace / "matrainer", True, ["lightning", "mlflow"])
    assert target.endswith("[dev,lightning,mlflow]")


# --------------------------------------------------------------------------- #
# the tracker axis behaves like the framework one
# --------------------------------------------------------------------------- #


def test_unset_is_not_empty(workspace: Path) -> None:
    """Setup prompts for unset and honours empty — the distinction the prompt hangs on."""
    assert s.read_committed(workspace, TRACKERS) is None
    s.write_committed(workspace, [], TRACKERS)
    assert s.read_committed(workspace, TRACKERS) == []


def test_the_override_wins_over_the_committed_file(workspace: Path) -> None:
    s.write_committed(workspace, ["mlflow"], TRACKERS)
    s.set_local_override(workspace, ["aim"], TRACKERS)
    assert s.effective(workspace, TRACKERS) == ["aim"]
    assert s.read_committed(workspace, TRACKERS) == ["mlflow"]  # the committed layer is untouched


def test_clearing_the_override_falls_back_to_committed(workspace: Path) -> None:
    s.write_committed(workspace, ["mlflow"], TRACKERS)
    s.set_local_override(workspace, ["aim"], TRACKERS)
    s.set_local_override(workspace, None, TRACKERS)
    assert s.effective(workspace, TRACKERS) == ["mlflow"]


def test_the_committed_file_is_valid_toml_with_the_axis_key(workspace: Path) -> None:
    path = s.write_committed(workspace, ["mlflow", "aim"], TRACKERS)
    assert path.name == CONFIG_FILE
    assert tomllib.loads(path.read_text()) == {"trackers": ["mlflow", "aim"]}


def test_writing_one_axis_leaves_the_other_sections_intact(workspace: Path) -> None:
    """Both axes and the source registry share ONE file, so a write must be surgical."""
    s.write_committed(workspace, ["lightning"], FRAMEWORKS)
    update_config(workspace, "sources", {"loggair": {"source": "pypi"}})
    s.write_committed(workspace, ["mlflow"], TRACKERS)

    data = tomllib.loads((workspace / CONFIG_FILE).read_text())
    assert data == {"frameworks": ["lightning"], "trackers": ["mlflow"], "sources": {"loggair": {"source": "pypi"}}}


def test_unsetting_one_axis_keeps_the_file_and_its_other_sections(workspace: Path) -> None:
    """Dropping the KEY is what unset means now; deleting the file would take sources with it."""
    s.write_committed(workspace, ["lightning"], FRAMEWORKS)
    s.write_committed(workspace, ["mlflow"], TRACKERS)
    update_config(workspace, TRACKERS.plural, None)

    assert s.read_committed(workspace, TRACKERS) is None
    assert s.read_committed(workspace, FRAMEWORKS) == ["lightning"]


def test_the_managed_header_names_the_right_command(workspace: Path) -> None:
    """The comments are the only instruction the file carries; each section names its command."""
    text = s.write_committed(workspace, ["mlflow"], TRACKERS).read_text()
    assert "aisland tracker set" in text
    assert "aisland.trackers" in text
    # The tracker section only — a file holding no framework selection must not document one.
    assert "aisland framework list" not in text


def test_resolve_for_setup_records_an_explicit_request(workspace: Path) -> None:
    chosen, just_chosen = s.resolve_for_setup(workspace, ["aim"], False, TRACKERS)
    assert chosen == ["aim"] and just_chosen
    assert s.read_committed(workspace, TRACKERS) == ["aim"]


def test_resolve_for_setup_reuses_a_recorded_selection(workspace: Path) -> None:
    s.write_committed(workspace, ["mlflow"], TRACKERS)
    chosen, just_chosen = s.resolve_for_setup(workspace, None, True, TRACKERS)
    assert chosen == ["mlflow"] and not just_chosen


def test_resolve_for_setup_non_interactive_selects_everything(workspace: Path) -> None:
    """A headless setup must not hang, and must not leave a backend nothing can test."""
    chosen, just_chosen = s.resolve_for_setup(workspace, None, True, TRACKERS)
    assert chosen == ["aim", "mlflow"] and just_chosen


def test_resolve_for_setup_is_inert_without_declarations(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _project(tmp_path, "confluid")
    assert s.resolve_for_setup(tmp_path, None, True, TRACKERS) == ([], False)
    assert not (tmp_path / CONFIG_FILE).exists()


def test_parse_selection_warns_with_the_axis_noun(capsys: pytest.CaptureFixture) -> None:
    assert s.parse_selection("mlflow nope", ["mlflow", "aim"], TRACKERS) == ["mlflow"]
    assert "unknown tracker 'nope'" in capsys.readouterr().out


def test_parse_selection_accepts_indexes_and_blank_means_all() -> None:
    assert s.parse_selection("2", ["aim", "mlflow"], TRACKERS) == ["mlflow"]
    assert s.parse_selection("", ["aim", "mlflow"], TRACKERS) == ["aim", "mlflow"]


def test_describe_keys_rows_by_the_axis_noun(workspace: Path) -> None:
    s.write_committed(workspace, ["mlflow"], TRACKERS)
    rows = s.describe(workspace, TRACKERS)
    assert [row["tracker"] for row in rows] == ["aim", "mlflow"]
    assert {row["tracker"]: row["effective"] for row in rows} == {"aim": "no", "mlflow": "yes"}
    assert rows[0]["projects"] == "matrainer"


# --------------------------------------------------------------------------- #
# editable_target — the one spelling of "install this checkout", shared by setup and sources
# --------------------------------------------------------------------------- #


def test_editable_target_carries_the_dev_extra_and_both_axes(workspace: Path) -> None:
    update_config(workspace, "frameworks", ["lightning"])
    update_config(workspace, "trackers", ["aim"])
    assert (
        s.editable_target(workspace, workspace / "matrainer", "matrainer")
        == f"{workspace / 'matrainer'}[dev,lightning,aim]"
    )
    assert s.editable_target(workspace, workspace / "confluid", "confluid") == f"{workspace / 'confluid'}[dev]"


def test_editable_target_without_a_pyproject_is_the_bare_directory(workspace: Path) -> None:
    """A checkout with nothing to read falls back to the directory, so uv reports the real error."""
    (workspace / "empty").mkdir()
    assert s.editable_target(workspace, workspace / "empty", "empty") == str(workspace / "empty")


# --------------------------------------------------------------------------- #
# the extras axis: vocabulary from what each project DEFINES, not what it declares
# --------------------------------------------------------------------------- #


_EXTRAS_PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"

[project.optional-dependencies]
{extras}
"""


def _extras_project(root: Path, name: str, extras: list[str]) -> Path:
    """A project that declares NOTHING under [tool.aisland] — only plain extras."""
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f'{e} = ["some-package"]' for e in extras)
    (project / "pyproject.toml").write_text(_EXTRAS_PYPROJECT.format(name=name, extras=body))
    return project


@pytest.fixture
def extras_workspace(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _extras_project(tmp_path, "company_ai", ["dev", "local", "platform"])
    _extras_project(tmp_path, "sonair", ["dev", "tools"])
    return tmp_path


def test_the_extras_axis_needs_no_declaration(extras_workspace: Path) -> None:
    """The reason the axis exists: `[tool.aisland] extras` is not a thing to forget."""
    assert s.available(extras_workspace, EXTRAS) == ["dev", "local", "platform", "tools"]
    assert s.declared(extras_workspace / "sonair" / "pyproject.toml", EXTRAS) == ["dev", "tools"]


def test_each_project_gets_only_the_extras_it_defines(extras_workspace: Path) -> None:
    """One workspace-wide selection, intersected per project — `platform` must not reach sonair."""
    s.write_committed(extras_workspace, ["dev", "local", "platform", "tools"], EXTRAS)
    assert s.editable_target(extras_workspace, extras_workspace / "company_ai", "company_ai").endswith(
        "[dev,local,platform]"
    )
    assert s.editable_target(extras_workspace, extras_workspace / "sonair", "sonair").endswith("[dev,tools]")


def test_select_all_takes_everything_without_an_all_extra(extras_workspace: Path) -> None:
    """`--all` is computed from what the projects DEFINE; no project here defines `all`."""
    chosen, just_chosen = s.resolve_for_setup(extras_workspace, None, False, EXTRAS, select_all=True)
    assert chosen == ["dev", "local", "platform", "tools"]
    assert just_chosen is True
    assert s.read_committed(extras_workspace, EXTRAS) == ["dev", "local", "platform", "tools"]


def test_select_all_beats_a_recorded_selection(extras_workspace: Path) -> None:
    """Passing the flag re-records: a narrowed workspace must be wideable without editing TOML."""
    s.write_committed(extras_workspace, ["dev"], EXTRAS)
    chosen, _ = s.resolve_for_setup(extras_workspace, None, False, EXTRAS, select_all=True)
    assert chosen == ["dev", "local", "platform", "tools"]


def test_naming_extras_explicitly_records_them(extras_workspace: Path) -> None:
    chosen, _ = s.resolve_for_setup(extras_workspace, ["local", "platform"], False, EXTRAS)
    assert chosen == ["local", "platform"]
    assert s.read_committed(extras_workspace, EXTRAS) == ["local", "platform"]


def test_a_resolved_extras_selection_owns_dev(extras_workspace: Path) -> None:
    """Deselecting `dev` must actually drop it, or the prompt offers a choice it ignores."""
    s.write_committed(extras_workspace, ["local"], EXTRAS)
    assert s.editable_target(extras_workspace, extras_workspace / "company_ai", "company_ai").endswith("[local]")


def test_an_unresolved_extras_axis_keeps_the_historical_dev(extras_workspace: Path) -> None:
    """Unset is not empty: a workspace that never answered still gets `[dev]` as it always did."""
    assert s.effective(extras_workspace, EXTRAS) is None
    assert s.editable_target(extras_workspace, extras_workspace / "company_ai", "company_ai").endswith("[dev]")


def test_install_target_never_lists_dev_twice(extras_workspace: Path) -> None:
    target = s.install_target(extras_workspace / "company_ai", True, ["dev", "local"])
    assert target.endswith("[dev,local]")


def test_the_extras_axis_has_nothing_to_declare_undefined(extras_workspace: Path) -> None:
    """`require_defined_declarations` must not fault an axis whose names ARE the defined extras."""
    assert s.undefined_declarations(extras_workspace) == []
    s.require_defined_declarations(extras_workspace)


def test_a_self_referential_extra_is_not_offered(tmp_path: Path) -> None:
    """`all = ["me[dev,local]"]` re-exports the others; offering it too perturbs resolution.

    Observed 2026-10-20: company_ai_bridge with `[all,...]` resolved platform-client to a version
    whose URL dependency uv refuses; the same list without `all` resolved cleanly.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    project = tmp_path / "company_ai_bridge"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "company-ai-bridge"\nversion = "0.1.0"\n\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest"]\n'
        'local = ["torch"]\n'
        'all = ["company-ai-bridge[dev,local]"]\n'
    )
    assert s.available(tmp_path, EXTRAS) == ["dev", "local"]


def test_the_self_reference_test_is_structural_not_a_name_match(tmp_path: Path) -> None:
    """An extra called `all` holding real packages stays; a differently named one that
    re-exports the project is still dropped. Name matching would get both backwards."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    project = tmp_path / "foo"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "foo"\nversion = "0.1.0"\n\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest"]\n'
        'all = ["rich", "tqdm"]\n'
        'everything = ["foo[dev]"]\n'
    )
    assert s.available(tmp_path, EXTRAS) == ["all", "dev"]


def test_the_self_reference_test_normalises_the_distribution_name(tmp_path: Path) -> None:
    """`company_ai_bridge[dev]` names the same distribution as `company-ai-bridge` (PEP 503)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    project = tmp_path / "p"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "company-ai-bridge"\nversion = "0.1.0"\n\n'
        "[project.optional-dependencies]\n"
        'dev = ["pytest"]\n'
        'all = ["company_ai_bridge[dev]"]\n'
    )
    assert s.available(tmp_path, EXTRAS) == ["dev"]


def test_undefined_declarations_still_reports_every_defined_extra(extras_workspace: Path) -> None:
    """The `defines:` column is about what EXISTS, so it must not hide self-referential extras."""
    assert s._defined_extras(extras_workspace / "company_ai" / "pyproject.toml") == ["dev", "local", "platform"]
