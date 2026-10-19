"""`aisland doctor`'s `torch-ext-lock` check: a build lock a killed compile left behind is named with its `rm`.

The check reads where torch builds (`$TORCH_EXTENSIONS_DIR`, else torch's per-user cache) without importing
torch; a lock older than the bound fails with the exact command, a younger one is a build in progress.
"""

import os
import time
from pathlib import Path

import pytest

from aisland import doctor


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """torch's extension root for the test: `$TORCH_EXTENSIONS_DIR`."""
    where = tmp_path / "torch_extensions"
    monkeypatch.setenv("TORCH_EXTENSIONS_DIR", str(where))
    return where


def _lock(root: Path, *parts: str, age_seconds: float) -> Path:
    lock = root.joinpath(*parts) / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    stamp = time.time() - age_seconds
    os.utime(lock, (stamp, stamp))
    return lock


class TestTorchExtensionLock:
    def test_registered(self) -> None:
        assert doctor.check_torch_extension_lock in doctor.CHECKS

    def test_no_root_yet_is_healthy(self, root: Path) -> None:
        result = doctor.check_torch_extension_lock()
        assert result.ok
        assert str(root) in result.detail

    def test_no_lock_is_healthy(self, root: Path) -> None:
        (root / "fast_kernels").mkdir(parents=True)
        (root / "fast_kernels" / "fast_kernels.so").touch()
        assert doctor.check_torch_extension_lock().ok

    def test_a_stale_lock_fails_with_the_rm_command(self, root: Path) -> None:
        lock = _lock(root, "fast_kernels", age_seconds=doctor._TORCH_LOCK_STALE_SECONDS + 3000)
        result = doctor.check_torch_extension_lock()
        assert not result.ok
        assert result.name == "torch-ext-lock"
        assert "fast_kernels" in result.detail
        assert str(lock) in result.detail
        assert "waits on it forever" in result.detail
        assert result.remedy == f"rm '{lock}'"

    def test_a_young_lock_is_a_build_in_progress_not_a_failure(self, root: Path) -> None:
        _lock(root, "fast_kernels", age_seconds=5)
        result = doctor.check_torch_extension_lock()
        assert result.ok
        assert "building" in result.detail
        assert "fast_kernels" in result.detail

    def test_the_default_layout_one_folder_deeper_is_searched_too(self, root: Path) -> None:
        """Without `TORCH_EXTENSIONS_DIR` torch builds under `<root>/py313_cpu/<ext>/`."""
        lock = _lock(root, "py313_cpu", "fast_kernels", age_seconds=600)
        result = doctor.check_torch_extension_lock()
        assert not result.ok
        assert result.remedy == f"rm '{lock}'"

    def test_several_stale_locks_go_in_one_rm(self, root: Path) -> None:
        older = _lock(root, "py313_cpu", "ext_a", age_seconds=900)
        younger = _lock(root, "py313_cpu", "ext_b", age_seconds=600)
        result = doctor.check_torch_extension_lock()
        assert not result.ok
        assert result.remedy == f"rm '{older}' '{younger}'"
        assert "ext_a, ext_b" in result.detail

    def test_a_live_build_beside_a_stale_lock_is_not_removed(self, root: Path) -> None:
        stale = _lock(root, "ext_a", age_seconds=900)
        live = _lock(root, "ext_b", age_seconds=3)
        result = doctor.check_torch_extension_lock()
        assert not result.ok
        assert result.remedy == f"rm '{stale}'"
        assert str(live) not in result.remedy


class TestTorchExtensionsRoot:
    def test_the_variable_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TORCH_EXTENSIONS_DIR", str(tmp_path / "ext"))
        assert doctor._torch_extensions_root() == tmp_path / "ext"

    def test_macos_uses_the_library_caches(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TORCH_EXTENSIONS_DIR", raising=False)
        monkeypatch.setattr(doctor.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert doctor._torch_extensions_root() == tmp_path / "Library" / "Caches" / "torch_extensions"

    def test_linux_uses_xdg_cache_home_then_dot_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TORCH_EXTENSIONS_DIR", raising=False)
        monkeypatch.setattr(doctor.sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert doctor._torch_extensions_root() == tmp_path / "xdg" / "torch_extensions"
        monkeypatch.delenv("XDG_CACHE_HOME")
        assert doctor._torch_extensions_root() == tmp_path / ".cache" / "torch_extensions"

    def test_windows_uses_localappdata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TORCH_EXTENSIONS_DIR", raising=False)
        monkeypatch.setattr(doctor.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert doctor._torch_extensions_root() == tmp_path / "Local" / "torch_extensions" / "torch_extensions" / "Cache"
        monkeypatch.delenv("LOCALAPPDATA")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert doctor._torch_extensions_root() == (
            tmp_path / "AppData" / "Local" / "torch_extensions" / "torch_extensions" / "Cache"
        )

    def test_the_real_torch_agrees_on_this_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The torch-free rule names the folder torch itself builds in (skipped where torch is absent)."""
        cpp_extension = pytest.importorskip("torch.utils.cpp_extension")
        monkeypatch.delenv("TORCH_EXTENSIONS_DIR", raising=False)
        assert cpp_extension.get_default_build_root() == str(doctor._torch_extensions_root().resolve())
