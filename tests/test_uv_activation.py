"""Tests for the activation-driven submodule lifecycle + generated root uv.toml.

Covers `aisland setup`'s new pieces in `manager.py`:
- `_write_root_uv_toml` / `_merge_uv` / `_emit_uv_toml` — the generated (gitignored) root
  uv.toml = workspace base merged with each ACTIVATED project's uv.toml.
- `_project_is_optional` — which submodules are prompted.
- `_resolve_submodule_activation` — per-machine `.git/config` decisions + prompt semantics.
- `reconcile_submodules` honoring the per-machine `.git/config` override.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from aisland import manager as m

# The workspace-wide override policy (manager._ROOT_UV_BASE), restated so a change to it is a
# deliberate edit here too: the GUI `opencv-python` is dropped workspace-wide, and headless
# opencv is pinned to the one range every consumer can share.
_OVERRIDE = [
    "opencv-python ; sys_platform == 'never'",
    "opencv-python-headless>=4.12,<5",
]

# The conditional half (manager._CONDITIONAL_OVERRIDES): dropping the unified `lightning` applies
# ONLY where a project names `pytorch-lightning`, because the two conflict with each other and
# nothing else. A workspace with no `pytorch-lightning` keeps `lightning` — stripping it there
# removes a dependency its projects declare outright.
_LIGHTNING_OVERRIDE = ["lightning ; sys_platform == 'never'"]


def _pyproject(project: Path, name: str, requires: list[str]) -> None:
    body = ", ".join(f'"{r}"' for r in requires)
    (project / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = [{body}]\n')


# --- root uv.toml generation --------------------------------------------------
class TestWriteRootUvToml:
    def test_lightning_is_kept_where_nothing_wants_pytorch_lightning(self, tmp_path: Path) -> None:
        """Another workspace, 2026-10-20: two projects require `lightning`, nothing requires the other one."""
        project = tmp_path / "my_project"
        project.mkdir()
        _pyproject(project, "my-project", ["lightning", "torch"])
        m._write_root_uv_toml(tmp_path, [{"name": "my_project", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _OVERRIDE
        assert "lightning ; sys_platform == 'never'" not in data["override-dependencies"]

    def test_lightning_is_dropped_where_a_project_wants_pytorch_lightning(self, tmp_path: Path) -> None:
        """acme, where the override was written: matrainer and friends declare pytorch-lightning."""
        project = tmp_path / "matrainer"
        project.mkdir()
        _pyproject(project, "matrainer", ["pytorch-lightning"])
        m._write_root_uv_toml(tmp_path, [{"name": "matrainer", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _LIGHTNING_OVERRIDE + _OVERRIDE

    def test_pytorch_lightning_behind_an_extra_still_triggers_the_override(self, tmp_path: Path) -> None:
        """matrainer declares it under [lightning]; an override that came and went with the
        selected extras would resolve differently run to run."""
        project = tmp_path / "matrainer"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[project]\nname = "matrainer"\nversion = "0.1.0"\ndependencies = []\n\n'
            "[project.optional-dependencies]\n"
            'lightning = ["pytorch-lightning", "torchmetrics"]\n'
        )
        m._write_root_uv_toml(tmp_path, [{"name": "matrainer", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _LIGHTNING_OVERRIDE + _OVERRIDE

    def test_the_distribution_name_is_normalised(self, tmp_path: Path) -> None:
        """`pytorch_lightning` and `pytorch-lightning` are the same distribution (PEP 503)."""
        project = tmp_path / "p"
        project.mkdir()
        _pyproject(project, "p", ["pytorch_lightning"])
        m._write_root_uv_toml(tmp_path, [{"name": "p", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _LIGHTNING_OVERRIDE + _OVERRIDE

    def test_merges_base_with_activated_project(self, tmp_path: Path) -> None:
        vendorcli = tmp_path / "vendorcli"
        vendorcli.mkdir()
        (vendorcli / "uv.toml").write_text('extra-index-url = ["https://idx/simple"]\n')
        m._write_root_uv_toml(tmp_path, [{"name": "vendorcli", "path": vendorcli}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _OVERRIDE
        assert data["extra-index-url"] == ["https://idx/simple"]

    def test_carries_a_project_constraint_through_to_the_root(self, tmp_path: Path) -> None:
        # A project that depends on a package whose own metadata names a direct URL must be able to
        # constrain that URL (uv refuses a transitive URL otherwise). The merge is key-agnostic, so
        # `constraint-dependencies` reaches the root exactly like `extra-index-url` — and only when
        # the project is activated.
        pin = "pkg @ https://idx/pkg-0.0.0%2Balpha.1-py3-none-any.whl"
        vendorcli = tmp_path / "vendorcli"
        vendorcli.mkdir()
        (vendorcli / "uv.toml").write_text(f'constraint-dependencies = ["{pin}"]\n')
        m._write_root_uv_toml(tmp_path, [{"name": "vendorcli", "path": vendorcli}])
        assert tomllib.loads((tmp_path / "uv.toml").read_text())["constraint-dependencies"] == [pin]
        m._write_root_uv_toml(tmp_path, [])
        assert "constraint-dependencies" not in tomllib.loads((tmp_path / "uv.toml").read_text())

    def test_base_only_when_no_uv_toml(self, tmp_path: Path) -> None:
        ordinary = tmp_path / "confluid"
        ordinary.mkdir()
        m._write_root_uv_toml(tmp_path, [{"name": "confluid", "path": ordinary}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert "extra-index-url" not in data  # no private index leaks in
        assert data["override-dependencies"] == _OVERRIDE

    def test_empty_activated_is_base_only(self, tmp_path: Path) -> None:
        m._write_root_uv_toml(tmp_path, [])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data == {"override-dependencies": _OVERRIDE}

    def test_unions_indexes_across_projects(self, tmp_path: Path) -> None:
        for name, url in (("a", "https://x/simple"), ("b", "https://x/simple")):  # duplicate url
            p = tmp_path / name
            p.mkdir()
            (p / "uv.toml").write_text(f'extra-index-url = ["{url}"]\n')
        (tmp_path / "b" / "uv.toml").write_text('extra-index-url = ["https://x/simple", "https://y/simple"]\n')
        m._write_root_uv_toml(tmp_path, [{"name": "a", "path": tmp_path / "a"}, {"name": "b", "path": tmp_path / "b"}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["extra-index-url"] == ["https://x/simple", "https://y/simple"]  # deduped

    def test_output_is_valid_toml_with_header(self, tmp_path: Path) -> None:
        m._write_root_uv_toml(tmp_path, [])
        raw = (tmp_path / "uv.toml").read_text()
        assert raw.startswith("# ")  # provenance header
        tomllib.loads(raw)  # parses

    def test_regenerates_idempotently(self, tmp_path: Path) -> None:
        vendorcli = tmp_path / "vendorcli"
        vendorcli.mkdir()
        (vendorcli / "uv.toml").write_text('extra-index-url = ["https://idx/simple"]\n')
        m._write_root_uv_toml(tmp_path, [{"name": "vendorcli", "path": vendorcli}])
        assert "idx" in (tmp_path / "uv.toml").read_text()
        m._write_root_uv_toml(tmp_path, [])  # vendorcli deactivated on the second run
        assert "idx" not in (tmp_path / "uv.toml").read_text()


def test_merge_uv_unions_lists_and_last_wins_scalars() -> None:
    into: dict[str, Any] = {"extra-index-url": ["a"]}
    m._merge_uv(into, {"extra-index-url": ["a", "b"], "index-url": "u"})
    assert into == {"extra-index-url": ["a", "b"], "index-url": "u"}


def test_emit_uv_toml_round_trips_supported_types() -> None:
    emitted = m._emit_uv_toml({"s": "x", "b": True, "n": 3, "lst": ["a", "b"]})
    assert tomllib.loads(emitted) == {"s": "x", "b": True, "n": 3, "lst": ["a", "b"]}


# --- optional detection -------------------------------------------------------
def test_project_is_optional(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    assert m._project_is_optional(p, True) is False  # ordinary, active, no uv.toml
    assert m._project_is_optional(p, False) is True  # active=false in .gitmodules
    (p / "uv.toml").write_text("")
    assert m._project_is_optional(p, True) is True  # ships uv.toml


# --- per-machine local override parsing --------------------------------------
def _fake_run(returncode: int, stdout: str) -> Any:
    def run(cmd: list[str], **kwargs: Any) -> Any:
        return type("R", (), {"returncode": returncode, "stdout": stdout})()

    return run


def test_local_submodule_active_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m.subprocess, "run", _fake_run(0, "true\n"))
    assert m._local_submodule_active(tmp_path, "x") is True


def test_local_submodule_active_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m.subprocess, "run", _fake_run(0, "false\n"))
    assert m._local_submodule_active(tmp_path, "x") is False


def test_local_submodule_active_unset_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m.subprocess, "run", _fake_run(1, ""))
    assert m._local_submodule_active(tmp_path, "x") is None


# --- activation resolution ----------------------------------------------------
def _stub_optional(monkeypatch: pytest.MonkeyPatch, entries: list[tuple[str, str, bool]]) -> dict[str, bool]:
    """Stub the activation surface; return a dict that captures `.git/config` writes."""
    monkeypatch.setattr(m, "submodule_active_map", lambda root: entries)
    monkeypatch.setattr(m, "_project_is_optional", lambda path, committed: True)
    written: dict[str, bool] = {}
    monkeypatch.setattr(m, "_set_local_submodule_active", lambda root, name, active: written.__setitem__(name, active))
    return written


def test_resolve_prompts_undecided_even_non_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written = _stub_optional(monkeypatch, [("vendorcli", "vendorcli", False)])
    monkeypatch.setattr(m, "_local_submodule_active", lambda root, name: None)  # undecided
    prompted: list[str] = []

    def fake_confirm(msg: str, default: bool = False) -> bool:
        prompted.append(msg)
        return True

    monkeypatch.setattr(m.click, "confirm", fake_confirm)
    m._resolve_submodule_activation(tmp_path, non_interactive=True)  # new module → still asks
    assert prompted and written == {"vendorcli": True}


def test_resolve_honors_decided_silently_non_interactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_optional(monkeypatch, [("vendorcli", "vendorcli", False)])
    monkeypatch.setattr(m, "_local_submodule_active", lambda root, name: True)  # decided
    monkeypatch.setattr(m, "_set_local_submodule_active", lambda *a: pytest.fail("should not rewrite a decided module"))
    monkeypatch.setattr(m.click, "confirm", lambda *a, **k: pytest.fail("should not prompt a decided module"))
    m._resolve_submodule_activation(tmp_path, non_interactive=True)  # no error = silent honor


def test_resolve_forced_flags_skip_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written = _stub_optional(monkeypatch, [("vendorcli", "vendorcli", False), ("hel", "hel", False)])
    monkeypatch.setattr(m, "_local_submodule_active", lambda root, name: None)
    monkeypatch.setattr(m.click, "confirm", lambda *a, **k: pytest.fail("forced flags must not prompt"))
    m._resolve_submodule_activation(tmp_path, activate=("vendorcli",), deactivate=("hel",))
    assert written == {"vendorcli": True, "hel": False}


# --- reconcile honors the override -------------------------------------------
def test_reconcile_deinits_locally_deactivated_committed_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / ".git").write_text("")  # looks checked-out
    monkeypatch.setattr(m, "submodule_active_map", lambda root: [("x", "x", True)])  # committed active
    monkeypatch.setattr(m, "_local_submodule_active", lambda root, name: False)  # per-machine deactivate
    calls: list[list[str]] = []
    monkeypatch.setattr(m.subprocess, "check_call", lambda cmd, **k: calls.append(cmd))
    m.reconcile_submodules(tmp_path)
    assert any("deinit" in c for c in calls)
