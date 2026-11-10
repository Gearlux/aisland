"""Installing system tools: Homebrew packages on macOS, and on Linux the pinned userspace archives
(:data:`USERSPACE_TOOLS`), each checked against its sha256 before it is unpacked. Which tools a machine needs is
:mod:`aisland.setup.provision`'s."""

import logging
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, List, Optional

from aisland import console, failures, paths
from aisland.downloads import download, sha256
from aisland.userspace_tools import USERSPACE_TOOLS

logger = logging.getLogger(__name__)


# platform.machine() → the architecture token used in release-asset names.
_LINUX_ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def install_brew_packages(packages: AbstractSet[str]) -> None:
    """Install the given packages via Homebrew (macOS).

    Silent no-op if ``brew`` is not on PATH. Idempotent — brew skips packages
    that are already installed.
    """
    try:
        subprocess.check_call(["brew", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        failures.report("  - Error: 'brew' command not found. Homebrew is required for system provisioning.")
        return

    env = os.environ.copy()
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

    for pkg in sorted(packages):
        console.echo(f"==> Installing {pkg} via Homebrew...")
        try:
            logger.debug("running brew install --quiet %s (HOMEBREW_NO_AUTO_UPDATE=1)", pkg)
            subprocess.check_call(["brew", "install", "--quiet", pkg], env=env)
            console.echo(f"  - {pkg} installed successfully.")
        except subprocess.CalledProcessError as e:
            failures.report(f"  - Error: Failed to install {pkg}: {e}")


@dataclass(frozen=True)
class _Release:
    """One :data:`USERSPACE_TOOLS` entry resolved for this machine: the archive, and what it unpacks into."""

    name: str
    version: str
    arch: str
    url: str
    #: The folder under ``tools/`` the archive unpacks into.
    extract_subdir: str
    #: The binaries' folder inside it; ``""`` for a flat archive, whose binaries sit at its root.
    bin_subdir: str
    binaries: List[str]
    #: The pinned digest for this architecture, ``None`` when the entry has none yet.
    sha256: Optional[str]

    @property
    def extract_root(self) -> Path:
        return paths.tools_dir() / self.extract_subdir

    @property
    def bin_dir(self) -> Path:
        return self.extract_root / self.bin_subdir if self.bin_subdir else self.extract_root


def _release(name: str) -> Optional[_Release]:
    """``name``'s release for this machine's architecture; ``None`` (reported) for an unknown tool or architecture."""
    spec = USERSPACE_TOOLS.get(name)
    if spec is None:
        failures.report(f"  - Error: '{name}' is not a known userspace tool. Known: {sorted(USERSPACE_TOOLS)}")
        return None
    version = spec["version"]
    machine = platform.machine().lower()
    arch = _LINUX_ARCH_ALIASES.get(machine)
    if arch is None:
        known = ", ".join(sorted(set(_LINUX_ARCH_ALIASES.values())))
        failures.report(f"  - Error: no {name} build for {machine} (aisland has {known}); install {name} yourself.")
        return None
    asset_arch = spec.get("arch_names", {}).get(arch, arch)
    return _Release(
        name=name,
        version=version,
        arch=arch,
        url=spec["url"].format(version=version, arch=asset_arch),
        extract_subdir=spec["extract_subdir"].format(version=version, arch=asset_arch),
        # "" means a flat archive: binaries at the archive root, no bin/ subdir.
        bin_subdir=spec.get("bin_subdir", "bin"),
        binaries=spec["binaries"],
        sha256=spec.get("sha256", {}).get(arch),
    )


def _fetch(release: _Release) -> Optional[Path]:
    """The release's archive in ``tools/cache/``, downloaded unless a non-empty copy is there; ``None`` (reported) when
    the download fails."""
    cache_dir = paths.tools_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tarball = cache_dir / Path(release.url).name
    if tarball.exists() and tarball.stat().st_size > 0:
        logger.debug("using cached tarball %s", tarball)
        return tarball
    console.echo(f"==> Downloading {release.name} {release.version} from {release.url}...")
    try:
        download(release.url, tarball)
    except OSError as e:
        failures.report(f"  - Error: Failed to download {release.url}: {e}")
        if tarball.exists():
            tarball.unlink()
        return None
    logger.debug("cached at %s", tarball)
    return tarball


def _verified(release: _Release, tarball: Path) -> bool:
    """Whether ``tarball`` may be unpacked: its sha256 is the pinned one, or no digest is pinned (warned).

    Checked BEFORE anything is extracted, for a fresh download and a reused cache entry alike (a poisoned cache must
    not be trusted just because it is on disk). A mismatch — a corrupt download or a tampered, re-uploaded asset — is
    reported and the tarball deleted, so a re-run fetches it again. A tool with no pinned digest for this arch installs
    UNVERIFIED with a loud warning, which keeps a not-yet-pinned addition working while making the gap visible.
    """
    if not release.sha256:
        failures.warn(
            f"  - Warning: no pinned sha256 for {release.name}/{release.arch}; installing UNVERIFIED. "
            f"Add a digest to USERSPACE_TOOLS['{release.name}']['sha256']['{release.arch}']."
        )
        return True
    actual = sha256(tarball)
    if actual.lower() != release.sha256.lower():
        failures.report(
            f"  - Error: checksum mismatch for {tarball.name} "
            f"(expected {release.sha256}, got {actual}). Refusing to install {release.name}; "
            f"deleting the tarball — re-run to re-download."
        )
        tarball.unlink()
        return False
    logger.debug("verified %s (sha256 OK)", tarball.name)
    return True


def _unpack(release: _Release, tarball: Path) -> bool:
    """Unpack ``tarball`` into the release's folder; whether it is there now (a failure is reported).

    It is unpacked into a scratch folder beside it and moved into place whole, so an unpack that fails or is
    interrupted leaves no half folder that later runs would take for a finished one. A flat archive has no top-level
    folder of its own and becomes the release's folder itself; a subdir-style one (node) brings its own.
    """
    console.echo(f"==> Extracting {tarball.name} into {release.extract_root}...")
    scratch = Path(tempfile.mkdtemp(prefix=f".{release.name}-", dir=paths.tools_dir()))
    try:
        with tarfile.open(tarball, "r:*") as tf:
            # `filter="data"` is the safe default in py3.14+ — block links/devices outside dest.
            tf.extractall(scratch, filter="data")
        unpacked = scratch if not release.bin_subdir else scratch / release.extract_subdir
        if not unpacked.is_dir():
            failures.report(f"  - Error: {tarball.name} holds no {release.extract_subdir}/ folder.")
            return False
        shutil.rmtree(release.extract_root, ignore_errors=True)
        unpacked.rename(release.extract_root)
        return True
    except (OSError, tarfile.TarError) as e:
        failures.report(f"  - Error: Failed to extract {tarball}: {e}")
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _link(release: _Release) -> None:
    """Link each of the release's binaries into ``bin/``: a stale link replaced, a real file never overwritten."""
    for binary in release.binaries:
        src = release.bin_dir / binary
        dst = paths.bin_dir() / binary
        if not src.exists():
            failures.warn(f"  - Warning: {src} missing in extracted tarball; skipping.")
            continue
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists():
            failures.report(f"  - Error: {dst} exists and is not a symlink; refusing to overwrite.")
            continue
        os.symlink(src, dst)
        logger.debug("linked %s -> %s", dst, src)


def install_userspace_tool(name: str) -> None:
    """Install a tool from :data:`USERSPACE_TOOLS` entirely in userspace.

    Downloads the upstream tarball into ``aisland/tools/cache/``, extracts it
    under ``aisland/tools/``, and symlinks the named binaries into
    ``aisland/bin/`` (which ``project.bashrc`` puts on ``PATH``). Idempotent:
    if every target binary symlink already exists, the download is skipped.

    Integrity: the tarball (freshly downloaded OR reused from the cache) is
    verified against the pinned per-arch sha256 in :data:`USERSPACE_TOOLS`
    before ANY extraction (:func:`_verified`).
    """
    release = _release(name)
    if release is None:
        return
    paths.bin_dir().mkdir(parents=True, exist_ok=True)
    if all((paths.bin_dir() / b).exists() for b in release.binaries):
        logger.debug("%s %s already installed (symlinks present in %s)", name, release.version, paths.bin_dir())
        return
    tarball = _fetch(release)
    if tarball is None or not _verified(release, tarball):
        return
    unpacked = all((release.bin_dir / binary).exists() for binary in release.binaries)
    if not unpacked and not _unpack(release, tarball):
        return
    if not release.bin_dir.is_dir():
        failures.report(f"  - Error: Expected {release.bin_dir} to exist after extraction.")
        return
    _link(release)
    console.echo(f"==> {name} {release.version} installed under {release.extract_root}.")
    console.echo(f"  - Ensure {paths.bin_dir()} is on PATH (project.bashrc handles this).")


def offer_shell_switch(required_pkgs: AbstractSet[str]) -> None:
    """If ``bash`` is declared as a sys-dep, offer to make brew's bash the login shell.

    No-op on non-macOS (chsh / /etc/shells flow is macOS-specific) and when
    ``bash`` is not among the required packages. Interactive: prompts before
    each side-effecting step (adding to ``/etc/shells`` via sudo, and ``chsh``
    itself which prompts the user for their own password).
    """
    if platform.system() != "Darwin":
        return
    if "bash" not in required_pkgs:
        return

    # brew installs to /opt/homebrew/bin/bash (Apple Silicon) or
    # /usr/local/bin/bash (Intel). Probe both.
    candidates = [Path("/opt/homebrew/bin/bash"), Path("/usr/local/bin/bash")]
    new_bash = next((str(p) for p in candidates if p.exists()), None)
    if not new_bash:
        failures.warn("  - Warning: Homebrew bash not found on disk; skipping shell-switch offer.")
        return

    current_shell = os.environ.get("SHELL", "")
    if current_shell == new_bash:
        return  # Already using brew bash — nothing to do.

    console.echo(
        f"\n==> Modern bash is installed at {new_bash}, but your login shell is {current_shell or '(unknown)'}."
    )
    if not console.confirm("    Switch your login shell to the new bash?", default=False):
        return

    # /etc/shells gate: chsh refuses any shell not listed there.
    shells_path = Path("/etc/shells")
    listed = False
    if shells_path.exists():
        listed = new_bash in shells_path.read_text().splitlines()
    if not listed:
        console.echo(f"  - {new_bash} is not in /etc/shells (chsh requires it).")
        if not console.confirm("    Add it now? (requires sudo)", default=True):
            console.echo("  - Skipping shell switch.")
            return
        try:
            # `sudo tee -a` is the canonical pattern for appending to a
            # root-owned file with a single password prompt.
            subprocess.run(
                ["sudo", "tee", "-a", str(shells_path)],
                input=f"{new_bash}\n",
                text=True,
                stdout=subprocess.DEVNULL,
                check=True,
            )
            console.echo(f"  - Added {new_bash} to {shells_path}.")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            failures.report(f"  - Error: could not add {new_bash} to {shells_path}: {e}")
            return

    console.echo("==> Running chsh (will prompt for your USER password, not sudo)...")
    try:
        subprocess.check_call(["chsh", "-s", new_bash])
        console.echo(f"  - Login shell switched to {new_bash}.")
        console.echo("  - Open a new terminal window for it to take effect.")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        failures.report(f"  - Error: chsh could not switch your login shell to {new_bash}: {e}")
