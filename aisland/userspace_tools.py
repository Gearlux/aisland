"""The tools aisland can install entirely in userspace, on Linux: each a pinned release and its sha256 per architecture.

A table, so what reads which tools exist (discovery checks a project's ``userspace`` declaration against it) needs
nothing of the installer that downloads them (:mod:`aisland.setup.installers`).
"""

from typing import Dict, List, NotRequired, TypedDict


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
    # The aisland service supervisor (see aisland/services/supervisor.py). macOS gets it
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
