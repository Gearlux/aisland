"""`aisland doctor` — preflight checks for known recurring venv skew.

Each check encodes an invariant the workspace enforces elsewhere (the uv override policy in
`manager._ROOT_UV_BASE`, the huggingface_hub floor in the root AGENTS.md). These tests drive
the checks through both the healthy state and the exact broken state that motivated them, so
a future refactor cannot silently turn a check into a no-op.
"""

from typing import Any, Dict, List, Optional

import pytest
from click.testing import CliRunner

from aisland import doctor
from aisland.cli import main


@pytest.fixture()
def installed(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Optional[str]]:
    """Control what `_installed_version` reports, without touching the real venv."""
    table: Dict[str, Optional[str]] = {}
    monkeypatch.setattr(doctor, "_installed_version", lambda name: table.get(name))
    return table


@pytest.fixture()
def probe(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Control the import probe's outcome and record the statements it was asked to run."""
    state: Dict[str, Any] = {"ok": True, "output": "4.13.0", "calls": []}

    def _fake(statement: str) -> Any:
        state["calls"].append(statement)
        return state["ok"], state["output"]

    monkeypatch.setattr(doctor, "_import_probe", _fake)
    return state


class TestOpenCVCheck:
    def test_healthy_when_only_headless_is_installed(
        self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]
    ) -> None:
        installed["opencv-python-headless"] = "4.13.0.92"
        result = doctor.check_opencv_single_variant()
        assert result.ok is True
        assert "only opencv-python-headless=4.13.0.92" in result.detail

    def test_both_variants_installed_fails(self, installed: Dict[str, Optional[str]]) -> None:
        # The state that produced the dlopen failure: both wheels own cv2/, so an install of
        # either one leaves the other's .dylibs behind.
        installed["opencv-python"] = "4.13.0.92"
        installed["opencv-python-headless"] = "4.13.0.92"
        result = doctor.check_opencv_single_variant()
        assert result.ok is False
        assert "both own cv2/" in result.detail
        assert "uv pip uninstall opencv-python" in result.remedy

    def test_gui_wheel_alone_still_fails(self, installed: Dict[str, Optional[str]]) -> None:
        # Matching versions are not the invariant — the override drops this wheel outright.
        installed["opencv-python"] = "4.13.0.92"
        result = doctor.check_opencv_single_variant()
        assert result.ok is False
        assert "the workspace override drops it" in result.detail

    def test_no_opencv_at_all_is_not_a_failure(self, installed: Dict[str, Optional[str]]) -> None:
        assert doctor.check_opencv_single_variant().ok is True

    def test_broken_native_bundle_is_reported_with_the_dlopen_line(
        self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]
    ) -> None:
        installed["opencv-python-headless"] = "4.13.0.92"
        probe["ok"] = False
        probe["output"] = "ImportError: dlopen(...): Library not loaded: @loader_path/libsrt.1.5.3.dylib"
        result = doctor.check_opencv_single_variant()
        assert result.ok is False
        assert "libsrt.1.5.3.dylib" in result.detail
        assert "force-reinstall" in result.remedy

    def test_import_is_probed_out_of_process(self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]) -> None:
        # A broken native bundle can abort the interpreter; the doctor must not die with it.
        installed["opencv-python-headless"] = "4.13.0.92"
        doctor.check_opencv_single_variant()
        assert probe["calls"] == ["import cv2; print(cv2.__version__)"]


class TestHuggingFaceHubCheck:
    def test_skipped_when_transformers_absent(self, installed: Dict[str, Optional[str]]) -> None:
        result = doctor.check_huggingface_hub_floor()
        assert result.ok is True
        assert "not installed" in result.detail

    def test_healthy_when_the_import_surface_exists(
        self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]
    ) -> None:
        installed["transformers"] = "5.1.0"
        probe["output"] = "1.12.2"
        result = doctor.check_huggingface_hub_floor()
        assert result.ok is True and "1.12.2" in result.detail

    def test_stale_hub_reports_the_pin_free_remedy(
        self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]
    ) -> None:
        installed["transformers"] = "5.1.0"
        probe["ok"] = False
        probe["output"] = "ImportError: cannot import name 'is_offline_mode' from 'huggingface_hub'"
        result = doctor.check_huggingface_hub_floor()
        assert result.ok is False
        assert "is_offline_mode" in result.detail
        assert result.remedy == "uv pip install 'huggingface_hub>=1.5.0,<2.0'"


class TestReporting:
    def test_exit_code_is_zero_when_everything_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor, "CHECKS", (lambda: doctor.CheckResult("x", True, "fine"),))
        assert doctor.run_doctor() == 0

    def test_exit_code_is_one_and_remedy_is_printed_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(doctor, "CHECKS", (lambda: doctor.CheckResult("x", False, "bad", "do the thing"),))
        assert doctor.run_doctor() == 1
        out = capsys.readouterr().out
        assert "✗ x" in out and "fix: do the thing" in out

    def test_every_registered_check_returns_a_result(self) -> None:
        results: List[doctor.CheckResult] = doctor.run_checks()
        assert len(results) == len(doctor.CHECKS)
        assert all(isinstance(r, doctor.CheckResult) and r.detail for r in results)
        assert all(r.ok or r.remedy for r in results), "a failing check must carry a remedy"

    def test_cli_command_exits_nonzero_when_a_check_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor, "CHECKS", (lambda: doctor.CheckResult("x", False, "bad", "fix it"),))
        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code == 1
        assert "fix: fix it" in result.output
