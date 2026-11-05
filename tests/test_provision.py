"""Tests for aisland.setup.manager's installers (Homebrew, the userspace registry) and shell-switch offer.

Which tools are installed, and the command: `tests/test_workspace_provision.py`.

Covers the deterministic early-return branches of `offer_shell_switch`.
The interactive sudo / chsh paths are not exercised here — they require
mocking subprocess + tty + interactive prompts and the behavior is shell-
specific in a way unit tests can't usefully verify.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import click
import pytest

from aisland import locations, paths
from aisland.setup import installers, manager


def _current_arch() -> str:
    """The arch token the installer resolves to (matches install_userspace_tool)."""
    return installers._LINUX_ARCH_ALIASES[platform.machine().lower()]


def _subdir(spec: Any) -> str:
    """The folder the tool's archive unpacks into on this machine, in the tool's own name for the architecture."""
    arch = spec.get("arch_names", {}).get(_current_arch(), _current_arch())
    return str(spec["extract_subdir"].format(version=spec["version"], arch=arch))


def test_shell_switch_skipped_on_non_darwin(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """On Linux / Windows the chsh flow is irrelevant — must early-return silently."""
    monkeypatch.setattr(platform, "system", lambda: "Linux", raising=False)
    installers.offer_shell_switch({"bash"})
    assert capsys.readouterr().out == ""


def test_shell_switch_skipped_when_bash_not_required(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """If `bash` is not in the project's sys-deps, the offer must not appear."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin", raising=False)
    installers.offer_shell_switch({"libomp", "openjdk"})
    assert capsys.readouterr().out == ""


def test_shell_switch_skipped_when_brew_bash_missing(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """If neither /opt/homebrew/bin/bash nor /usr/local/bin/bash exists, warn + skip."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin", raising=False)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    installers.offer_shell_switch({"bash"})
    out = capsys.readouterr().err
    assert "Homebrew bash not found" in out


def test_shell_switch_silent_when_already_current(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """If SHELL already points to brew bash, no offer should appear (idempotency)."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin", raising=False)
    # Pretend /opt/homebrew/bin/bash exists, /usr/local one doesn't.
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/opt/homebrew/bin/bash":
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/bash")
    installers.offer_shell_switch({"bash"})
    assert capsys.readouterr().out == ""


def test_shell_switch_declined_exits_cleanly(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """User declines the prompt — function must return without touching the system."""
    monkeypatch.setattr(platform, "system", lambda: "Darwin", raising=False)
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/opt/homebrew/bin/bash":
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setenv("SHELL", "/bin/bash")
    # click.confirm reads from stdin; mock it to "no".
    monkeypatch.setattr(click, "confirm", lambda *a, **kw: False)

    # Spy subprocess.run/check_call so we can assert nothing was invoked.
    calls: list = []

    def _record(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("subprocess should NOT be invoked when user declines")

    monkeypatch.setattr(subprocess, "run", _record)
    monkeypatch.setattr(subprocess, "check_call", _record)

    installers.offer_shell_switch({"bash"})
    out = capsys.readouterr().out
    assert "Modern bash is installed" in out
    assert calls == []


# ---------------------------------------------------------------------------
# The userspace installer (Linux).
# ---------------------------------------------------------------------------


def test_userspace_install_unknown_tool_errors(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """A tool name not in the registry must be reported, not silently skipped."""
    installers.install_userspace_tool("definitely-not-a-real-tool")
    assert "not a known userspace tool" in capsys.readouterr().err


def test_userspace_install_skips_when_already_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Idempotency: if all target symlinks already exist, no download is attempted — said in the debug log only."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Pre-create all three symlinks so the idempotency check fires.
    for name in ("node", "npm", "npx"):
        (bin_dir / name).write_text("stub")

    monkeypatch.setattr(paths, "bin_dir", lambda: bin_dir)
    monkeypatch.setattr(paths, "tools_dir", lambda: tmp_path / "tools")

    def _explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("nothing is downloaded when already installed")

    monkeypatch.setattr(installers, "download", _explode)

    with caplog.at_level(logging.DEBUG, logger="aisland"):
        installers.install_userspace_tool("node")
    assert "already installed" not in capsys.readouterr().out
    assert (
        f"node {installers.USERSPACE_TOOLS['node']['version']} already installed (symlinks present in {bin_dir})"
        in (caplog.messages)
    )


def _build_fake_node_tarball(staging: Path, extract_subdir: str) -> Path:
    """Construct a tar.xz that, when extracted into TOOLS_DIR, yields tools/<subdir>/bin/{node,npm,npx}."""
    src_root = staging / extract_subdir
    (src_root / "bin").mkdir(parents=True)
    for binary in ("node", "npm", "npx"):
        path = src_root / "bin" / binary
        path.write_text(f"#!/bin/sh\necho {binary}\n")
        path.chmod(0o755)
    tarball = staging / f"{extract_subdir}.tar.xz"
    with tarfile.open(tarball, "w:xz") as tf:
        tf.add(src_root, arcname=extract_subdir)
    return tarball


def test_userspace_install_extracts_and_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end: a fake tarball is extracted and binaries are symlinked into BIN_DIR. The download, the digest and
    the links are aisland's own mechanics: the debug log says them, the output shows the steps and the result."""
    bin_dir = tmp_path / "bin"
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(paths, "bin_dir", lambda: bin_dir)
    monkeypatch.setattr(paths, "tools_dir", lambda: tools_dir)

    # Build a fake upstream tarball mirroring the real Node layout.
    spec = installers.USERSPACE_TOOLS["node"]
    extract_subdir = _subdir(spec)
    staging = tmp_path / "upstream"
    staging.mkdir()
    fake_tarball = _build_fake_node_tarball(staging, extract_subdir)

    # Pin the fake tarball's real digest so the integrity gate passes (and this
    # test also exercises the happy path of verification).
    digest = hashlib.sha256(fake_tarball.read_bytes()).hexdigest()
    monkeypatch.setitem(installers.USERSPACE_TOOLS["node"], "sha256", {_current_arch(): digest})

    # Patch urlretrieve to copy the local tarball instead of hitting the network.
    def fake_urlretrieve(url: str, dst: Path) -> None:
        dst.write_bytes(fake_tarball.read_bytes())

    monkeypatch.setattr(installers, "download", fake_urlretrieve)

    with caplog.at_level(logging.DEBUG, logger="aisland"):
        installers.install_userspace_tool("node")

    out = capsys.readouterr().out
    assert "sha256 OK" not in out and "Cached at" not in out and "linked" not in out
    assert f"==> Downloading node {spec['version']}" in out and f"==> node {spec['version']} installed under" in out
    (tarball,) = (tools_dir / "cache").iterdir()
    assert f"cached at {tarball}" in caplog.messages
    assert f"verified {tarball.name} (sha256 OK)" in caplog.messages
    for binary in ("node", "npm", "npx"):
        assert any(message.startswith(f"linked {bin_dir / binary} -> ") for message in caplog.messages)
    for binary in ("node", "npm", "npx"):
        link = bin_dir / binary
        assert link.is_symlink(), f"{binary} should be a symlink in {bin_dir}"
        target = Path(os.readlink(link))
        assert target.name == binary
        assert extract_subdir in str(target)


def test_userspace_install_refuses_to_clobber_real_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """A pre-existing real file (not a symlink) in BIN_DIR must NOT be overwritten."""
    bin_dir = tmp_path / "bin"
    tools_dir = tmp_path / "tools"
    bin_dir.mkdir()
    monkeypatch.setattr(paths, "bin_dir", lambda: bin_dir)
    monkeypatch.setattr(paths, "tools_dir", lambda: tools_dir)

    # One of the binaries is a real file the user wrote themselves — leave it alone.
    (bin_dir / "node").write_text("USER-OWNED CONTENT")

    spec = installers.USERSPACE_TOOLS["node"]
    extract_subdir = _subdir(spec)
    staging = tmp_path / "upstream"
    staging.mkdir()
    fake_tarball = _build_fake_node_tarball(staging, extract_subdir)

    digest = hashlib.sha256(fake_tarball.read_bytes()).hexdigest()
    monkeypatch.setitem(installers.USERSPACE_TOOLS["node"], "sha256", {_current_arch(): digest})

    def fake_urlretrieve(url: str, dst: Path) -> None:
        dst.write_bytes(fake_tarball.read_bytes())

    monkeypatch.setattr(installers, "download", fake_urlretrieve)

    installers.install_userspace_tool("node")

    # node was untouched; npm/npx were symlinked.
    assert (bin_dir / "node").read_text() == "USER-OWNED CONTENT"
    assert not (bin_dir / "node").is_symlink()
    assert (bin_dir / "npm").is_symlink()
    assert (bin_dir / "npx").is_symlink()
    assert "refusing to overwrite" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Checksum verification of userspace downloads.
# ---------------------------------------------------------------------------


def test_userspace_install_rejects_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """A tarball whose sha256 != the pinned digest must NOT be extracted/installed,
    and the bad tarball is deleted so a re-run re-downloads."""
    bin_dir = tmp_path / "bin"
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(paths, "bin_dir", lambda: bin_dir)
    monkeypatch.setattr(paths, "tools_dir", lambda: tools_dir)

    spec = installers.USERSPACE_TOOLS["node"]
    extract_subdir = _subdir(spec)
    staging = tmp_path / "upstream"
    staging.mkdir()
    fake_tarball = _build_fake_node_tarball(staging, extract_subdir)

    # Pin a digest the fake tarball cannot match.
    monkeypatch.setitem(installers.USERSPACE_TOOLS["node"], "sha256", {_current_arch(): "0" * 64})

    def fake_urlretrieve(url: str, dst: Path) -> None:
        dst.write_bytes(fake_tarball.read_bytes())

    monkeypatch.setattr(installers, "download", fake_urlretrieve)

    installers.install_userspace_tool("node")

    assert "checksum mismatch" in capsys.readouterr().err
    # Nothing was symlinked...
    for binary in ("node", "npm", "npx"):
        assert not (bin_dir / binary).exists()
    # ...and the corrupt tarball was removed.
    cache = tools_dir / "cache"
    assert not list(cache.glob("*.tar.xz")) if cache.exists() else True


def test_userspace_install_rejects_tampered_cached_tarball(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A pre-existing (cached) tarball is NOT trusted just because it is on disk —
    it is verified too, so a poisoned cache is rejected without re-downloading."""
    bin_dir = tmp_path / "bin"
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(paths, "bin_dir", lambda: bin_dir)
    monkeypatch.setattr(paths, "tools_dir", lambda: tools_dir)

    spec = installers.USERSPACE_TOOLS["node"]
    tarball_name = f"{_subdir(spec)}.tar.xz"
    cache = tools_dir / "cache"
    cache.mkdir(parents=True)
    (cache / tarball_name).write_bytes(b"TAMPERED-NOT-A-REAL-TARBALL")

    # Pin a real-looking digest that the tampered bytes cannot match.
    monkeypatch.setitem(installers.USERSPACE_TOOLS["node"], "sha256", {_current_arch(): "a" * 64})

    def fail_urlretrieve(url: str, dst: Path) -> None:
        raise AssertionError("must not re-download a cached tarball before verifying it")

    monkeypatch.setattr(installers, "download", fail_urlretrieve)

    with caplog.at_level(logging.DEBUG, logger="aisland"):
        installers.install_userspace_tool("node")

    captured = capsys.readouterr()
    assert f"using cached tarball {cache / tarball_name}" in caplog.messages
    assert "Using cached tarball" not in captured.out
    assert "checksum mismatch" in captured.err
    assert not (cache / tarball_name).exists()


def test_userspace_install_warns_when_no_digest_pinned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """An unpinned tool still installs (backward-compatible) but loudly warns."""
    bin_dir = tmp_path / "bin"
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(paths, "bin_dir", lambda: bin_dir)
    monkeypatch.setattr(paths, "tools_dir", lambda: tools_dir)

    spec = installers.USERSPACE_TOOLS["node"]
    extract_subdir = _subdir(spec)
    staging = tmp_path / "upstream"
    staging.mkdir()
    fake_tarball = _build_fake_node_tarball(staging, extract_subdir)

    # No digest for this arch → verification skipped with a warning.
    monkeypatch.setitem(installers.USERSPACE_TOOLS["node"], "sha256", {})

    def fake_urlretrieve(url: str, dst: Path) -> None:
        dst.write_bytes(fake_tarball.read_bytes())

    monkeypatch.setattr(installers, "download", fake_urlretrieve)

    installers.install_userspace_tool("node")

    assert "UNVERIFIED" in capsys.readouterr().err
    assert (bin_dir / "node").is_symlink()


def test_registry_pins_wellformed_sha256_for_every_tool() -> None:
    """Every shipped USERSPACE_TOOLS entry pins at least one 64-hex-char digest,
    guarding against a bad paste or a version bump that forgot to refresh them."""
    for name, spec in installers.USERSPACE_TOOLS.items():
        digests = spec.get("sha256", {})
        assert digests, f"{name} has no pinned sha256 digests"
        for arch, digest in digests.items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{name}/{arch} malformed digest {digest!r}"


# ---------------------------------------------------------------------------
# project.bashrc symlink wiring.
# ---------------------------------------------------------------------------


def test_a_clone_no_workspace_lists_copies_its_own_shell_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An editable clone of aisland kept outside any workspace works like a wheel, but no wheel put a copy in its
    package: setup copies the clone's own `project.bashrc`, beside the package."""
    clone = tmp_path / "code" / "aisland"
    (clone / "aisland").mkdir(parents=True)
    (clone / "project.bashrc").write_text("# the clone's shell init\n")
    workspace = tmp_path / "acme"
    workspace.mkdir()
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.setattr(locations, "PACKAGE_DIR", clone / "aisland")
    monkeypatch.setattr(locations, "CHECKOUT_DIR", clone)
    monkeypatch.setattr(paths, "state_dir", lambda: workspace / ".aisland")
    manager.link_project_bashrc(workspace)
    assert (workspace / "project.bashrc").read_text() == "# the clone's shell init\n"
    assert os.readlink(workspace / "project.bashrc") == ".aisland/project.bashrc"


@pytest.fixture
def from_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests below lay out a checkout (``<workspace>/aisland/project.bashrc``): aisland runs as one there."""
    monkeypatch.setattr(locations, "FROM_CHECKOUT", True)


@pytest.mark.usefixtures("from_a_checkout")
def test_link_project_bashrc_creates_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    """First-time setup: workspace gains a relative symlink to aisland/project.bashrc."""
    workspace = tmp_path / "ws"
    aisland_dir = workspace / "aisland"
    aisland_dir.mkdir(parents=True)
    target = aisland_dir / "project.bashrc"
    target.write_text("# fake aisland bashrc\n")

    monkeypatch.setattr(paths, "state_dir", lambda: aisland_dir)
    manager.link_project_bashrc(workspace)

    link = workspace / "project.bashrc"
    assert link.is_symlink()
    assert os.readlink(link) == "aisland/project.bashrc"
    assert link.resolve() == target.resolve()


@pytest.mark.usefixtures("from_a_checkout")
def test_link_project_bashrc_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any) -> None:
    """Re-running setup with the correct symlink in place is a no-op (no log noise)."""
    workspace = tmp_path / "ws"
    aisland_dir = workspace / "aisland"
    aisland_dir.mkdir(parents=True)
    target = aisland_dir / "project.bashrc"
    target.write_text("# fake aisland bashrc\n")
    (workspace / "project.bashrc").symlink_to("aisland/project.bashrc")

    monkeypatch.setattr(paths, "state_dir", lambda: aisland_dir)
    manager.link_project_bashrc(workspace)

    assert capsys.readouterr().out == ""


@pytest.mark.usefixtures("from_a_checkout")
def test_link_project_bashrc_refuses_to_clobber_real_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """If the user has a hand-edited project.bashrc, never overwrite it."""
    workspace = tmp_path / "ws"
    aisland_dir = workspace / "aisland"
    aisland_dir.mkdir(parents=True)
    target = aisland_dir / "project.bashrc"
    target.write_text("# fake aisland bashrc\n")
    real_file = workspace / "project.bashrc"
    real_file.write_text("# USER's hand-edited bashrc\n")

    monkeypatch.setattr(paths, "state_dir", lambda: aisland_dir)
    manager.link_project_bashrc(workspace)

    assert real_file.read_text() == "# USER's hand-edited bashrc\n"
    assert not real_file.is_symlink()
    assert "exists as a regular file" in capsys.readouterr().err


@pytest.mark.usefixtures("from_a_checkout")
def test_link_project_bashrc_replaces_stale_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A symlink pointing somewhere else gets repointed to aisland/project.bashrc."""
    workspace = tmp_path / "ws"
    aisland_dir = workspace / "aisland"
    aisland_dir.mkdir(parents=True)
    target = aisland_dir / "project.bashrc"
    target.write_text("# fake aisland bashrc\n")
    stale_target = workspace / "old-bashrc"
    stale_target.write_text("# stale\n")
    (workspace / "project.bashrc").symlink_to("old-bashrc")

    monkeypatch.setattr(paths, "state_dir", lambda: aisland_dir)
    manager.link_project_bashrc(workspace)

    link = workspace / "project.bashrc"
    assert link.resolve() == target.resolve()


@pytest.mark.parametrize("machine, asset", [("x86_64", "linux-x64"), ("aarch64", "linux-arm64")])
def test_node_downloads_the_build_of_this_machines_architecture_and_verifies_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine: str, asset: str
) -> None:
    """Node names its builds x64 and arm64; an ARM machine gets the arm64 build, checked against its own digest."""
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(paths, "bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(paths, "tools_dir", lambda: tmp_path / "tools")
    fetched = []

    def record(url: str, dst: Path) -> None:
        fetched.append(url)
        dst.write_bytes(b"not the real archive")

    monkeypatch.setattr(installers, "download", record)
    installers.install_userspace_tool("node")
    assert fetched == [f"https://nodejs.org/dist/v22.11.0/node-v22.11.0-{asset}.tar.xz"]
    assert set(installers.USERSPACE_TOOLS["node"]["sha256"]) == {"amd64", "arm64"}


def test_an_architecture_aisland_has_no_build_for_is_refused_before_any_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(paths, "bin_dir", lambda: tmp_path / "bin")
    monkeypatch.setattr(paths, "tools_dir", lambda: tmp_path / "tools")

    def explode(url: str, dst: Path) -> None:
        raise AssertionError("nothing is downloaded for an unknown architecture")

    monkeypatch.setattr(installers, "download", explode)
    installers.install_userspace_tool("node")
    assert (
        "Error: no node build for riscv64 (aisland has amd64, arm64); install node yourself." in capsys.readouterr().err
    )


def test_a_half_unpacked_folder_from_an_interrupted_run_is_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unpack killed half way left `tools/<tool>/` without its binaries; the next run unpacks again, whole."""
    bin_dir, tools_dir = tmp_path / "bin", tmp_path / "tools"
    monkeypatch.setattr(paths, "bin_dir", lambda: bin_dir)
    monkeypatch.setattr(paths, "tools_dir", lambda: tools_dir)
    spec = installers.USERSPACE_TOOLS["node"]
    staging = tmp_path / "upstream"
    staging.mkdir()
    fake_tarball = _build_fake_node_tarball(staging, _subdir(spec))
    monkeypatch.setitem(spec, "sha256", {_current_arch(): hashlib.sha256(fake_tarball.read_bytes()).hexdigest()})
    monkeypatch.setattr(installers, "download", lambda url, dst: dst.write_bytes(fake_tarball.read_bytes()))
    (tools_dir / _subdir(spec) / "bin").mkdir(parents=True)  # what the interrupted run left: no binaries
    installers.install_userspace_tool("node")
    assert all((bin_dir / binary).is_symlink() for binary in ("node", "npm", "npx"))
    assert all((tools_dir / _subdir(spec) / "bin" / binary).is_file() for binary in ("node", "npm", "npx"))
    assert sorted(path.name for path in tools_dir.iterdir()) == sorted(["cache", _subdir(spec)])
