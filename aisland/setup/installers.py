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
from typing import AbstractSet, Dict, List, NotRequired, Optional, TypedDict

from aisland import console, failures, locations, paths
from aisland.downloads import download, sha256
from aisland.errors import AislandError

logger = logging.getLogger(__name__)


class UserspaceTool(TypedDict):
    """One entry of :data:`USERSPACE_TOOLS`: a pinned release and how to install it.

    ``url`` and ``extract_subdir`` are templates of ``{version}`` and ``{arch}`` (the architecture in the tool's own
    names, ``arch_names``, when they differ from aisland's ``amd64``/``arm64``); ``bin_subdir`` is the binaries'
    folder inside the archive (``""``: at its root; default ``bin``); ``sha256`` maps aisland's architecture names to
    the archive's digest."""

    version: str
    url: str
    extract_subdir: str
    binaries: List[str]
    sha256: Dict[str, str]
    arch_names: NotRequired[Dict[str, str]]
    bin_subdir: NotRequired[str]


# Registry of tools aisland can install entirely in userspace (no sudo / package
# manager required). Used on Linux where the macOS brew flow is unavailable.
# Each entry pins a specific version AND per-architecture sha256 digests taken
# from the upstream SHASUMS / checksums files. The downloaded tarball is verified
# against its digest BEFORE extraction (see `install_userspace_tool`); a mismatch
# aborts the install rather than running an unverified binary. `sha256` is keyed
# by the same {arch} token used in the URL — node ships an x64-only asset, so it
# is keyed "amd64". When bumping `version`, refresh the digests from upstream in
# the SAME edit (e.g. `curl -sL <checksums-url>`), or the install will abort.
USERSPACE_TOOLS: Dict[str, UserspaceTool] = {
    "node": {
        "version": "v22.11.0",
        "url": "https://nodejs.org/dist/{version}/node-{version}-linux-{arch}.tar.xz",
        "extract_subdir": "node-{version}-linux-{arch}",
        # Node names its builds x64 and arm64 (`{arch}` in its asset names); the digests stay keyed by aisland's names.
        "arch_names": {"amd64": "x64", "arm64": "arm64"},
        "binaries": ["node", "npm", "npx"],
        # https://nodejs.org/dist/v22.11.0/SHASUMS256.txt
        "sha256": {
            "amd64": "83bf07dd343002a26211cf1fcd46a9d9534219aad42ee02847816940bf610a72",
            "arm64": "6031d04b98f59ff0f7cb98566f65b115ecd893d3b7870821171708cdbaf7ae6e",
        },
    },
    # The aisland service supervisor (see aisland/supervisor.py). macOS gets it
    # via Homebrew (f1bonacc1/tap, in aisland.setup.provision.AISLAND_TOOLS);
    # this entry covers Linux. Flat archive: the binary sits at the tarball
    # root (bin_subdir "" — no bin/ directory), and upstream publishes
    # per-architecture assets ({arch} = amd64 / arm64).
    "process-compose": {
        "version": "v1.116.0",
        "url": (
            "https://github.com/F1bonacc1/process-compose/releases/download/"
            "{version}/process-compose_linux_{arch}.tar.gz"
        ),
        "extract_subdir": "process-compose-{version}",
        "bin_subdir": "",
        "binaries": ["process-compose"],
        # github.com/F1bonacc1/process-compose/releases/download/v1.116.0/process-compose_checksums.txt
        "sha256": {
            "amd64": "f3550750dd17c3d0d5e378aa6239b87ee8405dd0eeb460a83b8cc03ef48a87fb",
            "arm64": "b82c4d6c633b4775d6f11c41dc5f7943f7b94a99ff6699298de813cb9c9ba76d",
        },
    },
}


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
        except subprocess.CalledProcessError as e:
            console.echo(f"  - Failed to update {shells_path}: {e}")
            return

    console.echo("==> Running chsh (will prompt for your USER password, not sudo)...")
    try:
        subprocess.check_call(["chsh", "-s", new_bash])
        console.echo(f"  - Login shell switched to {new_bash}.")
        console.echo("  - Open a new terminal window for it to take effect.")
    except subprocess.CalledProcessError as e:
        console.echo(f"  - chsh failed: {e}")


def ensure_uv_in_venv(venv_bin: Path) -> str:
    """Guarantee ``uv`` is present in the workspace venv and return its path.

    Every Jenkins .local job, every ``aisland setup`` run, and every call into the
    workspace's Python tools assumes ``${VENV_BIN}/uv`` exists. If the venv was
    created without uv (or uv got uninstalled by some downstream ``uv pip install``
    shenanigan), this function installs it via the venv's own ``pip``.

    Idempotent: if uv is already executable, returns its path immediately.
    """
    uv_path = venv_bin / "uv"
    if uv_path.exists() and os.access(uv_path, os.X_OK):
        return str(uv_path)

    pip_path = venv_bin / "pip"
    if not pip_path.exists():
        # A venv made by `uv venv` without `--seed` has no pip (setup.sh seeds one with ensurepip).
        remedy = "Re-run aisland/setup.sh." if locations.FROM_CHECKOUT else "Recreate it with `uv venv --seed .venv`."
        raise AislandError(f"{pip_path} not found. Cannot bootstrap uv; the venv is broken. {remedy}")

    console.echo(f"==> Bootstrapping uv into {venv_bin}...")
    logger.debug("running %s install --quiet --upgrade uv", pip_path)
    subprocess.check_call([str(pip_path), "install", "--quiet", "--upgrade", "uv"])
    return str(uv_path)
