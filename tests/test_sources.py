"""Tests for per-project dependency sources (`aisland workspace configure source`, aisland/sources.py).

Covers the four things a source change has to keep consistent:
- the source model (local / github / pypi) + its two configuration layers (committed file,
  per-machine .git/config);
- `apply_source`'s git/uv command sequence for each of the three kinds;
- the CI scaffolder's emission (pypi deps dropped, refs honoured, the uniform
  `[ -f <dep>/pyproject.toml ]` guard — both its text and its BEHAVIOUR against a real deinit'd
  directory — and a detached dep surviving its own empty directory).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, List, Set

import click
import pytest

from aisland import sources as s
from aisland import workspace_file
from aisland.config import CONFIG_FILE
from aisland.services.jenkins import JenkinsService

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
        src = s.ProjectSource(kind="pypi", version=">=0.1.0", distribution="acme-tools")
        assert src.requirement("acme") == "acme-tools>=0.1.0"

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
    """The committed layer lives in workspace.toml: each project's `source`, its `version` (pypi), its `branch`
    (local/github: the project's own branch) and a `distribution` only where it differs from the project's name."""

    @staticmethod
    def _file(root: Path) -> Dict[str, Any]:
        return tomllib.loads((root / "workspace.toml").read_text())

    def test_missing_file_is_unmanaged(self, tmp_path: Path) -> None:
        assert s.read_committed_sources(tmp_path) == {}

    def test_write_then_read_round_trip(self, tmp_path: Path) -> None:
        written = {
            "loggair": s.ProjectSource(kind="pypi", version=">=0.1.0"),
            "acme": s.ProjectSource(kind="local", ref="dev/main", distribution="acme-tools"),
        }
        s.write_committed_sources(tmp_path, written)
        assert s.read_committed_sources(tmp_path) == written

    def test_the_settings_go_into_each_projects_table_and_repeat_nothing(self, tmp_path: Path) -> None:
        s.write_committed_sources(
            tmp_path,
            {
                "loggair": s.ProjectSource(kind="pypi", distribution="loggair"),
                "confluid": s.ProjectSource(kind="local", ref="dev/main", distribution="confluid"),
            },
        )
        assert self._file(tmp_path)["project"] == {
            "confluid": {"branch": "dev/main", "source": "local"},
            "loggair": {"source": "pypi"},
        }

    def test_a_project_keeps_its_url_branch_and_other_settings(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            '[project."loggair"]\nurl = "https://example.com/loggair.git"\nbranch = "dev/main"\nactive = false\n'
        )
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="pypi")})
        assert self._file(tmp_path)["project"]["loggair"] == {
            "url": "https://example.com/loggair.git",
            "branch": "dev/main",
            "active": False,
            "source": "pypi",
        }

    def test_unsetting_removes_only_the_source_settings(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text(
            '[project."loggair"]\nurl = "u"\nbranch = "dev/main"\nsource = "pypi"\nversion = ">=1"\n'
            'distribution = "loggair-x"\n'
        )
        s.write_committed_sources(tmp_path, {})
        assert self._file(tmp_path)["project"]["loggair"] == {"url": "u", "branch": "dev/main"}

    def test_version_omitted_for_git_and_the_branch_left_alone_for_pypi(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text('[project."a"]\nurl = "u"\nbranch = "dev/main"\n')
        s.write_committed_sources(
            tmp_path,
            {
                "a": s.ProjectSource(kind="pypi", ref="ignored"),
                "b": s.ProjectSource(kind="local", ref="feature", version="ignored"),
            },
        )
        data = self._file(tmp_path)["project"]
        assert data["a"]["branch"] == "dev/main" and "version" not in data["b"] and data["b"]["branch"] == "feature"

    def test_writing_sources_leaves_the_workspace_section_alone(self, tmp_path: Path) -> None:
        (tmp_path / "workspace.toml").write_text('[workspace]\nframework = ["torch"]\n')
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="pypi")})
        assert self._file(tmp_path)["workspace"] == {"framework": ["torch"]}

    def test_an_unknown_kind_is_skipped_with_a_warning_naming_its_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "workspace.toml").write_text(
            '[project."loggair"]\nsource = "svn"\n\n[project."confluid"]\nsource = "local"\n'
        )
        assert set(s.read_committed_sources(tmp_path)) == {"confluid"}
        assert f"{tmp_path / 'workspace.toml'}:1: project 'loggair' has source 'svn'" in capsys.readouterr().out


# --- layering -----------------------------------------------------------------------------


class TestEffectiveSource:
    """The per-machine layer: `aisland.source.<project>.source`, `.branch` and `.version` in .git/config."""

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
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({"aisland.source.loggair.source": "pypi"}))
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="local", ref="main")})
        effective = s.effective_source(tmp_path, "loggair")
        assert effective is not None and effective.kind == "pypi"

    def test_an_override_names_its_own_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = {"aisland.source.loggair.source": "local", "aisland.source.loggair.branch": "feature"}
        monkeypatch.setattr(s.subprocess, "run", self._fake_config(store))
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        assert s.effective_source(tmp_path, "loggair") == s.ProjectSource(kind="local", ref="feature")

    def test_an_override_without_a_branch_takes_the_projects_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({"aisland.source.loggair.source": "github"}))
        (tmp_path / "workspace.toml").write_text('[project."loggair"]\nurl = "u"\nbranch = "dev/main"\n')
        assert s.effective_source(tmp_path, "loggair") == s.ProjectSource(kind="github", ref="dev/main")

    def test_override_inherits_committed_distribution(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An override carries no dist name; without inheritance a pypi switch would install the DIRECTORY name
        (`acme`) instead of `acme-tools`."""
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({"aisland.source.acme.source": "pypi"}))
        s.write_committed_sources(tmp_path, {"acme": s.ProjectSource(kind="local", distribution="acme-tools")})
        effective = s.effective_source(tmp_path, "acme")
        assert effective is not None and effective.requirement("acme") == "acme-tools"

    def test_unmanaged_project_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({}))
        assert s.effective_source(tmp_path, "confluid") is None

    def test_malformed_override_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s.subprocess, "run", self._fake_config({"aisland.source.loggair.source": "svn"}))
        assert s.local_override(tmp_path, "loggair") is None

    def test_setting_an_override_writes_one_key_each_and_clearing_removes_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store: Dict[str, str] = {}
        monkeypatch.setattr(s.subprocess, "run", self._fake_config(store))
        s.set_local_override(tmp_path, "loggair", s.ProjectSource(kind="local", ref="feature"))
        assert store == {"aisland.source.loggair.source": "local", "aisland.source.loggair.branch": "feature"}
        s.set_local_override(tmp_path, "loggair", s.ProjectSource(kind="pypi", version=">=1"))
        assert store == {"aisland.source.loggair.source": "pypi", "aisland.source.loggair.version": ">=1"}
        s.set_local_override(tmp_path, "loggair", s.ProjectSource(kind="pypi"))
        assert store == {"aisland.source.loggair.source": "pypi"}
        s.set_local_override(tmp_path, "loggair", None)
        assert store == {}

    def test_other_aisland_settings_are_no_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = {"aisland.frameworks": "torch", "aisland.source.loggair": "pypi", "aisland.source.alpha.source": "pypi"}
        monkeypatch.setattr(s.subprocess, "run", self._fake_config(store))
        assert s._overridden_projects(tmp_path) == ["alpha"]


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
        """No ``pyproject.toml`` in the checkout -> the bare directory (uv then reports its own
        "not a Python project" error, exactly as before extras were threaded in)."""
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

    def test_local_installs_the_dev_extra_and_the_selected_frameworks(
        self, tmp_path: Path, recorder: _Recorder
    ) -> None:
        """The local install is the SAME target ``aisland setup`` computes for the checkout.

        Measured 2026-08-22: ``aisland source set confluid local --ref dev/main`` ran
        ``uv pip install -e <w>/confluid`` — no ``[dev]`` — so the dev-only ``omegaconf``
        vanished from the venv and its pin skipped; a framework project lost its backends too.
        """
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / CONFIG_FILE).write_text('frameworks = ["lightning"]\n')
        (tmp_path / "sonair").mkdir()
        (tmp_path / "sonair" / ".git").write_text("gitdir: ../.git/modules/sonair\n")
        (tmp_path / "sonair" / "pyproject.toml").write_text(
            '[project]\nname = "sonair"\n'
            '[project.optional-dependencies]\ndev = ["pytest"]\nlightning = ["lightning"]\nkeras = ["keras"]\n'
            '[tool.aisland]\nframeworks = ["lightning", "keras"]\n'
        )
        source = s.ProjectSource(kind="local", ref="dev/main")
        assert s.apply_source(tmp_path, "sonair", source, "/venv/bin/uv") is True
        assert recorder.flat()[-1] == f"/venv/bin/uv pip install -e {tmp_path / 'sonair'}[dev,lightning]"

    def test_local_without_extras_keeps_the_bare_directory(self, tmp_path: Path, recorder: _Recorder) -> None:
        """A project declaring no ``dev`` extra and no frameworks installs exactly as before."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        (tmp_path / "loggair").mkdir()
        (tmp_path / "loggair" / ".git").write_text("gitdir: ../.git/modules/loggair\n")
        (tmp_path / "loggair" / "pyproject.toml").write_text('[project]\nname = "loggair"\n')
        assert s.apply_source(tmp_path, "loggair", s.ProjectSource(kind="local"), "/venv/bin/uv") is True
        assert recorder.flat()[-1] == f"/venv/bin/uv pip install -e {tmp_path / 'loggair'}"

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


class TestScaffoldAsksPypiNotTheSource:
    """CI installs a RELEASED dependency from PyPI and an unreleased one from GitHub `main`,
    whatever `config.toml` says its source is (user ruling 2026-09-26: "it should always point
    to released packages, and to GitHub for non-released ones"). The source setting describes
    THIS machine's checkout; it never reaches a runner."""

    def test_an_unreleased_dep_is_installed_from_github_main(self, ci_workspace: Path) -> None:
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        files = _generated(ci_workspace, "confluid")
        assert "git+https://github.com/Gearlux/loggair.git@main" in files["Jenkinsfile"]
        assert "git+https://github.com/Gearlux/loggair.git@main" in files["ci.yml"]

    def test_a_released_dep_is_dropped_from_the_runner_files(
        self, ci_workspace: Path, released_packages: Set[str]
    ) -> None:
        """`.[dev]` resolves it from PyPI. Jenkinsfile.local is not included — it installs nothing."""
        released_packages.add("loggair")
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        files = _generated(ci_workspace, "confluid")
        for name in ("Jenkinsfile", "ci.yml"):
            assert "Gearlux/loggair.git" not in files[name], f"{name} still installs loggair from git"
            assert "WORKSPACE_ROOT}/loggair" not in files[name], f"{name} still installs from the workspace"
        assert "uv pip install -e .[dev]" in files["Jenkinsfile"]

    def test_a_released_dep_checked_out_locally_still_comes_from_pypi(
        self, ci_workspace: Path, released_packages: Set[str]
    ) -> None:
        """The case that wrote the ruling: liquifai is released but sourced `local`, and every
        consumer's CI installed it from GitHub `main`."""
        released_packages.add("loggair")
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        assert (ci_workspace / "loggair" / "pyproject.toml").exists()
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "Gearlux/loggair.git" not in _generated(ci_workspace, "confluid")["ci.yml"]

    def test_a_pypi_sourced_dep_with_no_final_release_comes_from_github(self, ci_workspace: Path) -> None:
        """`source set … pypi` accepts a pre-release-only name (recordstream 0.1.0a1); the
        scaffolder does not count that as released, so the runner builds from GitHub `main`."""
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "git+https://github.com/Gearlux/loggair.git@main" in _generated(ci_workspace, "confluid")["ci.yml"]

    def test_no_source_setting_changes_a_ci_file(self, ci_workspace: Path, released_packages: Set[str]) -> None:
        """Every kind, every ref, and unmanaged render the same three files — released or not."""
        for released in (False, True):
            if released:
                released_packages.add("loggair")
            rendered = []
            for source in (
                None,
                s.ProjectSource(kind="local", ref="main"),
                s.ProjectSource(kind="local", ref="dev/main"),
                s.ProjectSource(kind="github", ref="some/branch", distribution="loggair"),
                s.ProjectSource(kind="pypi", distribution="loggair"),
            ):
                s.write_committed_sources(ci_workspace, {} if source is None else {"loggair": source})
                JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
                rendered.append(_generated(ci_workspace, "confluid"))
            assert all(r == rendered[0] for r in rendered), f"released={released}"

    def test_github_dep_survives_its_own_empty_directory(self, ci_workspace: Path) -> None:
        """A detached project has NO pyproject.toml, so `discover_gearlux_projects` cannot read its
        name from disk — without the registry fallback the dep vanished from the internal set
        entirely and CI silently resolved the PyPI release (or failed, for an unpublished one)."""
        s.write_committed_sources(
            ci_workspace, {"loggair": s.ProjectSource(kind="github", ref="dev/main", distribution="loggair")}
        )
        # Detached == the directory is gone, exactly as `apply_source` leaves it.
        shutil.rmtree(ci_workspace / "loggair")

        from aisland.services.jenkins import discover_gearlux_projects

        assert "loggair" in discover_gearlux_projects(ci_workspace)
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "git+https://github.com/Gearlux/loggair.git@main" in _generated(ci_workspace, "confluid")["ci.yml"]

    def test_a_released_dep_is_dropped_when_its_directory_is_gone(
        self, ci_workspace: Path, released_packages: Set[str]
    ) -> None:
        released_packages.add("loggair")
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi", distribution="loggair")})
        shutil.rmtree(ci_workspace / "loggair")
        JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert "Gearlux/loggair.git" not in _generated(ci_workspace, "confluid")["ci.yml"]

    def test_an_unreachable_pypi_stops_the_scaffold_and_names_the_package(
        self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never a guess: an offline scaffold that assumed "unreleased" would flip every released
        dependency to a GitHub line."""

        def _unreachable(distribution: str) -> bool:
            raise s.ReleaseProbeError(distribution, "could not reach PyPI (offline)")

        monkeypatch.setattr("aisland.services.jenkins.released_on_pypi", _unreachable)
        with pytest.raises(click.ClickException, match="loggair.*could not reach PyPI"):
            JenkinsService().scaffold_pipelines(project_name="confluid", force=True)
        assert not (ci_workspace / "confluid" / ".github" / "workflows" / "ci.yml").exists()


class TestSourceChangesRewriteNoCI:
    """`aisland workspace configure source set/unset` decide this machine's checkout and venv — never a CI file."""

    def test_set_never_scaffolds(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (ci_workspace / ".venv" / "bin").mkdir(parents=True)
        (ci_workspace / ".venv" / "bin" / "uv").touch(mode=0o755)
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        monkeypatch.setattr(s, "apply_source", lambda *a, **k: True)
        monkeypatch.setattr(s, "_pypi_provenance", lambda dist: (True, "0.1.0"))
        assert s.set_project_source("loggair", "pypi", workspace_root=ci_workspace) == 0
        assert not (ci_workspace / "confluid" / "Jenkinsfile").exists()
        assert not (ci_workspace / "confluid" / ".github").exists()

    def test_unset_never_scaffolds(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi")})
        assert s.unset_project_source("loggair", workspace_root=ci_workspace) == 0
        assert not (ci_workspace / "confluid" / "Jenkinsfile").exists()

    def test_the_cli_has_no_scaffold_switch(self) -> None:
        import click

        from aisland.cli import main

        configure = main.commands["workspace"].commands["configure"]  # type: ignore[attr-defined]
        source = configure.get_command(click.Context(configure), "source")
        for command in ("set", "unset"):
            params = {p.name for p in source.commands[command].params}
            assert "no_scaffold" not in params, command


class TestReleasedOnPypi:
    """The released/not question the scaffolder asks: a FINAL version on PyPI whose metadata
    points at github.com/Gearlux/ (PyPI names are global — a stranger's release is not ours)."""

    @staticmethod
    def _payload(monkeypatch: pytest.MonkeyPatch, releases: Dict[str, Any], info: Dict[str, Any]) -> None:
        import contextlib
        import io
        import urllib.request

        s.released_on_pypi.cache_clear()

        @contextlib.contextmanager
        def _open(url: str, timeout: int = 5) -> Any:
            yield io.BytesIO(json.dumps({"info": info, "releases": releases}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", _open)

    _GEARLUX = {"project_urls": {"Repository": "https://github.com/Gearlux/liquifai.git"}}
    _FILE = [{"filename": "x.whl", "yanked": False}]

    def test_a_final_release_is_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._payload(monkeypatch, {"0.1.0": self._FILE, "0.2.0": self._FILE}, self._GEARLUX)
        assert s.released_on_pypi("liquifai") is True

    def test_only_pre_releases_is_not_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """recordstream: 0.1.0a1 is all PyPI has."""
        self._payload(monkeypatch, {"0.1.0a1": self._FILE}, self._GEARLUX)
        assert s.released_on_pypi("recordstream") is False

    def test_a_fully_yanked_final_release_does_not_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._payload(monkeypatch, {"0.1.0": [{"filename": "x.whl", "yanked": True}]}, self._GEARLUX)
        assert s.released_on_pypi("liquifai") is False

    def test_a_strangers_package_is_not_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._payload(monkeypatch, {"9.9.9": self._FILE}, {"project_urls": {"Homepage": "https://example.com/x"}})
        assert s.released_on_pypi("purifai") is False

    def test_404_is_not_released(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.request

        s.released_on_pypi.cache_clear()

        def _raise(url: str, timeout: int = 5) -> Any:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        assert s.released_on_pypi("matrainer") is False

    def test_an_unreachable_index_raises_naming_the_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.error
        import urllib.request

        s.released_on_pypi.cache_clear()

        def _raise(url: str, timeout: int = 5) -> Any:
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        with pytest.raises(s.ReleaseProbeError, match="liquifai"):
            s.released_on_pypi("liquifai")


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
        assert s.read_committed_sources(ci_workspace) == {}, "a registry created by the failed run must be removed"
        assert not (ci_workspace / workspace_file.FILE_NAME).exists(), "nothing else was configured: no file is left"

    def test_failed_apply_restores_a_pre_existing_entry(
        self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (ci_workspace / ".venv" / "bin").mkdir(parents=True)
        (ci_workspace / ".venv" / "bin" / "uv").touch(mode=0o755)
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        monkeypatch.setattr(s, "apply_source", lambda *a, **k: False)
        monkeypatch.setattr(s, "_pypi_provenance", lambda dist: (True, "0.1.0"))
        before = {"loggair": s.ProjectSource(kind="local", ref="dev/main")}
        s.write_committed_sources(ci_workspace, before)

        assert s.set_project_source("loggair", "pypi", workspace_root=ci_workspace) == 1
        assert s.read_committed_sources(ci_workspace) == before

    def test_rejected_pypi_name_writes_nothing(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "_pypi_provenance", lambda dist: (False, "is not published on PyPI"))
        assert s.set_project_source("loggair", "pypi", workspace_root=ci_workspace) == 1
        assert not (ci_workspace / workspace_file.FILE_NAME).exists()

    def test_unknown_project_writes_nothing(self, ci_workspace: Path) -> None:
        assert s.set_project_source("nosuchproj", "pypi", workspace_root=ci_workspace) == 1
        assert not (ci_workspace / workspace_file.FILE_NAME).exists()


class TestSetALocalSource:
    """`set <project> local` without --branch keeps the project's own branch; a failed set puts workspace.toml back
    byte for byte, a branch it wrote included."""

    @pytest.fixture
    def stubbed(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        (ci_workspace / ".venv" / "bin").mkdir(parents=True)
        (ci_workspace / ".venv" / "bin" / "uv").touch(mode=0o755)
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        monkeypatch.setattr("aisland.manager.write_code_workspace", lambda root: None)
        (ci_workspace / "workspace.toml").write_text('[project."loggair"]\nurl = "u"\nbranch = "dev/main"\n')
        return ci_workspace

    def test_without_a_branch_the_projects_own_is_kept(self, stubbed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "apply_source", lambda *a, **k: True)
        assert s.set_project_source("loggair", "local", workspace_root=stubbed) == 0
        assert s.read_committed_sources(stubbed)["loggair"].ref == "dev/main"

    def test_a_branch_given_is_written_as_the_projects_branch(
        self, stubbed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s, "apply_source", lambda *a, **k: True)
        assert s.set_project_source("loggair", "local", ref="feature", workspace_root=stubbed) == 0
        assert workspace_file.load(stubbed).projects["loggair"].branch == "feature"

    def test_a_failed_set_puts_the_file_back_branch_and_all(
        self, stubbed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = (stubbed / "workspace.toml").read_text()
        monkeypatch.setattr(s, "apply_source", lambda *a, **k: False)
        assert s.set_project_source("loggair", "local", ref="feature", workspace_root=stubbed) == 1
        assert (stubbed / "workspace.toml").read_text() == before

    def test_a_github_branch_missing_on_the_remote_is_refused(
        self, stubbed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s, "_remote_ref_exists", lambda root, project, ref: False)
        assert s.set_project_source("loggair", "github", workspace_root=stubbed) == 1
        assert "source" not in workspace_file.load(stubbed).projects["loggair"].others


class TestUnsetProjectSource:
    def test_removes_the_committed_entry(self, ci_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
        s.write_committed_sources(ci_workspace, {"loggair": s.ProjectSource(kind="pypi")})
        assert s.unset_project_source("loggair", workspace_root=ci_workspace) == 0
        assert s.read_committed_sources(ci_workspace) == {}

    def test_already_unmanaged_is_a_no_op(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(s, "local_override", lambda root, name: None)
        assert s.unset_project_source("loggair", workspace_root=tmp_path) == 0
        assert not (tmp_path / workspace_file.FILE_NAME).exists()

    def test_machine_only_keeps_the_committed_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cleared: List[Any] = []
        monkeypatch.setattr(s, "local_override", lambda root, name: s.ProjectSource(kind="pypi"))
        monkeypatch.setattr(s, "set_local_override", lambda root, name, src: cleared.append((name, src)))
        s.write_committed_sources(tmp_path, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
        s.unset_project_source("loggair", machine_only=True, workspace_root=tmp_path)
        assert cleared == [("loggair", None)]
        assert set(s.read_committed_sources(tmp_path)) == {"loggair"}
