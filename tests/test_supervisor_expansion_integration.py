"""Integration test pinning process-compose's ``${VAR}`` expansion contract.

``supervisor.apply()`` stores credentials in the desired-state document as
``${VAR}`` references and layers the workspace ``.env`` over the client env,
because expansion happens CLIENT-side when ``project update`` loads the
config — NOT in the server (which loaded its env once at startup). This test
runs the REAL binary to pin that contract: if a future process-compose
release moves expansion server-side or starts erroring on unset variables,
this fails loudly instead of aisland silently wiping stored credentials.

Promoted from the ad-hoc verification against v1.116.0 (2026-07-08).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import yaml

from aisland import supervisor

BINARY = supervisor.find_binary()

pytestmark = pytest.mark.skipif(BINARY is None, reason="process-compose binary not installed")


def _write_config(path: Path, extra: dict) -> None:
    processes = {
        # Keeps the project alive between updates (mirrors --keep-project use).
        "keeper": {"command": "sleep 60", "availability": {"restart": "no"}},
        **extra,
    }
    path.write_text(yaml.safe_dump({"processes": processes}))


def _wait_for_file(path: Path, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text()
        time.sleep(0.2)
    raise AssertionError(f"{path} not produced within {timeout}s")


def test_project_update_expands_refs_from_client_env(tmp_path: Path) -> None:
    assert BINARY is not None
    cfg = tmp_path / "pc.yaml"
    sock = "pc.sock"  # relative to cwd — absolute tmp paths can exceed AF_UNIX limits
    _write_config(cfg, {})
    up = subprocess.run(
        [BINARY, "up", "-f", str(cfg), "--detached", "--keep-project", "-L", "server.log", "-u", sock],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert up.returncode == 0, up.stderr
    try:
        # 1. A ${VAR} present in the CLIENT env at `project update` expands.
        out_set = tmp_path / "env-set.txt"
        _write_config(
            cfg,
            {
                "echo_set": {
                    "command": f"env > {out_set}",
                    "availability": {"restart": "no"},
                    "environment": ["GOT=${PC_EXPANSION_PROBE}"],
                }
            },
        )
        subprocess.run(
            [BINARY, "project", "update", "-f", str(cfg), "-u", sock],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin", "PC_EXPANSION_PROBE": "from-client-env"},
            capture_output=True,
            timeout=30,
            check=True,
        )
        assert "GOT=from-client-env" in _wait_for_file(out_set)

        # 2. The same ref with the var ABSENT from the client env expands to
        #    empty — the wipe scenario supervisor.apply()'s .env layering
        #    exists to prevent.
        out_unset = tmp_path / "env-unset.txt"
        _write_config(
            cfg,
            {
                "echo_unset": {
                    "command": f"env > {out_unset}",
                    "availability": {"restart": "no"},
                    "environment": ["GOT=${PC_EXPANSION_PROBE}"],
                }
            },
        )
        subprocess.run(
            [BINARY, "project", "update", "-f", str(cfg), "-u", sock],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            timeout=30,
            check=True,
        )
        assert "GOT=\n" in _wait_for_file(out_unset)
    finally:
        subprocess.run([BINARY, "down", "-u", sock], cwd=tmp_path, capture_output=True, timeout=30)
