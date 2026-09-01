"""`aisland doctor` — preflight checks for known recurring venv skew.

Each check encodes an invariant the workspace enforces elsewhere (the uv override policy in
`manager._ROOT_UV_BASE`, the huggingface_hub floor in the root AGENTS.md). These tests drive
the checks through both the healthy state and the exact broken state that motivated them, so
a future refactor cannot silently turn a check into a no-op.
"""

import json
from pathlib import Path
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


class TestMcpServerApiCheck:
    """The 2.x MCP SDK renamed the server class AND its module; the two never coexist.

    The real incident: `mcp` 2.0.0 resolved into the shared venv against an unbounded
    `mcp>=1.9.0` floor, `mcp.server.fastmcp` vanished, and `vendorcli-mcp` died on import —
    discovered only when someone tried to use it.
    """

    def test_skipped_when_mcp_absent(self, installed: Dict[str, Optional[str]]) -> None:
        result = doctor.check_mcp_server_api()
        assert result.ok is True
        assert "not installed" in result.detail

    def test_healthy_when_the_server_class_imports(
        self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]
    ) -> None:
        installed["mcp"] = "2.0.0"
        probe["output"] = "2.0.0"
        result = doctor.check_mcp_server_api()
        assert result.ok is True
        assert "2.0.0" in result.detail

    def test_missing_server_class_reports_the_upgrade(
        self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]
    ) -> None:
        installed["mcp"] = "1.28.1"
        probe["ok"] = False
        probe["output"] = "ModuleNotFoundError: No module named 'mcp.server.mcpserver'"
        result = doctor.check_mcp_server_api()
        assert result.ok is False
        assert "mcp.server.mcpserver" in result.detail
        assert "mcp>=2.0" in result.remedy

    def test_probes_the_exact_symbol_the_servers_import(
        self, installed: Dict[str, Optional[str]], probe: Dict[str, Any]
    ) -> None:
        """Guards against a refactor that turns the check into a version comparison.

        A version number does not prove the class is importable; the symbol does.
        """
        installed["mcp"] = "2.0.0"
        doctor.check_mcp_server_api()
        assert any("from mcp.server.mcpserver import MCPServer" in call for call in probe["calls"])


class TestDockerCredsStoreCheck:
    """A `credsStore` naming an absent helper kills EVERY docker pull before the registry is contacted.

    The real incident (2026-08-31): `~/.docker/config.json` still carried `"credsStore":
    "desktop"` after Docker Desktop was replaced by Colima, so `aisland iqengine start` died
    with `exec: "docker-credential-desktop": executable file not found in $PATH` — for a
    PUBLIC image that needs no credentials at all.
    """

    @pytest.fixture()
    def docker_config(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """Point `Path.home()` at a temp dir; return the docker config path inside it (not yet written)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".docker").mkdir()
        return tmp_path / ".docker" / "config.json"

    @pytest.fixture()
    def helpers(self, monkeypatch: pytest.MonkeyPatch) -> Dict[str, str]:
        """Control which `docker-credential-*` binaries `shutil.which` finds."""
        table: Dict[str, str] = {}
        monkeypatch.setattr(doctor.shutil, "which", lambda name: table.get(name))
        return table

    def test_no_config_file_is_healthy(self, docker_config: Path, helpers: Dict[str, str]) -> None:
        result = doctor.check_docker_credsstore()
        assert result.ok is True
        assert "nothing to skew" in result.detail

    def test_no_credsstore_key_is_healthy(self, docker_config: Path, helpers: Dict[str, str]) -> None:
        # The post-fix state of the incident machine: auths empty, no credsStore at all.
        docker_config.write_text(json.dumps({"auths": {}, "currentContext": "colima"}))
        result = doctor.check_docker_credsstore()
        assert result.ok is True
        assert "no credsStore" in result.detail

    def test_missing_helper_fails_with_the_removal_remedy(self, docker_config: Path, helpers: Dict[str, str]) -> None:
        # The exact state that produced the incident.
        docker_config.write_text(json.dumps({"auths": {}, "credsStore": "desktop"}))
        result = doctor.check_docker_credsstore()
        assert result.ok is False
        assert "docker-credential-desktop" in result.detail
        assert "config.json.bak" in result.remedy  # the remedy backs up before editing
        assert "credsStore" in result.remedy

    def test_present_helper_is_healthy(self, docker_config: Path, helpers: Dict[str, str]) -> None:
        # A working helper is NOT the skew — only a dangling reference is.
        docker_config.write_text(json.dumps({"credsStore": "osxkeychain"}))
        helpers["docker-credential-osxkeychain"] = "/usr/local/bin/docker-credential-osxkeychain"
        result = doctor.check_docker_credsstore()
        assert result.ok is True
        assert "osxkeychain" in result.detail

    @pytest.mark.parametrize("content", ["not json {{", "[]"])
    def test_unreadable_config_is_inconclusive_not_a_failure(
        self, docker_config: Path, helpers: Dict[str, str], content: str
    ) -> None:
        # An inconclusive probe must never cry wolf (same principle as
        # `supervisor._preflight_full_disk_access`): a mangled config is docker's problem
        # to report, not this check's.
        docker_config.write_text(content)
        result = doctor.check_docker_credsstore()
        assert result.ok is True
        assert "inconclusive" in result.detail


class TestColimaVmTrustCheck:
    """The Colima VM must verify TLS on the network the host is on, or every pull fails.

    The real incident (2026-08-31): a TLS-intercepting corporate firewall re-signs all
    traffic with a private CA that macOS trusts (System keychain) but the Ubuntu VM inside
    Colima does not — `docker pull` died with `tls: failed to verify certificate: x509:
    certificate signed by unknown authority` on the blob host. The fix (installing the CA
    into the VM) does NOT survive `colima delete`, which is why a doctor check exists.
    """

    @pytest.fixture()
    def colima(self, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
        """Control colima's presence, the VM's run state, and the in-VM curl probe."""
        state: Dict[str, Any] = {"installed": True, "running": True, "curl_exit": 0, "urls": []}
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda name: "/opt/homebrew/bin/colima" if (name == "colima" and state["installed"]) else None,
        )
        monkeypatch.setattr(doctor, "_colima_running", lambda: state["running"])

        def _fake_curl(url: str) -> int:
            state["urls"].append(url)
            return int(state["curl_exit"])

        monkeypatch.setattr(doctor, "_vm_curl_exit", _fake_curl)
        return state

    def test_skipped_when_colima_is_not_installed(self, colima: Dict[str, Any]) -> None:
        colima["installed"] = False
        result = doctor.check_colima_vm_trust()
        assert result.ok is True
        assert "not installed" in result.detail
        assert colima["urls"] == []  # nothing was probed

    def test_a_stopped_vm_is_not_probed_and_not_a_failure(self, colima: Dict[str, Any]) -> None:
        # The doctor must never boot a VM, and a laptop not running Docker must pass preflight.
        colima["running"] = False
        result = doctor.check_colima_vm_trust()
        assert result.ok is True
        assert "not probed" in result.detail
        assert colima["urls"] == []

    def test_cert_failure_reports_the_ca_install_remedy(self, colima: Dict[str, Any]) -> None:
        # curl exit 60 is specifically "certificate verification failed" — the incident state.
        colima["curl_exit"] = 60
        result = doctor.check_colima_vm_trust()
        assert result.ok is False
        assert "cannot verify TLS" in result.detail
        assert "s_client" in result.remedy  # the CA is captured from a live handshake
        assert "update-ca-certificates" in result.remedy
        assert "restart docker" in result.remedy

    def test_a_network_hiccup_is_inconclusive_not_a_failure(self, colima: Dict[str, Any]) -> None:
        # Exit 7 = connection refused/unreachable: the handshake never happened, so trust
        # was not tested — an inconclusive probe must never cry wolf.
        colima["curl_exit"] = 7
        result = doctor.check_colima_vm_trust()
        assert result.ok is True
        assert "inconclusive" in result.detail

    def test_healthy_when_the_vm_verifies(self, colima: Dict[str, Any]) -> None:
        result = doctor.check_colima_vm_trust()
        assert result.ok is True
        assert "verifies TLS" in result.detail

    def test_probes_the_registry_the_workspace_pulls_from(self, colima: Dict[str, Any]) -> None:
        """Guards against a refactor silently retargeting the probe at an unrelated host."""
        doctor.check_colima_vm_trust()
        assert colima["urls"] == ["https://ghcr.io/v2/"]


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
