"""The standalone VS Code CLI the tunnel runs: where it comes from, and how it is unpacked into place.

The CLI is downloaded once per machine into the tunnel's state folder (:func:`aisland.tunnel.state.state_dir`), or
installed from an archive a box that cannot reach the download host was given. A download is checked against the
SHA-256 the VS Code update service lists for that build before anything is unpacked. It is unpacked to a ``.part``
file and moved into place whole, so an interrupted copy leaves no binary a later run would take for an installed one.
The build is this machine's: Linux's statically linked alpine CLI, or macOS's own (a ``.zip``, where Linux's is a
``.tar.gz``).
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import IO, Iterator, Optional, Tuple

from aisland import console, downloads
from aisland.errors import AislandError
from aisland.tunnel.state import state_dir

#: The VS Code update service's document of the latest stable standalone CLI: its archive's ``url`` and the
#: ``sha256hash`` it is checked against. On Linux the alpine builds are statically linked, so the same binary runs on a
#: glibc image; there is no ``cli-linux-*`` identifier (it answers 404). macOS has its own builds, ``cli-darwin-*``.
CLI_RELEASE_URL = "https://update.code.visualstudio.com/api/update/{asset}/stable/latest"
#: The build for each (operating system, architecture), as ``platform.system()`` and ``platform.machine()`` name them
#: (in lower case).
CLI_ASSETS = {
    ("linux", "x86_64"): "cli-alpine-x64",
    ("linux", "amd64"): "cli-alpine-x64",
    ("linux", "aarch64"): "cli-alpine-arm64",
    ("linux", "arm64"): "cli-alpine-arm64",
    ("darwin", "x86_64"): "cli-darwin-x64",
    ("darwin", "arm64"): "cli-darwin-arm64",
}


def cli_path() -> Path:
    """The standalone VS Code CLI this feature downloads for the REMOTE side."""
    return state_dir() / "code"


def cli_asset(machine: Optional[str] = None, system: Optional[str] = None) -> str:
    """The download identifier for this operating system and architecture; a refusal naming them for any other."""
    key = ((system or platform.system()).lower(), (machine or platform.machine()).lower())
    asset = CLI_ASSETS.get(key)
    if asset is None:
        supported = ", ".join(f"{name} {arch}" for name, arch in sorted(CLI_ASSETS))
        raise AislandError(f"No VS Code CLI build for {key[0]} {key[1]}. Supported: {supported}.")
    return asset


def cannot_run(program: Path, error: OSError) -> AislandError:
    """The refusal for a CLI the system will not run — another machine's build, a file that lost its execute bit:
    one line naming the file and how to get this machine's own."""
    return AislandError(
        f"The VS Code CLI at {program} cannot run here ({error.strerror or error}). Remove it and run "
        "`aisland tunnel start` again to fetch this machine's build (or `--cli` with an archive built for it)."
    )


@contextlib.contextmanager
def _code_member(archive_path: Path) -> Iterator[Optional[IO[bytes]]]:
    """The archive's ``code`` member, open for reading while the block runs (None when it has none): the CLI comes as a
    ``.zip`` on macOS and a ``.tar.gz`` on Linux."""
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            yield archive.open("code") if "code" in names else None
        return
    with tarfile.open(archive_path) as tar:
        try:
            member = tar.extractfile("code")
        except KeyError:  # pragma: no cover - a truncated or wrong archive
            member = None
        yield member


def _extract_cli(tarball: Path, destination: Path) -> None:
    """Write the archive's ``code`` member to ``destination``, executable.

    The single member is copied out by name rather than unpacked: an archive cannot then write
    anywhere but the one path this function chose. It is written to a ``.part`` file beside
    ``destination`` and moved into place whole, so an interrupted copy leaves no binary that
    :func:`install_cli` would take for an installed one.
    """
    with _code_member(tarball) as member:
        if member is None:
            raise AislandError(f"{tarball} holds no 'code' binary — is it the VS Code CLI archive?")
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        try:
            with member, open(part, "wb") as out:
                shutil.copyfileobj(member, out)
            part.chmod(0o755)
            os.replace(part, destination)
        except BaseException:
            part.unlink(missing_ok=True)
            raise


def _latest_release(asset: str, folder: Path) -> Tuple[str, str]:
    """The latest stable CLI archive's url and SHA-256, from the update service's document (fetched into ``folder``).

    Raises ``OSError`` when the service does not answer, and :class:`AislandError` for a document without an https
    url or a digest — an archive aisland could not check is never downloaded."""
    document = folder / "release.json"
    downloads.download(CLI_RELEASE_URL.format(asset=asset), document)
    try:
        release = json.loads(document.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AislandError(f"The VS Code update service's answer for {asset} is no JSON document: {exc}") from exc
    url = release.get("url") if isinstance(release, dict) else None
    digest = release.get("sha256hash") if isinstance(release, dict) else None
    if not (isinstance(url, str) and url.startswith("https://") and isinstance(digest, str) and len(digest) == 64):
        raise AislandError(f"The VS Code update service names no https archive and SHA-256 for {asset}")
    return url, digest.lower()


def install_cli(tarball: Optional[Path] = None, force: bool = False) -> Path:
    """Make the VS Code CLI available and return its path.

    Args:
        tarball: Install from this archive instead of downloading — for a box that cannot reach the
            download host (upload it the way an aisland wheels zip is uploaded).
        force: Re-install even when the binary is already there.
    """
    target = cli_path()
    if target.exists() and not force:
        return target

    state_dir().mkdir(parents=True, exist_ok=True)
    if tarball is not None:
        source = Path(tarball).expanduser()
        if not source.is_file():
            raise AislandError(f"No such file: {source}")
        _extract_cli(source, target)
        return target

    asset = cli_asset()
    console.echo(f"Fetching the VS Code CLI ({asset})…")
    with tempfile.TemporaryDirectory() as tmp:
        url = CLI_RELEASE_URL.format(asset=asset)
        downloaded = Path(tmp) / "vscode_cli.archive"  # a .tar.gz or a .zip, told apart by its content
        try:
            url, expected = _latest_release(asset, Path(tmp))
            downloads.download(url, downloaded, timeout=180)
        except OSError as exc:  # ``URLError`` and a timeout are ``OSError``s
            raise AislandError(
                f"Could not download the VS Code CLI from {url}: {exc}\n"
                "  Fetch it on a machine that can, upload it here, and install it with:\n"
                "      aisland tunnel start --cli <the downloaded archive>"
            ) from exc
        actual = downloads.sha256(downloaded)
        if actual != expected:
            raise AislandError(
                f"The VS Code CLI downloaded from {url} has SHA-256 {actual}, not the {expected} the update service "
                "lists; it was not installed. Try again, or install an archive you checked with "
                "`aisland tunnel start --cli <the archive>`"
            )
        _extract_cli(downloaded, target)
    return target
