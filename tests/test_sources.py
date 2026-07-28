"""Tests for per-project dependency sources (`aisland source`, aisland/sources.py).

Covers the four things a source change has to keep consistent:
- the source model (local / github / pypi) + its two configuration layers (committed file,
  per-machine .git/config);
- the `*.code-workspace` `folders` sync (comment-out in place, byte-lossless round trip);
- `apply_source`'s git/uv command sequence for each of the three kinds;
- the CI scaffolder's emission (pypi deps dropped, refs honoured, the `[ -d ]` guard on `local`
  deps only, and a detached dep surviving its own empty directory).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from aisland import sources as s
from aisland.services.jenkins import JenkinsService

# The real workspace file, condensed to the structure that matters: JSONC (trailing commas and
# comments in `settings`) around a strict-JSON `folders` array, with an out-of-tree entry last.
_WORKSPACE = """\
{
    "folders": [
        {
            "name": "acme",
            "path": "."
        },
        {
            "name": "loggair",
            "path": "loggair"
        },
        {
            "name": "confluid",
            "path": "confluid"
        },
        {
            "name": "vendorcli",
            "path": "vendorcli"
        },
        {
            "name": "logs",
            "path": "../logs"
        }
    ],
    "settings": {
        // A comment, and a trailing comma below: this file is JSONC, not JSON.
        "python.analysis.typeCheckingMode": "strict",
    },
}
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "acme.code-workspace").write_text(_WORKSPACE)
    return tmp_path


# --- the model ----------------------------------------------------------------------------


class TestProjectSource:
    @pytest.mark.parametrize("raw", ["pypi", "pypi:>=0.1.0", "local:main", "local:dev/main"])
    def test_spec_parse_round_trip(self, raw: str) -> None:
        assert s.ProjectSource.parse(raw).spec() == raw

    def test_bare_local_defaults_to_main(self) -> None:
        parsed = s.ProjectSource.parse("local")
        assert parsed.kind == "local" and parsed.ref == s.DEFAULT_REF

    def test_ref_may_contain_a_slash(self) -> None:
        assert s.ProjectSource.parse("local:dev/main").ref == "dev/main"

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown source kind"):
            s.ProjectSource.parse("svn:trunk")

    def test_requirement_uses_distribution_over_project(self) -> None:
        src = s.ProjectSource(kind="pypi", version=">=0.1.0", distribution="aisland-manager")
        assert src.requirement("aisland") == "aisland-manager>=0.1.0"

    def test_requirement_unpinned_when_no_version(self) -> None:
        assert s.ProjectSource(kind="pypi").requirement("loggair") == "loggair"

    def test_source_kinds_derive_from_the_literal(self) -> None:
        """No parallel list: the click Choice and the docs read this tuple."""
        assert s.SOURCE_KINDS == ("local", "github", "pypi")

    @pytest.mark.parametrize("raw", ["github", "github:main", "github:dev/main"])
    def test_github_carries_a_ref_like_local(self, raw: str) -> None:
        parsed = s.ProjectSource.parse(raw)
        assert parsed.kind == "github" and parsed.ref == (raw.partition(":")[2] or s.DEFAULT_REF)

    def test_github_spec_round_trips(self) -> None:
        assert s.ProjectSource.parse("github:dev/main").spec() == "github:dev/main"

    @pytest.mark.parametrize(
        "kind,expected",
        [("local", False), ("github", True), ("pypi", True)],
    )
    def test_detached_is_the_no_checkout_predicate(self, kind: str, expected: bool) -> None:
        """`github` and `pypi` empty the directory; only `local` keeps a working tree."""
        assert s.ProjectSource(kind=kind).detached is expected  # type: ignore[arg-type]

    def test_detached_kinds_and_ref_kinds_partition_the_literal(self) -> None:
        """Every kind is exactly one of ref-carrying/pypi and detached/checked-out."""
        assert set(s.REF_KINDS) | {"pypi"} == set(s.SOURCE_KINDS)
        assert set(s.DETACHED_KINDS) | {"local"} == set(s.SOURCE_KINDS)


class TestPipVcsUrl:
    def test_https_remote_gains_the_git_prefix(self) -> None:
        url = s.pip_vcs_url("https://github.com/Gearlux/loggair.git", "dev/main")
        assert url == "git+https://github.com/Gearlux/loggair.git@dev/main"

    def test_scp_style_ssh_remote_is_rewritten_to_a_url(self) -> None:
        """`git@host:org/repo` is scp syntax — pip cannot parse it; the colon must become a `/`."""
        url = s.pip_vcs_url("git@github.com:Gearlux/confluid.git", "main")
        assert url == "git+ssh://git@github.com/Gearlux/confluid.git@main"

    def test_non_github_remote_is_supported(self) -> None:
        """The URL comes from .gitmodules, so a Bitbucket-hosted submodule works too."""
        url = s.pip_vcs_url("https://bitbucket.example.com/scm/~u/vendorcli.git", "main")
        assert url == "git+https://bitbucket.example.com/scm/~u/vendorcli.git@main"


# --- committed layer ----------------------------------------------------------------------


class TestCommittedSources:
    def test_missing_file_is_unmanaged(self, tmp_path: Path) -> None:
        assert s.read_committed_sources(tmp_path) == {}

    def test_write_then_read_round_trip(self, tmp_path: Path) -> None:
        written = {
            "loggair": s.ProjectSource(kind="pypi", version=">=0.1.0", distribution="loggair"),
            "confluid": s.ProjectSource(kind="local", ref="dev/main", distribution="confluid"),
        }
        s.write_committed_sources(tmp_path, written)
        assert s.read_committed_sources(tmp_path) == written

    def test_written_file_is_valid_toml_with_provenance(self, tmp_path: Path) -> None:
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        text = (tmp_path / s.SOURCES_FILE).read_text()
        assert "aisland source set" in text, "generated files must name the command that regenerates them"
        assert tomllib.loads(text) == {"loggair": {"source": "pypi", "distribution": "loggair"}}

    def test_ref_omitted_for_pypi_and_version_omitted_for_git(self, tmp_path: Path) -> None:
        s.write_committed_sources(
            tmp_path,
            {
                "a": s.ProjectSource(kind="pypi", ref="ignored", distribution="a"),
                "b": s.ProjectSource(kind="local", ref="dev/main", version="ignored", distribution="b"),
            },
        )
        data = tomllib.loads((tmp_path / s.SOURCES_FILE).read_text())
        assert "ref" not in data["a"] and "version" not in data["b"]

    def test_unknown_kind_is_skipped_not_raised(self, tmp_path: Path) -> None:
        (tmp_path / s.SOURCES_FILE).write_text('[loggair]\nsource = "svn"\n[confluid]\nsource = "local"\n')
        assert set(s.read_committed_sources(tmp_path)) == {"confluid"}

    def test_malformed_toml_degrades_to_unmanaged(self, tmp_path: Path) -> None:
        (tmp_path / s.SOURCES_FILE).write_text("[loggair\nsource =\n")
        assert s.read_committed_sources(tmp_path) == {}


# --- layering -----------------------------------------------------------------------------


class TestEffectiveSource:
    @staticmethod
    def _fake_config(store: Dict[str, str]) -> Any:
        """Stand in for `git config --local` against an in-memory store."""

        def _run(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
            key = cmd[-1] if "--get" in cmd or "--unset" in cmd else cmd[-2]
            if "--get-regexp" in cmd:
                out = "".join(f"{k} {v}\n" for k, v in store.items())
                return subprocess.CompletedProcess(cmd, 0, out, "")
            if "--unset" in cmd:
                store.pop(key, None)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if "--get" in cmd:
                if key not in store:
                    return subprocess.CompletedProcess(cmd, 1, "", "")
                return subprocess.CompletedProcess(cmd, 0, store[key] + "\n", "")
            store[key] = cmd[-1]
            return subprocess.CompletedProcess(cmd, 0, "", "")

        return _run

    def test_committed_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({}))
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        assert s.effective_source(tmp_path, "loggair") == s.ProjectSource(kind="local", ref="dev/main")

    def test_override_wins_over_committed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({"aisland.source.loggair": "pypi"}))
        s.write_committed_sources(
            tmp_path, {"loggair": s.ProjectSource(kind="local", ref="main", distribution="loggair")}
        )
        effective = s.effective_source(tmp_path, "loggair")
        assert effective is not None and effective.kind == "pypi"

    def test_override_inherits_committed_distribution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A compact override carries no dist name; without inheritance a pypi switch would
        install the DIRECTORY name (`aisland`) instead of `aisland-manager`."""
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({"aisland.source.aisland": "pypi"}))
        s.write_committed_sources(tmp_path, {"aisland": s.ProjectSource(kind="local", distribution="aisland-manager")})
        effective = s.effective_source(tmp_path, "aisland")
        assert effective is not None and effective.requirement("aisland") == "aisland-manager"

    def test_unmanaged_project_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({}))
        assert s.effective_source(tmp_path, "confluid") is None

    def test_malformed_override_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({"aisland.source.loggair": "svn:trunk"}))
        assert s.local_override(tmp_path, "loggair") is None


class TestSourceForDependency:
    def test_matches_project_key(self) -> None:
        committed = {"loggair": s.ProjectSource(kind="pypi")}
        assert s.source_for_dependency(committed, "loggair") is committed["loggair"]

    def test_matches_explicit_distribution(self) -> None:
        committed = {"aisland": s.ProjectSource(kind="pypi", distribution="aisland-manager")}
        assert s.source_for_dependency(committed, "aisland-manager") is committed["aisland"]

    def test_matches_dash_stripped_form(self) -> None:
        committed = {"recordstream": s.ProjectSource(kind="local")}
        assert s.source_for_dependency(committed, "record-stream") is committed["recordstream"]

    def test_unknown_dependency_is_none(self) -> None:
        assert s.source_for_dependency({"loggair": s.ProjectSource(kind="pypi")}, "numpy") is None


# --- *.code-workspace folders --------------------------------------------------------------


class TestSyncCodeWorkspace:
    def test_pypi_comments_the_entry_out_in_place(self, workspace: Path) -> None:
        s.sync_code_workspace(workspace, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        text = (workspace / "acme.code-workspace").read_text()
        assert '        //     "name": "loggair",' in text
        assert '            "name": "loggair",' not in text
        # Position is preserved: still between acme and confluid.
        assert text.index("acme") < text.index("// aisland source:") < text.index('"confluid"')

    def test_round_trip_is_byte_identical(self, workspace: Path) -> None:
        """Flipping to pypi and back must leave `git diff` clean — the whole reason entries are
        commented out rather than deleted."""
        path = workspace / "acme.code-workspace"
        original = path.read_text()
        s.sync_code_workspace(workspace, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        assert path.read_text() != original
        s.sync_code_workspace(workspace, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        assert path.read_text() == original

    def test_jsonc_outside_folders_is_preserved(self, workspace: Path) -> None:
        s.sync_code_workspace(workspace, {"loggair": s.ProjectSource(kind="pypi")})
        text = (workspace / "acme.code-workspace").read_text()
        assert "// A comment, and a trailing comma below" in text
        assert '"python.analysis.typeCheckingMode": "strict",' in text

    def test_unmanaged_entries_are_untouched(self, workspace: Path) -> None:
        """`vendorcli` and friends are deactivated by a different mechanism; this one must not
        touch entries it does not own."""
        s.sync_code_workspace(workspace, {"loggair": s.ProjectSource(kind="pypi")})
        text = (workspace / "acme.code-workspace").read_text()
        assert '            "name": "vendorcli",' in text

    def test_trailing_comma_normalised_when_last_entry_disabled(self, workspace: Path) -> None:
        s.sync_code_workspace(workspace, {"logs": s.ProjectSource(kind="pypi")})
        text = (workspace / "acme.code-workspace").read_text()
        # `vendorcli` is now the last enabled entry, so its closing brace loses the separator.
        vendorcli_close = text.index('"path": "vendorcli"')
        assert text[vendorcli_close:].lstrip().splitlines()[1].strip() == "}"

    def test_missing_entry_is_inserted_before_out_of_tree_paths(self, workspace: Path) -> None:
        s.sync_code_workspace(workspace, {"brandnew": s.ProjectSource(kind="local")})
        text = (workspace / "acme.code-workspace").read_text()
        assert text.index('"brandnew"') < text.index('"../logs"')

    def test_pypi_project_with_no_entry_is_not_inserted(self, workspace: Path) -> None:
        s.sync_code_workspace(workspace, {"brandnew": s.ProjectSource(kind="pypi")})
        assert "brandnew" not in (workspace / "acme.code-workspace").read_text()

    def test_no_workspace_file_is_a_no_op(self, tmp_path: Path) -> None:
        assert s.sync_code_workspace(tmp_path, {"loggair": s.ProjectSource(kind="pypi")}) is None

    def test_unchanged_file_is_not_rewritten(self, workspace: Path) -> None:
        assert s.sync_code_workspace(workspace, {"loggair": s.ProjectSource(kind="local")}) is None


# --- applying -----------------------------------------------------------------------------


class _Recorder:
    """Records every subprocess invocation and reports success unless a prefix is set to fail."""

    def __init__(self, fail_on: str = "") -> None:
        self.calls: List[List[str]] = []
        self.fail_on = fail_on

    def check_call(self, cmd: List[str], **kwargs: Any) -> int:
        self.calls.append(cmd)
        if self.fail_on and self.fail_on in " ".join(cmd):
            raise subprocess.CalledProcessError(1, cmd)
        return 0

    def flat(self) -> List[str]:
        return [" ".join(c) for c in self.calls]


def _fake_capture(*, status: str, head: str, recorded: str) -> Callable[[List[str], Path], str]:
    """Stub for :func:`aisland.sources._capture`, answering per probe.

    `_capture` shells out via ``subprocess.run``, which `_Recorder` (``check_call``) does not
    intercept — so the remedy probes are stubbed at the function instead of the subprocess.
    """

    def capture(cmd: List[str], cwd: Path) -> str:
        if cmd[:2] == ["git", "status"]:
            return status
        if cmd[:2] == ["git", "rev-parse"]:
            return recorded if ":" in cmd[2] else head  # `HEAD:<rel>` is the superproject's gitlink
        return ""

    return capture


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(s.subprocess, "check_call", rec.check_call)
    monkeypatch.setattr(s, "_set_local_submodule_active", lambda *a, **k: None)
    monkeypatch.setattr(s, "submodule_active_map", lambda root: [("loggair", "loggair", True)])
    monkeypatch.setattr(s, "remote_url", lambda root, project: "https://github.com/Gearlux/loggair.git")
    return rec


class TestApplySource:
    def test_git_initializes_switches_and_installs_editable(self, tmp_path: Path, recorder: _Recorder) -> None:
        source = s.ProjectSource(kind="local", ref="dev/main")
        assert s.apply_source(tmp_path, "loggair", source, "/venv/bin/uv") is True
        flat = recorder.flat()
        assert flat[0] == "git submodule update --init loggair"
        assert "git switch dev/main" in flat
        assert flat[-1] == f"/venv/bin/uv pip install -e {tmp_path / 'loggair'}"

    def test_git_skips_init_when_already_checked_out(self, tmp_path: Path, recorder: _Recorder) -> None:
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: ../.git/modules/loggair\n")
        s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="local"), "/venv/bin/uv")
        assert not any("submodule update" in c for c in recorder.flat())

    def test_failed_switch_reports_and_leaves_venv_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
    ) -> None:
        """A dirty submodule must NOT be force-switched, and the editable install must not run
        against a checkout that is on the wrong branch."""
        recorder.fail_on = "git switch"
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="local", ref="dev/main"), "uv") is False
        assert not any("pip install" in c for c in recorder.flat())

    def test_pypi_deinits_then_uninstalls_then_installs(self, tmp_path: Path, recorder: _Recorder) -> None:
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: ../.git/modules/loggair\n")
        source = s.ProjectSource(kind="pypi", version=">=0.1.0", distribution="loggair")
        assert s.apply_source(tmp_path, "loggair", source, "/venv/bin/uv") is True
        assert recorder.flat() == [
            "git submodule deinit loggair",
            "/venv/bin/uv pip uninstall loggair",
            "/venv/bin/uv pip install -U loggair>=0.1.0",
        ]

    def test_pypi_deinit_is_not_forced(self, tmp_path: Path, recorder: _Recorder) -> None:
        """`-f` would silently discard uncommitted work in the submodule."""
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="pypi", distribution="loggair"), "uv")
        assert "-f" not in recorder.calls[0]

    def test_refused_deinit_aborts_before_touching_the_venv(self, tmp_path: Path, recorder: _Recorder) -> None:
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        recorder.fail_on = "submodule deinit"
        source = s.ProjectSource(kind="pypi", distribution="loggair")
        assert s.apply_source(tmp_path, "loggair", source, "uv") is False
        assert not any("pip" in c for c in recorder.flat())

    def test_refused_deinit_names_the_stale_pointer_not_the_clean_submodule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The blocker's fix lives in the SUPERPROJECT when only the gitlink is stale.

        A submodule committed-but-not-pointer-bumped has a spotless work tree, so the old
        unconditional "commit or stash the changes in <sub>" sent the operator to a clean tree.
        """
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        recorder.fail_on = "submodule deinit"
        monkeypatch.setattr(s, "_capture", _fake_capture(status="", head="8a6c24e" * 5, recorded="f3bba7c" * 5))
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="pypi", distribution="loggair"), "uv") is False
        err = capsys.readouterr().err
        assert "records f3bba7c for loggair but the checkout is at 8a6c24e" in err
        assert "git add loggair && git commit -- loggair" in err
        assert "stash" not in err

    def test_refused_deinit_names_the_dirty_tree_when_the_pointer_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        recorder.fail_on = "submodule deinit"
        sha = "8a6c24e" * 5
        monkeypatch.setattr(s, "_capture", _fake_capture(status=" M core.py", head=sha, recorded=sha))
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="pypi", distribution="loggair"), "uv") is False
        err = capsys.readouterr().err
        assert "Commit or stash the changes in loggair." in err
        assert "records" not in err

    def test_refused_deinit_reports_both_blockers_when_both_apply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        recorder.fail_on = "submodule deinit"
        monkeypatch.setattr(s, "_capture", _fake_capture(status=" M core.py", head="a" * 40, recorded="b" * 40))
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="pypi", distribution="loggair"), "uv") is False
        err = capsys.readouterr().err
        assert "Commit or stash the changes in loggair." in err
        assert "commit the pointer" in err

    def test_refused_deinit_stays_generic_when_the_probes_cannot_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unanswerable probe must not assert a blocker it did not verify."""
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        recorder.fail_on = "submodule deinit"
        monkeypatch.setattr(s, "_capture", _fake_capture(status="", head="", recorded=""))
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="pypi", distribution="loggair"), "uv") is False
        err = capsys.readouterr().err
        assert "Resolve the local modifications in the checkout or the superproject" in err

    def test_capture_degrades_to_empty_instead_of_raising(self, tmp_path: Path) -> None:
        """`_capture` backs the remedy probes — a failing git must not abort the switch."""
        assert s._capture(["git", "rev-parse", "HEAD"], tmp_path) == ""
        assert s._capture(["definitely-not-a-binary"], tmp_path) == ""

    def test_pypi_reads_distribution_from_pyproject_when_unrecorded(self, tmp_path: Path, recorder: _Recorder) -> None:
        """The name must be captured while the directory is still readable — afterwards it is gone."""
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / "pyproject.toml").write_text('[project]\nname = "loggair-dist"\n')
        s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="pypi"), "uv")
        assert "uv pip install -U loggair-dist" in recorder.flat()


class TestApplyGithubSource:
    def test_deinits_then_installs_from_the_remote_at_ref(self, tmp_path: Path, recorder: _Recorder) -> None:
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        source = s.ProjectSource(kind="github", ref="dev/main", distribution="loggair")
        assert s.apply_source(tmp_path, "loggair", source, "/venv/bin/uv") is True
        assert recorder.flat() == [
            "git submodule deinit loggair",
            "/venv/bin/uv pip uninstall loggair",
            "/venv/bin/uv pip install -U git+https://github.com/Gearlux/loggair.git@dev/main",
        ]

    def test_never_installs_editable(self, tmp_path: Path, recorder: _Recorder) -> None:
        """A github source has no working tree — an `-e <dir>` would point at an empty path."""
        s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="github"), "uv")
        assert not any(" -e " in c for c in recorder.flat())

    def test_uninstalls_first_so_a_ref_change_is_not_skipped(self, tmp_path: Path, recorder: _Recorder) -> None:
        """github:main -> github:dev/main reports the SAME version; without the uninstall uv
        would treat the requirement as already satisfied and leave the old ref installed."""
        s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="github", ref="dev/main"), "uv")
        flat = recorder.flat()
        assert flat.index("uv pip uninstall loggair") < flat.index(
            "uv pip install -U git+https://github.com/Gearlux/loggair.git@dev/main"
        )

    def test_refused_deinit_aborts_before_touching_the_venv(self, tmp_path: Path, recorder: _Recorder) -> None:
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        recorder.fail_on = "submodule deinit"
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="github"), "uv") is False
        assert not any("pip" in c for c in recorder.flat())

    def test_missing_remote_url_is_refused_not_guessed(
        self, tmp_path: Path, recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a .gitmodules URL there is nothing to install from — refuse rather than
        fabricate a github.com/Gearlux/<name> that may not exist."""
        monkeypatch.setattr(s, "remote_url", lambda root, project: None)
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="github"), "uv") is False
        assert recorder.flat() == []


# --- CI scaffolding -----------------------------------------------------------------------


def _project(root: Path, name: str, deps: List[str]) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    dep_list = ", ".join(f'"{d}"' for d in deps)
    (root / name / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\ndependencies = [{dep_list}]\n'
    )
    pkg = root / name / name
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").touch()


@pytest.fixture
def ci_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A two-project workspace (confluid -> loggair) with both declared as Gearlux submodules."""
    _project(tmp_path, "loggair", [])
    _project(tmp_path, "confluid", ["loggair>=0.1.0"])
    (tmp_path / ".gitmodules").write_text(
        '[submodule "loggair"]\n\tpath = loggair\n\turl = git@github.com:Gearlux/loggair.git\n'
        '[submodule "confluid"]\n\tpath = confluid\n\turl = git@github.com:Gearlux/confluid.git\n'
    )
    from aisland import manager

    monkeypatch.setattr(manager, "get_workspace_root", lambda: tmp_path)
    # Keep the scaffolder off the network: `gh api` would otherwise decide the token step.
    monkeypatch.setattr("aisland.services.jenkins.is_private_gearlux_repo", lambda name: False)
    return tmp_path


def _generated(root: Path, project: str) -> Dict[str, str]:
    base = root / project
    return {
        "Jenkinsfile": (base / "Jenkinsfile").read_text(),
        "Jenkinsfile.local": (base / "Jenkinsfile.local").read_text(),
        "ci.yml": (base / ".github" / "workflows" / "ci.yml").read_text(),
    }


class TestScaffoldHonoursSources:
    def test_unmanaged_dep_keeps_the_historical_main_ref(self, ci_workspace: Path) -> None:
        """Adding this feature must be a no-op until a source is actually set."""
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        files = _generated(ci_workspace, "confluid")
        assert "git+https://github.com/Gearlux/loggair.git@main" in files["Jenkinsfile"]
        assert "git+https://github.com/Gearlux/loggair.git@main" in files["ci.yml"]

    def test_ci_ignores_the_configured_ref(self, ci_workspace: Path) -> None:
        """`--ref` is a DEVELOPMENT pin. Letting it decide what a runner clones would publish a
        personal choice to every consumer's CI and churn tracked files on every ref switch."""
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        files = _generated(ci_workspace, "confluid")
        assert "git+https://github.com/Gearlux/loggair.git@main" in files["Jenkinsfile"]
        assert "git+https://github.com/Gearlux/loggair.git@main" in files["ci.yml"]
        assert "dev/main" not in files["Jenkinsfile"] and "dev/main" not in files["ci.yml"]

    def test_a_ref_change_rewrites_no_ci_file_at_all(self, ci_workspace: Path) -> None:
        """The payoff: `aisland source set --ref` regenerates nothing, so it can no longer drag
        17 projects' committed files (and their unrelated template drift) along with it."""
        generated = []
        for ref in ("main", "dev/main", "some/feature"):
            s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="local", ref=ref)})
            JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
            generated.append(_generated(ci_workspace, "confluid"))
        assert generated[0] == generated[1] == generated[2]

    def test_pypi_dep_is_dropped_from_the_runner_files(self, ci_workspace: Path) -> None:
        """A published dep resolves via `.[dev]` from PyPI; a git URL would pin it instead.
        Jenkinsfile.local is deliberately NOT included — see the identical-line test below."""
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        files = _generated(ci_workspace, "confluid")
        for name in ("Jenkinsfile", "ci.yml"):
            assert "Gearlux/loggair.git" not in files[name], f"{name} still installs loggair from git"
            assert "WORKSPACE_ROOT}/loggair" not in files[name], f"{name} still installs from the workspace"

    def test_pypi_dep_still_installed_via_dev_extra(self, ci_workspace: Path) -> None:
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "uv pip install -e .[dev]" in _generated(ci_workspace, "confluid")["Jenkinsfile"]

    def test_pypi_dep_dropped_even_when_still_checked_out(self, ci_workspace: Path) -> None:
        """The committed registry is authoritative: a machine with a `git` override still has the
        directory present, and discovery would otherwise classify the dep as internal."""
        assert (ci_workspace / "loggair" / "pyproject.toml").exists()
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "Gearlux/loggair.git" not in _generated(ci_workspace, "confluid")["Jenkinsfile"]

    def test_jenkinsfile_local_guards_the_editable_install(self, ci_workspace: Path) -> None:
        """A per-machine pypi override empties the directory this COMMITTED file names, so the
        editable install must fall back to the committed git ref instead of failing."""
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        local = _generated(ci_workspace, "confluid")["Jenkinsfile.local"]
        assert "if [ -d '${env.WORKSPACE_ROOT}/loggair' ]; then" in local
        assert "else ${VENV_BIN}/uv pip install --no-deps git+https://github.com/Gearlux/loggair.git@main; fi" in local

    def test_guard_always_falls_back_to_the_default_ref(self, ci_workspace: Path) -> None:
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "loggair.git@main; fi" in _generated(ci_workspace, "confluid")["Jenkinsfile.local"]

    def test_jenkinsfile_local_is_identical_for_every_source_kind(self, ci_workspace: Path) -> None:
        """The whole point of the `[ -d ]` guard: it asks at BUILD time the question the config
        answers, so this file is a function of the dependency LIST alone. No `aisland source`
        change of any kind can rewrite it — including flipping to/from pypi."""
        rendered = []
        for source in (
            s.ProjectSource(kind="local", ref="dev/main"),
            s.ProjectSource(kind="github", ref="some/branch"),
            s.ProjectSource(kind="pypi", distribution="loggair"),
        ):
            s.write_committed_sources(ci_workspace, {"loggair": source})
            JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
            rendered.append(_generated(ci_workspace, "confluid")["Jenkinsfile.local"])
        assert rendered[0] == rendered[1] == rendered[2]

    def test_github_dep_keeps_the_guard_in_jenkinsfile_local(self, ci_workspace: Path) -> None:
        """Uniformity is the point — the guard simply takes its else-branch on a machine where
        the directory is empty, which is exactly what a detached source leaves behind."""
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="github", ref="dev/main")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        local = _generated(ci_workspace, "confluid")["Jenkinsfile.local"]
        assert "if [ -d '${env.WORKSPACE_ROOT}/loggair' ]; then" in local
        assert "else ${VENV_BIN}/uv pip install --no-deps git+https://github.com/Gearlux/loggair.git@main; fi" in local

    def test_pypi_dep_keeps_the_guard_in_jenkinsfile_local(self, ci_workspace: Path) -> None:
        """A pypi dep is dropped from the runner files but NOT here: the local pipeline builds
        against `main` so it catches breakage before a release, and keeping the line uniform is
        what makes this file config-independent."""
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        local = _generated(ci_workspace, "confluid")["Jenkinsfile.local"]
        assert "if [ -d '${env.WORKSPACE_ROOT}/loggair' ]; then" in local
        assert "loggair.git@main; fi" in local

    def test_github_dep_survives_its_own_empty_directory(self, ci_workspace: Path) -> None:
        """The regression this class exists for. A detached project has NO pyproject.toml, so
        `discover_gearlux_projects` cannot read its name from disk — without the registry
        fallback the dep vanished from the internal set entirely and CI silently resolved the
        PyPI release instead of the pinned ref (or failed, for an unpublished project)."""
        s.write_committed_sources(
            ci_workspace, {"loggair": s.ProjectSource(kind="github", ref="dev/main", distribution="loggair")}
        )
        # Detached == the directory is gone, exactly as `apply_source` leaves it.
        shutil.rmtree(ci_workspace / "loggair")

        from aisland.services.jenkins import discover_gearlux_projects

        assert "loggair" in discover_gearlux_projects(ci_workspace)
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "git+https://github.com/Gearlux/loggair.git@main" in _generated(ci_workspace, "confluid")["ci.yml"]

    def test_pypi_dep_still_dropped_when_its_directory_is_gone(self, ci_workspace: Path) -> None:
        """The registry fallback must not resurrect a pypi dep into the git-URL loop."""
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        shutil.rmtree(ci_workspace / "loggair")
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "Gearlux/loggair.git" not in _generated(ci_workspace, "confluid")["ci.yml"]


class TestPublishednessChanged:
    """CI reads exactly one bit of the registry, so only that bit may trigger a regeneration."""

    def test_ref_switch_is_not_a_change(self) -> None:
        before = s.ProjectSource(kind="local", ref="main")
        after = s.ProjectSource(kind="local", ref="dev/main")
        assert s._publishedness_changed(before, after) is False

    def test_local_to_github_is_not_a_change(self) -> None:
        """Both are git-backed; a runner clones either identically."""
        before = s.ProjectSource(kind="local", ref="dev/main")
        after = s.ProjectSource(kind="github", ref="dev/main")
        assert s._publishedness_changed(before, after) is False

    @pytest.mark.parametrize("other", ["local", "github"])
    def test_crossing_the_pypi_boundary_is_a_change(self, other: str) -> None:
        git_backed = s.ProjectSource(kind=other)  # type: ignore[arg-type]
        published = s.ProjectSource(kind="pypi")
        assert s._publishedness_changed(git_backed, published) is True
        assert s._publishedness_changed(published, git_backed) is True

    def test_unmanaged_reads_as_not_published(self) -> None:
        assert s._publishedness_changed(None, s.ProjectSource(kind="local")) is False
        assert s._publishedness_changed(None, s.ProjectSource(kind="pypi")) is True


class TestRemoteRefExists:
    @staticmethod
    def _probe(monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str) -> None:
        monkeypatch.setattr(s, "remote_url", lambda root, project: "https://example.com/x.git")
        monkeypatch.setattr(
            s.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode, stdout, ""),
        )

    def test_existing_ref_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._probe(monkeypatch, 0, "abc123\trefs/heads/dev/main\n")
        assert s._remote_ref_exists(tmp_path, "loggair", "dev/main") is True

    def test_missing_ref_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exit 0 with no output means the remote answered and has no such ref."""
        self._probe(monkeypatch, 0, "")
        assert s._remote_ref_exists(tmp_path, "loggair", "nope") is False

    def test_unreachable_remote_is_inconclusive_not_a_rejection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same rule as the PyPI probe: never block a machine on a check it cannot perform."""
        self._probe(monkeypatch, 128, "")
        assert s._remote_ref_exists(tmp_path, "loggair", "dev/main") is True

    def test_no_declared_url_skips_the_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "remote_url", lambda root, project: None)
        assert s._remote_ref_exists(tmp_path, "loggair", "dev/main") is True


class TestPypiProvenance:
    @staticmethod
    def _payload(monkeypatch: pytest.MonkeyPatch, info: Dict[str, Any]) -> None:
        import contextlib
        import io
        import urllib.request

        @contextlib.contextmanager
        def _open(url: str, timeout: int = 5) -> Any:
            yield io.BytesIO(json.dumps({"info": info}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", _open)

    def test_gearlux_metadata_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._payload(
            monkeypatch,
            {"version": "0.1.0", "project_urls": {"Repository": "https://github.com/Gearlux/loggair.git"}},
        )
        assert s._pypi_provenance("loggair") == (True, "0.1.0")

    def test_name_owned_by_another_project_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PyPI names are global: a bare existence check would install a stranger's package into
        the shared venv."""
        self._payload(
            monkeypatch,
            {"version": "9.9.9", "project_urls": {"Homepage": "https://example.com/somebody-else"}},
        )
        ok, reason = s._pypi_provenance("loggair")
        assert ok is False and "owned by a different project" in reason

    def test_home_page_also_counts_as_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._payload(monkeypatch, {"version": "1.0", "home_page": "https://github.com/Gearlux/loggair"})
        assert s._pypi_provenance("loggair")[0] is True

    def test_404_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.request

        def _raise(url: str, timeout: int = 5) -> Any:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        ok, reason = s._pypi_provenance("liquifai")
        assert ok is False and "not published" in reason

    def test_unreachable_index_is_inconclusive_not_a_rejection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An offline machine must not be blocked by a check it cannot perform."""
        import urllib.error
        import urllib.request

        def _raise(url: str, timeout: int = 5) -> Any:
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        assert s._pypi_provenance("loggair")[0] is None


class TestSetProjectSourceRollback:
    def test_failed_apply_reverts_the_registry(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A registry claiming a state the working tree does not have would be retried by every
        subsequent `aisland setup`."""
        (ci_workspace / ".venv" / "bin").mkdir(parents=True)
        (ci_workspace / ".venv" / "bin" / "uv").touch(mode=0o755)
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        monkeypatch.setattr(s, "apply_source", lambda *a, **k: False)
        monkeypatch.setattr(s, "_pypi_provenance", lambda dist: (True, "0.1.0"))

        assert s.set_project_source("loggair", "pypi", workspace_root=ci_workspace) == 1
        assert not (ci_workspace / s.SOURCES_FILE).exists(), "a registry created by the failed run must be removed"

    def test_failed_apply_restores_a_pre_existing_entry(
        self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (ci_workspace / ".venv" / "bin").mkdir(parents=True)
        (ci_workspace / ".venv" / "bin" / "uv").touch(mode=0o755)
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        monkeypatch.setattr(s, "apply_source", lambda *a, **k: False)
        monkeypatch.setattr(s, "_pypi_provenance", lambda dist: (True, "0.1.0"))
        before = {"loggair": s.ProjectSource(kind="local", ref="dev/main", distribution="loggair")}
        s.write_committed_sources(ci_workspace, before)

        assert s.set_project_source("loggair", "pypi", workspace_root=ci_workspace) == 1
        assert s.read_committed_sources(ci_workspace) == before

    def test_rejected_pypi_name_writes_nothing(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "_pypi_provenance", lambda dist: (False, "is not published on PyPI"))
        assert s.set_project_source("loggair", "pypi", workspace_root=ci_workspace) == 1
        assert not (ci_workspace / s.SOURCES_FILE).exists()

    def test_unknown_project_writes_nothing(self, ci_workspace: Path) -> None:
        assert s.set_project_source("nosuchproj", "pypi", workspace_root=ci_workspace) == 1
        assert not (ci_workspace / s.SOURCES_FILE).exists()


class TestUnsetProjectSource:
    def test_removes_the_committed_entry(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi")})
        assert s.unset_project_source("loggair", scaffold=False, workspace_root=ci_workspace) == 0
        assert s.read_committed_sources(ci_workspace) == {}

    def test_restores_the_historical_main_ref_in_ci(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi")})
        s.unset_project_source("loggair", workspace_root=ci_workspace)
        assert "Gearlux/loggair.git@main" in _generated(ci_workspace, "confluid")["Jenkinsfile"]

    def test_re_enables_the_editor_folder_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A leftover `// aisland source:` block would point at configuration that is now gone."""
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        (tmp_path / "acme.code-workspace").write_text(_WORKSPACE)
        original = (tmp_path / "acme.code-workspace").read_text()
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="pypi")})
        s.sync_code_workspace(tmp_path, s.read_committed_sources(tmp_path))
        s.unset_project_source("loggair", scaffold=False, workspace_root=tmp_path)
        assert (tmp_path / "acme.code-workspace").read_text() == original

    def test_already_unmanaged_is_a_no_op(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        assert s.unset_project_source("loggair", scaffold=False, workspace_root=tmp_path) == 0
        assert not (tmp_path / s.SOURCES_FILE).exists()

    def test_machine_only_keeps_the_committed_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cleared: List[Any] = []
        monkeypatch.setattr(s, "local_override", lambda root, name: s.ProjectSource(kind="pypi"))
        monkeypatch.setattr(s, "set_local_override", lambda root, name, src: cleared.append((name, src)))
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        s.unset_project_source("loggair", machine_only=True, workspace_root=tmp_path)
        assert cleared == [("loggair", None)]
        assert set(s.read_committed_sources(tmp_path)) == {"loggair"}


class TestDependentProjects:
    def test_direct_and_transitive_dependents(self, ci_workspace: Path) -> None:
        _project(ci_workspace, "liquifai", ["confluid>=0.1.0"])
        assert s.dependent_projects(ci_workspace, "loggair", "loggair") == ["confluid", "liquifai"]

    def test_the_project_itself_is_not_a_dependent(self, ci_workspace: Path) -> None:
        assert "loggair" not in s.dependent_projects(ci_workspace, "loggair", "loggair")

    def test_leaf_project_has_no_dependents(self, ci_workspace: Path) -> None:
        assert s.dependent_projects(ci_workspace, "confluid", "confluid") == []


class TestReconcileSkipsPypiProjects:
    def test_pypi_source_deinits_a_committed_active_submodule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this, the next `aisland setup` would re-init a directory the source says is empty."""
        from aisland import manager

        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: x\n")
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        monkeypatch.setattr(manager, "submodule_active_map", lambda root: [("loggair", "loggair", True)])
        monkeypatch.setattr(manager, "_local_submodule_active", lambda root, name: None)
        monkeypatch.setattr(s, "_overridden_projects", lambda root: [])
        monkeypatch.setattr(s, "local_override", lambda root, name: None)

        calls: List[List[str]] = []
        monkeypatch.setattr(manager.subprocess, "check_call", lambda cmd, **kw: calls.append(cmd))
        manager.reconcile_submodules(tmp_path)
        assert calls == [["git", "submodule", "deinit", "loggair"]]
