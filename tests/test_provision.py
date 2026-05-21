"""Tests for aisland.manager.provision_system and shell-switch offer.

Covers the deterministic early-return branches of `_maybe_offer_shell_switch`.
The interactive sudo / chsh paths are not exercised here — they require
mocking subprocess + tty + interactive prompts and the behavior is shell-
specific in a way unit tests can't usefully verify.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path
from typing import Any

import pytest

from aisland import manager


def test_shell_switch_skipped_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """On Linux / Windows the chsh flow is irrelevant — must early-return silently."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Linux", raising=False)
    manager._maybe_offer_shell_switch({"bash"})
    assert capsys.readouterr().out == ""


def test_shell_switch_skipped_when_bash_not_required(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """If `bash` is not in the project's sys-deps, the offer must not appear."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    manager._maybe_offer_shell_switch({"libomp", "openjdk"})
    assert capsys.readouterr().out == ""


def test_shell_switch_skipped_when_brew_bash_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """If neither /opt/homebrew/bin/bash nor /usr/local/bin/bash exists, warn + skip."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    manager._maybe_offer_shell_switch({"bash"})
    out = capsys.readouterr().out
    assert "Homebrew bash not found" in out


def test_shell_switch_silent_when_already_current(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """If SHELL already points to brew bash, no offer should appear (idempotency)."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    # Pretend /opt/homebrew/bin/bash exists, /usr/local one doesn't.
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/opt/homebrew/bin/bash":
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/bash")
    manager._maybe_offer_shell_switch({"bash"})
    assert capsys.readouterr().out == ""


def test_shell_switch_declined_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """User declines the prompt — function must return without touching the system."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/opt/homebrew/bin/bash":
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setenv("SHELL", "/bin/bash")
    # click.confirm reads from stdin; mock it to "no".
    monkeypatch.setattr(manager.click, "confirm", lambda *a, **kw: False)

    # Spy subprocess.run/check_call so we can assert nothing was invoked.
    calls: list = []

    def _record(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("subprocess should NOT be invoked when user declines")

    monkeypatch.setattr(manager.subprocess, "run", _record)
    monkeypatch.setattr(manager.subprocess, "check_call", _record)

    manager._maybe_offer_shell_switch({"bash"})
    out = capsys.readouterr().out
    assert "Modern bash is installed" in out
    assert calls == []


# ---------------------------------------------------------------------------
# Cross-platform provisioning: brew vs userspace dispatch.
# ---------------------------------------------------------------------------


def _write_pyproject(
    path: Path, *, brew: list[str] | None = None, userspace: list[str] | None = None
) -> None:
    """Write a minimal pyproject.toml declaring sys-dependencies."""
    lines = [
        "[project]\nname = 'fake'\nversion = '0.0.0'\n",
        "[tool.aisland.sys-dependencies]\n",
    ]
    if brew is not None:
        lines.append(f"brew = {brew!r}\n")
    if userspace is not None:
        lines.append(f"userspace = {userspace!r}\n")
    path.write_text("".join(lines))


def _make_fake_workspace(tmp_path: Path) -> Path:
    """Build a workspace skeleton with a single fake project containing a pyproject."""
    workspace = tmp_path / "ws"
    (workspace / "proj").mkdir(parents=True)
    (workspace / ".git").mkdir()  # makes get_workspace_root anchor here
    return workspace


def test_provision_dispatches_to_brew_on_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """Darwin: only brew installer runs; userspace deps are ignored."""
    workspace = _make_fake_workspace(tmp_path)
    _write_pyproject(
        workspace / "proj" / "pyproject.toml", brew=["node"], userspace=["node"]
    )

    monkeypatch.setattr(manager, "get_workspace_root", lambda: workspace)
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)

    brew_calls: list = []
    user_calls: list = []
    monkeypatch.setattr(
        manager, "_install_brew_packages", lambda pkgs: brew_calls.append(set(pkgs))
    )
    monkeypatch.setattr(
        manager, "_install_userspace_tool", lambda name: user_calls.append(name)
    )
    monkeypatch.setattr(manager, "_maybe_offer_shell_switch", lambda pkgs: None)

    manager.provision_system()

    assert brew_calls == [{"node"}]
    assert user_calls == []


def test_provision_dispatches_to_userspace_on_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Linux: only userspace installer runs; brew deps are ignored."""
    workspace = _make_fake_workspace(tmp_path)
    _write_pyproject(
        workspace / "proj" / "pyproject.toml", brew=["node"], userspace=["node"]
    )

    monkeypatch.setattr(manager, "get_workspace_root", lambda: workspace)
    monkeypatch.setattr(manager.platform, "system", lambda: "Linux", raising=False)

    brew_calls: list = []
    user_calls: list = []
    monkeypatch.setattr(
        manager, "_install_brew_packages", lambda pkgs: brew_calls.append(set(pkgs))
    )
    monkeypatch.setattr(
        manager, "_install_userspace_tool", lambda name: user_calls.append(name)
    )

    manager.provision_system()

    assert brew_calls == []
    assert user_calls == ["node"]


def test_provision_no_deps_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """If no projects declare sys-deps, no installers should run."""
    workspace = _make_fake_workspace(tmp_path)
    (workspace / "proj" / "pyproject.toml").write_text(
        "[project]\nname='fake'\nversion='0.0.0'\n"
    )

    monkeypatch.setattr(manager, "get_workspace_root", lambda: workspace)
    monkeypatch.setattr(manager.platform, "system", lambda: "Linux", raising=False)

    user_calls: list = []
    monkeypatch.setattr(
        manager, "_install_userspace_tool", lambda name: user_calls.append(name)
    )

    manager.provision_system()

    assert user_calls == []
    assert "No system dependencies found" in capsys.readouterr().out


def test_provision_unknown_platform_skips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """Unsupported platform → log + skip, never raise."""
    workspace = _make_fake_workspace(tmp_path)
    _write_pyproject(workspace / "proj" / "pyproject.toml", brew=["node"])

    monkeypatch.setattr(manager, "get_workspace_root", lambda: workspace)
    monkeypatch.setattr(manager.platform, "system", lambda: "Windows", raising=False)

    manager.provision_system()
    assert "not implemented for platform 'Windows'" in capsys.readouterr().out


def test_userspace_install_unknown_tool_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A tool name not in the registry must be reported, not silently skipped."""
    manager._install_userspace_tool("definitely-not-a-real-tool")
    assert "not a known userspace tool" in capsys.readouterr().out


def test_userspace_install_skips_when_already_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """Idempotency: if all target symlinks already exist, no download is attempted."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Pre-create all three symlinks so the idempotency check fires.
    for name in ("node", "npm", "npx"):
        (bin_dir / name).write_text("stub")

    monkeypatch.setattr(manager, "BIN_DIR", bin_dir)
    monkeypatch.setattr(manager, "TOOLS_DIR", tmp_path / "tools")

    def _explode(*a: Any, **kw: Any) -> Any:
        raise AssertionError("urlretrieve should not be called when already installed")

    monkeypatch.setattr(manager.urllib.request, "urlretrieve", _explode)

    manager._install_userspace_tool("node")
    assert "already installed" in capsys.readouterr().out


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """End-to-end: a fake tarball is extracted and binaries are symlinked into BIN_DIR."""
    bin_dir = tmp_path / "bin"
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(manager, "BIN_DIR", bin_dir)
    monkeypatch.setattr(manager, "TOOLS_DIR", tools_dir)

    # Build a fake upstream tarball mirroring the real Node layout.
    spec = manager.USERSPACE_TOOLS["node"]
    extract_subdir = spec["extract_subdir"].format(version=spec["version"])
    staging = tmp_path / "upstream"
    staging.mkdir()
    fake_tarball = _build_fake_node_tarball(staging, extract_subdir)

    # Patch urlretrieve to copy the local tarball instead of hitting the network.
    def fake_urlretrieve(url: str, dst: str) -> tuple[str, Any]:
        Path(dst).write_bytes(fake_tarball.read_bytes())
        return (dst, None)

    monkeypatch.setattr(manager.urllib.request, "urlretrieve", fake_urlretrieve)

    manager._install_userspace_tool("node")

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
    monkeypatch.setattr(manager, "BIN_DIR", bin_dir)
    monkeypatch.setattr(manager, "TOOLS_DIR", tools_dir)

    # One of the binaries is a real file the user wrote themselves — leave it alone.
    (bin_dir / "node").write_text("USER-OWNED CONTENT")

    spec = manager.USERSPACE_TOOLS["node"]
    extract_subdir = spec["extract_subdir"].format(version=spec["version"])
    staging = tmp_path / "upstream"
    staging.mkdir()
    fake_tarball = _build_fake_node_tarball(staging, extract_subdir)

    def fake_urlretrieve(url: str, dst: str) -> tuple[str, Any]:
        Path(dst).write_bytes(fake_tarball.read_bytes())
        return (dst, None)

    monkeypatch.setattr(manager.urllib.request, "urlretrieve", fake_urlretrieve)

    manager._install_userspace_tool("node")

    # node was untouched; npm/npx were symlinked.
    assert (bin_dir / "node").read_text() == "USER-OWNED CONTENT"
    assert not (bin_dir / "node").is_symlink()
    assert (bin_dir / "npm").is_symlink()
    assert (bin_dir / "npx").is_symlink()
    assert "refusing to overwrite" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# project.bashrc symlink wiring.
# ---------------------------------------------------------------------------


def test_link_project_bashrc_creates_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """First-time setup: workspace gains a relative symlink to aisland/project.bashrc."""
    workspace = tmp_path / "ws"
    aisland_dir = workspace / "aisland"
    aisland_dir.mkdir(parents=True)
    target = aisland_dir / "project.bashrc"
    target.write_text("# fake aisland bashrc\n")

    monkeypatch.setattr(manager, "BASE_DIR", aisland_dir)
    manager._link_project_bashrc(workspace)

    link = workspace / "project.bashrc"
    assert link.is_symlink()
    assert os.readlink(link) == "aisland/project.bashrc"
    assert link.resolve() == target.resolve()


def test_link_project_bashrc_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """Re-running setup with the correct symlink in place is a no-op (no log noise)."""
    workspace = tmp_path / "ws"
    aisland_dir = workspace / "aisland"
    aisland_dir.mkdir(parents=True)
    target = aisland_dir / "project.bashrc"
    target.write_text("# fake aisland bashrc\n")
    (workspace / "project.bashrc").symlink_to("aisland/project.bashrc")

    monkeypatch.setattr(manager, "BASE_DIR", aisland_dir)
    manager._link_project_bashrc(workspace)

    assert capsys.readouterr().out == ""


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

    monkeypatch.setattr(manager, "BASE_DIR", aisland_dir)
    manager._link_project_bashrc(workspace)

    assert real_file.read_text() == "# USER's hand-edited bashrc\n"
    assert not real_file.is_symlink()
    assert "exists as a regular file" in capsys.readouterr().out


def test_link_project_bashrc_replaces_stale_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symlink pointing somewhere else gets repointed to aisland/project.bashrc."""
    workspace = tmp_path / "ws"
    aisland_dir = workspace / "aisland"
    aisland_dir.mkdir(parents=True)
    target = aisland_dir / "project.bashrc"
    target.write_text("# fake aisland bashrc\n")
    stale_target = workspace / "old-bashrc"
    stale_target.write_text("# stale\n")
    (workspace / "project.bashrc").symlink_to("old-bashrc")

    monkeypatch.setattr(manager, "BASE_DIR", aisland_dir)
    manager._link_project_bashrc(workspace)

    link = workspace / "project.bashrc"
    assert link.resolve() == target.resolve()
