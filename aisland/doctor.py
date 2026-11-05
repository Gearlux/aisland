"""Preflight health checks for known-recurring environment skew.

Some breakages in this workspace are *environment* skew, not code. The venv is shared by
every project and built incrementally, so one `pip install` can leave a package pair
inconsistent; the Docker layer (Colima) has its own equivalents — a client config left behind
by an uninstalled tool, a VM whose trust store does not match the network the host is on.
Those failures surface far from their cause — a `dlopen` error inside a pytest COLLECTION
step, an `ImportError` three libraries deep in an unrelated import chain, a `docker pull`
dying on credentials it does not need — and cost a long diagnosis every time they recur.

Each check here encodes one such skew that `AGENTS.md` documents as recurring, together with
the remediation recorded there. The point is to turn a twenty-minute forensic session into
one line of output before a build starts.

Adding a check: write a ``check_*`` function returning a :class:`CheckResult` and add it to
:data:`CHECKS`. A check MUST be cheap, read-only, and specific — it reports a known-recurring
skew with the exact command that fixes it, never a generic "something looks off".
"""

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import click
from packaging.version import InvalidVersion, Version

from aisland import console, locations
from aisland.services.docker import DockerService
from aisland.services.jenkins import JenkinsService
from aisland.services.jenkins_java import JenkinsJava
from aisland.tunnel import binary as tunnel_binary
from aisland.tunnel import connect as tunnel_connect
from aisland.tunnel import host as tunnel
from aisland.tunnel import state as tunnel_state

#: The OpenCV variant the workspace keeps. Both distributions install the SAME files
#: (`cv2/cv2.abi3.so` + `cv2/.dylibs/*` appear in BOTH RECORDs) with different bundled native
#: libraries, so only one may be present. `uv_config._ROOT_UV_BASE` enforces that by dropping
#: the GUI wheel workspace-wide (`opencv-python ; sys_platform == 'never'`).
_OPENCV_KEPT = "opencv-python-headless"

#: The variant the uv override drops. Its presence means something installed OUTSIDE the
#: override — `pip` does not read `uv.toml`, and some libraries self-install requirements.
_OPENCV_DROPPED = "opencv-python"


#: The exact symbol every MCP server in the workspace imports. The 2.x SDK renamed BOTH the
#: module and the class (`mcp.server.fastmcp.FastMCP` -> `mcp.server.mcpserver.MCPServer`) and
#: the two spellings NEVER coexist — 1.x ships only the old one, 2.x only the new. The consuming
#: projects declare `mcp>=2.0`, so a venv that still holds 1.x is stale (an incremental install
#: that never moved it) and the mismatch surfaces only when someone actually starts a server.
_MCP_SERVER_IMPORT = "from mcp.server.mcpserver import MCPServer"


#: The URL the VM-trust probe fetches: the registry the workspace's digest-pinned containers
#: actually pull from (`aisland iqengine`'s image lives on ghcr.io). Any HTTP answer — even a
#: 401 — proves the TLS handshake verified; curl exit 60 is specifically "certificate
#: verification failed".
_VM_TLS_PROBE_URL = "https://ghcr.io/v2/"

#: Drop the `credsStore` key from the docker client config, with a backup first. Removing the
#: key (rather than repointing it) is correct because the workspace's registries are public —
#: with `auths` empty and no helper configured, every pull runs anonymously.
_CREDSSTORE_REMEDY = (
    "cp ~/.docker/config.json ~/.docker/config.json.bak && "
    "python3 -c \"import json,pathlib; p=pathlib.Path.home()/'.docker'/'config.json'; "
    "c=json.loads(p.read_text()); c.pop('credsStore',None); "
    "p.write_text(json.dumps(c,indent=2)+'\\n')\""
)

#: Install the CA chain the network actually presents into the VM's trust store. Capturing it
#: from a live handshake (rather than naming a corporate CA file) works for ANY intercepting
#: proxy and stays correct when the proxy's chain rotates; the leaf riding along in the bundle
#: is inert (it cannot sign, so it anchors nothing).
_VM_CA_REMEDY = (
    "echo | openssl s_client -connect ghcr.io:443 -servername ghcr.io -showcerts 2>/dev/null"
    " | awk '/BEGIN CERT/,/END CERT/'"
    " | colima ssh -- sudo tee /usr/local/share/ca-certificates/corporate-proxy-ca.crt >/dev/null"
    " && colima ssh -- sudo update-ca-certificates"
    " && colima ssh -- sudo systemctl restart docker"
)


#: How old a torch extension build lock may be before it is a killed build's leftover. torch serialises
#: a JIT build with an empty `lock` file and waits on an existing one WITHOUT a timeout, deleting it only
#: when the compiling process lives through the build; the workspace's one such extension compiles in
#: about ten seconds (measured 2026-10-13), so two minutes is an order of magnitude over a live build.
#: A project that imports such an extension may clear a lock older than this before its import.
_TORCH_LOCK_STALE_SECONDS = 120.0

#: The first torch measured to build under an OS-level lock (`filelock`) instead of a lock file whose mere existence
#: blocks: the kernel releases it when the builder dies, so a killed build blocks nothing, and the `lock` file stays
#: behind after every build. Measured 2026-10-25: a build killed mid-compile left the next import waiting > 240 s on
#: torch 2.13.0 and loading in 43 s on 2.14.1 (pytorch issue 189245). Not 2.14.0: it was not measured.
_TORCH_OS_LOCK_SINCE = Version("2.14.1")


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one health check.

    Args:
        name: Short check identifier shown in the report.
        ok: True when the environment is healthy for this check.
        detail: One line describing what was found (shown pass or fail).
        remedy: Copy-pasteable command that fixes it — required when ``ok`` is False.
    """

    name: str
    ok: bool
    detail: str
    remedy: str = ""


#: Seconds a probe the doctor runs in a process of its own may take: an import, the Colima VM's TLS request.
PROBE_SECONDS = 120


def _installed_version(distribution: str) -> Optional[str]:
    """Version of ``distribution`` in the running interpreter, or None if absent."""
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _import_probe(statement: str) -> Tuple[bool, str]:
    """Run ``statement`` in a SUBPROCESS; return (succeeded, first error line).

    A subprocess because these probes import libraries that can abort the interpreter on a
    broken native bundle — a doctor that dies while diagnosing is worse than no doctor. It
    also keeps heavy modules out of the CLI's own process. An import that has not finished after
    :data:`PROBE_SECONDS` fails saying so: a hanging import is itself the breakage (a stale build
    lock makes one wait forever), and the doctor — and the MCP tool that runs it — must answer.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", statement], capture_output=True, text=True, timeout=PROBE_SECONDS
        )
    except subprocess.TimeoutExpired:
        return False, f"did not finish within {PROBE_SECONDS} s"
    if completed.returncode == 0:
        return True, completed.stdout.strip()
    error_lines = [ln for ln in (completed.stderr or "").strip().splitlines() if ln.strip()]
    return False, error_lines[-1] if error_lines else f"exit code {completed.returncode}"


def check_opencv_single_variant() -> CheckResult:
    """Exactly ONE OpenCV distribution may be installed — the two overwrite each other's files.

    `opencv-python` and `opencv-python-headless` ship the same `cv2/` package with DIFFERENT
    bundled native libraries, and both claim `cv2/cv2.abi3.so` + `cv2/.dylibs/*` in their
    RECORD. Several projects pull each one (ultralytics / torchsig / supervision want the GUI
    wheel; albumentations / fiftyone / label-studio-sdk want headless), so
    `uv_config._ROOT_UV_BASE` resolves the conflict workspace-wide by dropping the GUI wheel
    (`opencv-python ; sys_platform == 'never'`) and keeping `opencv-python-headless>=4.12,<5`.

    The GUI wheel coming back means something installed outside that override — plain `pip`
    ignores `uv.toml`, and libraries like ultralytics self-install their requirements at
    runtime. Once both are present, the next install of either rewrites the other's bundled
    libs and `cv2/.dylibs/` ends up mixed between two wheels: ``Library not loaded:
    @loader_path/libsrt.1.5.3.dylib`` from a leftover `libavformat.60.*` — surfacing as a
    pytest COLLECTION error in every suite that imports cv2.
    """
    dropped = _installed_version(_OPENCV_DROPPED)
    kept = _installed_version(_OPENCV_KEPT)

    if dropped is not None:
        detail = f"{_OPENCV_DROPPED}={dropped} is installed; the workspace override drops it"
        if kept is not None:
            detail += f" and keeps {_OPENCV_KEPT}={kept} — both own cv2/, so .dylibs will mix"
        return CheckResult(
            "opencv",
            False,
            detail,
            f"uv pip uninstall {_OPENCV_DROPPED} && uv pip install --force-reinstall {_OPENCV_KEPT}"
            "   # use `uv pip`, not `pip` — pip does not read uv.toml's overrides",
        )

    if kept is None:
        return CheckResult("opencv", True, "no OpenCV installed — nothing to skew")

    ok, output = _import_probe("import cv2; print(cv2.__version__)")
    if not ok:
        return CheckResult(
            "opencv",
            False,
            f"`import cv2` fails: {output}",
            f"uv pip install --force-reinstall {_OPENCV_KEPT}",
        )
    return CheckResult("opencv", True, f"cv2 {output} imports; only {_OPENCV_KEPT}={kept} installed")


def check_huggingface_hub_floor() -> CheckResult:
    """`huggingface_hub` must not sit below the floor `transformers` imports against.

    `transformers` (5.x) imports `is_offline_mode`, which exists only in `huggingface_hub`
    1.x, but an incrementally-built venv can leave the 0.x that satisfied `datasets` /
    `tokenizers` when they were first installed. The failure surfaces while importing
    `pytorch_lightning` (via `torchmetrics` -> `transformers`), nowhere near its cause.
    """
    if _installed_version("transformers") is None:
        return CheckResult("huggingface_hub", True, "transformers not installed — floor does not apply")

    ok, output = _import_probe(
        "from huggingface_hub import is_offline_mode; import huggingface_hub as h; print(h.__version__)"
    )
    if not ok:
        return CheckResult(
            "huggingface_hub",
            False,
            f"transformers' import surface is missing: {output}",
            "uv pip install 'huggingface_hub>=1.5.0,<2.0'",
        )
    return CheckResult("huggingface_hub", True, f"huggingface_hub {output} satisfies transformers' import surface")


def check_mcp_server_api() -> CheckResult:
    """The installed `mcp` must expose the server class the workspace's MCP servers import.

    `mcp` 2.0 renamed `mcp.server.fastmcp.FastMCP` to `mcp.server.mcpserver.MCPServer`. The
    names do not overlap, so a venv holding either version satisfies an unbounded
    `mcp>=1.9.0` floor while breaking whichever side the code is not written against. The
    failure is invisible until an MCP server is launched — a CLI built on the same package
    keeps working, because it never imports the server module.
    """
    if _installed_version("mcp") is None:
        return CheckResult("mcp", True, "mcp not installed — no MCP server surface in play")

    ok, output = _import_probe(f"{_MCP_SERVER_IMPORT}; import importlib.metadata as m; print(m.version('mcp'))")
    if not ok:
        return CheckResult(
            "mcp",
            False,
            f"the MCP server class is missing — servers cannot start: {output}",
            "uv pip install 'mcp>=2.0'",
        )
    return CheckResult("mcp", True, f"mcp {output} exposes MCPServer — MCP servers can start")


def check_docker_credsstore() -> CheckResult:
    """`~/.docker/config.json` must not name a credential helper that does not exist.

    Docker Desktop writes ``"credsStore": "desktop"`` into the client config and ships the
    `docker-credential-desktop` binary; uninstalling it in favour of Colima leaves the config
    behind with the binary gone, and the docker CLI then consults the dead helper on EVERY
    pull — ``exec: "docker-credential-desktop": executable file not found in $PATH`` — even
    for public images that need no credentials at all (hit 2026-08-31 by `aisland iqengine
    start`; documented in the root AGENTS.md alongside the other recurring skews). The remedy
    removes the key rather than repointing it: with ``auths`` empty, anonymous pulls are the
    correct state for this workspace's public registries.
    """
    config_path = Path.home() / ".docker" / "config.json"
    if not config_path.exists():
        return CheckResult("docker-creds", True, "no ~/.docker/config.json — nothing to skew")

    try:
        document = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        # An inconclusive probe must never cry wolf (same principle as
        # `supervisor._preflight_full_disk_access`): a mangled config is docker's to report.
        return CheckResult("docker-creds", True, f"~/.docker/config.json did not parse ({error}) — inconclusive")
    if not isinstance(document, dict) or not document.get("credsStore"):
        detail = (
            "no credsStore configured — public pulls run anonymously"
            if isinstance(document, dict)
            else "~/.docker/config.json is not a JSON object — inconclusive"
        )
        return CheckResult("docker-creds", True, detail)

    store = document["credsStore"]
    helper = f"docker-credential-{store}"
    if shutil.which(helper) is None:
        return CheckResult(
            "docker-creds",
            False,
            f'credsStore "{store}" needs {helper}, which is not on PATH — '
            "every docker pull dies before contacting the registry",
            _CREDSSTORE_REMEDY,
        )
    return CheckResult("docker-creds", True, f'credsStore "{store}" resolves — {helper} is on PATH')


def _colima_running() -> bool:
    """Whether the Colima VM is up — the docker service's exit-code judgement, reused.

    `DockerService.is_running` judges by `colima status`'s EXIT CODE, never its text (see the
    mandate in aisland's AGENTS.md); this seam exists so tests can control the answer.
    """
    return DockerService().is_running()


def _vm_curl_exit(url: str) -> Optional[int]:
    """Exit code of ``curl -sI <url>`` run INSIDE the Colima VM; None when the VM did not answer in time.

    In a subprocess for the same reason as :func:`_import_probe` — and inside the VM because
    that is where the docker daemon verifies TLS: the HOST succeeding proves nothing about the
    VM's trust store, which is the thing that breaks. curl's own ``--max-time`` bounds the request,
    :data:`PROBE_SECONDS` the ``colima ssh`` around it.
    """
    try:
        completed = subprocess.run(
            ["colima", "ssh", "--", "curl", "-sI", "-o", "/dev/null", "--max-time", "10", url],
            capture_output=True,
            timeout=PROBE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    return completed.returncode


def check_colima_vm_trust() -> CheckResult:
    """The Colima VM must be able to verify TLS on the network the host is on.

    A TLS-intercepting corporate firewall re-signs all traffic with a private CA that macOS
    trusts (System keychain) but the Ubuntu VM inside Colima does not, so every `docker pull`
    dies with ``tls: failed to verify certificate: x509: certificate signed by unknown
    authority`` while the same URL works on the host (hit 2026-08-31; documented in the root
    AGENTS.md). The remedy installs the CA into the VM — which does NOT survive
    ``colima delete``, so this check is what turns the post-recreation re-diagnosis into one
    line. Judged by curl's exit code: 60 is specifically "certificate verification failed";
    any HTTP answer means the handshake verified; other network errors are inconclusive, not
    failures. A stopped VM passes with a note — the doctor never boots a VM, and a laptop not
    running Docker must pass preflight.
    """
    if shutil.which("colima") is None:
        return CheckResult("colima-tls", True, "colima not installed — no VM trust store in play")
    if not _colima_running():
        return CheckResult("colima-tls", True, "Colima is not running — VM trust store not probed")

    exit_code = _vm_curl_exit(_VM_TLS_PROBE_URL)
    if exit_code is None:
        return CheckResult("colima-tls", True, f"probe inconclusive — the VM did not answer within {PROBE_SECONDS} s")
    if exit_code == 60:
        return CheckResult(
            "colima-tls",
            False,
            f"the VM cannot verify TLS to {_VM_TLS_PROBE_URL} (curl exit 60) — an intercepting "
            "proxy's CA is trusted by macOS but not by the VM, so every docker pull fails",
            _VM_CA_REMEDY,
        )
    if exit_code != 0:
        return CheckResult(
            "colima-tls", True, f"probe inconclusive (curl exit {exit_code}) — a network error, not a trust failure"
        )
    return CheckResult("colima-tls", True, f"the VM verifies TLS to {_VM_TLS_PROBE_URL} — pulls can download")


def _jenkins_java() -> Optional[JenkinsJava]:
    """What the downloaded Jenkins WAR says about the installed JDKs; None before it is downloaded.

    The doctor never downloads it — that is `aisland jenkins start`'s and `aisland workspace provision install`'s.
    """
    service = JenkinsService()
    if not service.war_file.exists():
        return None
    return service.resolve_java()


def check_jenkins_java() -> CheckResult:
    """An installed JDK must be one the Jenkins WAR accepts — `java` on PATH is not enough.

    Homebrew's `openjdk` formula follows the newest Java, and a shell profile usually puts it
    first on PATH; Jenkins refuses every Java its launcher does not name ("Supported Java
    versions are: [21, 25]"), so when Java 26 arrived on 2026-10-06 Jenkins exited 1 three
    times under the supervisor and stayed down, with the reason only in its log. The probe
    runs `java -jar jenkins.war --version` per JDK, newest first (about 0.1 s each).
    """
    choice = _jenkins_java()
    if choice is None:
        return CheckResult("jenkins-java", True, "Jenkins not downloaded yet — no Java to check")
    if choice.runtime is None:
        return CheckResult("jenkins-java", False, choice.problem(), choice.remedy())
    return CheckResult("jenkins-java", True, choice.describe())


def _torch_extensions_root() -> Path:
    """Where torch builds JIT extensions, without importing torch: `$TORCH_EXTENSIONS_DIR`, else torch's
    per-user cache (`torch._appdirs.user_cache_dir("torch_extensions")`: `~/Library/Caches` on macOS,
    `$XDG_CACHE_HOME` or `~/.cache` elsewhere, `%LOCALAPPDATA%` on Windows)."""
    configured = os.environ.get("TORCH_EXTENSIONS_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "torch_extensions"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "torch_extensions" / "torch_extensions" / "Cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "torch_extensions"


def _torch_build_locks(root: Path) -> List[Path]:
    """This venv's build locks, oldest first: `<root>/<ext>/lock` with `TORCH_EXTENSIONS_DIR` set, else
    `<root>/py<this python>_<accelerator>/<ext>/lock`. Another Python's folder belongs to another venv, whose torch
    (and so whether its lock can block) this doctor cannot see."""
    if not root.is_dir():
        return []
    if os.environ.get("TORCH_EXTENSIONS_DIR"):
        pattern = "*/lock"
    else:
        pattern = f"py{sys.version_info.major}{sys.version_info.minor}_*/*/lock"
    found = [path for path in root.glob(pattern) if path.is_file()]
    return sorted(found, key=lambda path: path.stat().st_mtime)


def _torch_version() -> Optional[Version]:
    """This venv's torch, from its installed metadata (torch is never imported here); None when it has none."""
    try:
        return Version(version("torch"))
    except (PackageNotFoundError, InvalidVersion):
        return None


def check_torch_extension_lock() -> CheckResult:
    """No torch JIT-extension build lock is left behind by a killed compile.

    `torch.utils.cpp_extension.load` takes an empty `lock` file in the extension's build directory and,
    when one exists, polls until it disappears — forever. Only the process that compiles removes it, so a
    process killed mid-compile (a `pytest` stopped by hand, a server killed from its terminal) leaves it,
    and from then on every process importing the package that compiles sleeps with no error: an
    annotation server's first tool-graph load, a class catalogue's helper process (2026-10-13: one lock
    hung every start for an afternoon, 8 s of CPU then nothing). A lock younger than the bound is a build
    that may still be running and is reported as such, not as a failure.

    That is torch up to 2.13. From :data:`_TORCH_OS_LOCK_SINCE` torch builds under an OS lock that a killed process
    releases, and leaves the `lock` file behind after every build: then a lock file is no failure, and the check
    passes saying why. Only this venv's own folder is read (:func:`_torch_build_locks`).
    """
    torch = _torch_version()
    if torch is None:
        return CheckResult("torch-ext-lock", True, "torch is not installed in this venv — it builds no extension")
    if torch >= _TORCH_OS_LOCK_SINCE:
        return CheckResult(
            "torch-ext-lock",
            True,
            f"torch {torch} holds an OS lock while it builds — a killed build leaves no lock that blocks",
        )
    root = _torch_extensions_root()
    locks = _torch_build_locks(root)
    if not locks:
        return CheckResult("torch-ext-lock", True, f"no torch extension build lock left behind under {root}")
    now = time.time()
    stale = [lock for lock in locks if now - lock.stat().st_mtime > _TORCH_LOCK_STALE_SECONDS]
    if not stale:
        building = ", ".join(f"{lock.parent.name} ({now - lock.stat().st_mtime:.0f} s)" for lock in locks)
        return CheckResult("torch-ext-lock", True, f"torch is building an extension right now: {building}")
    oldest = stale[0]
    age_minutes = (now - oldest.stat().st_mtime) / 60
    names = ", ".join(lock.parent.name for lock in stale)
    remedy = "rm " + " ".join(f"'{lock}'" for lock in stale)
    return CheckResult(
        "torch-ext-lock",
        False,
        f"a torch extension build lock is {age_minutes:.0f} min old with no build to hold it ({names}: {oldest}) — "
        f"with torch {torch}, every import that compiles that extension waits on it forever",
        remedy,
    )


def check_tunnel_relay() -> CheckResult:
    """A machine that uses `aisland tunnel` can still reach the relay the tunnel dials out to.

    The tunnel's whole premise is outbound-only: nothing is exposed through an ingress, so the one
    thing that can quietly take it away is egress to the relay being closed — and the symptom on the
    remote box is a tunnel that starts, logs nothing useful and never appears in the editor.

    The probe runs ONLY where a tunnel has been used (a downloaded CLI, a remembered provider or a
    pidfile): the doctor runs on every machine, most of which have no tunnel, and none of them should
    pay a network timeout for a feature they never touched.
    """
    state = tunnel_state.state_dir()
    configured = any(path.exists() for path in (tunnel_binary.cli_path(), tunnel.provider_path(), tunnel.pid_path()))
    if not configured:
        return CheckResult("tunnel-relay", True, f"no tunnel configured here (nothing in {state})")
    reachable, detail = tunnel.relay_reachable()
    if reachable:
        return CheckResult("tunnel-relay", True, detail)
    return CheckResult(
        "tunnel-relay",
        False,
        f"{detail} — a tunnel started here cannot reach the editor",
        f"curl -sS -m 8 -o /dev/null -w '%{{http_code}}\\n' https://{tunnel.RELAY_HOST}/   "
        "# confirm, then ask for outbound 443 to that host; until then `aisland tunnel stop` and work over git",
    )


def check_tunnel_connect() -> CheckResult:
    """A machine that uses `aisland tunnel connect` has what it needs: `devtunnel`, signed in, and msgpack.

    `connect` refuses at once without any of them, naming the fix — but only when someone runs it; the
    doctor says it ahead of time, the way `tunnel-relay` does for the remote side. In the order a first
    `connect` meets them: the program, the extra, the sign-in.

    Runs ONLY where `connect` has been used (its last list or its forward left a file), so a machine that
    never connects neither needs `devtunnel` nor pays for asking the service. The sign-in question goes
    over the network; when `devtunnel` cannot answer it the check passes with that note, as a probe that
    cannot run does.
    """
    if not tunnel_connect.used_here():
        return CheckResult(
            "tunnel-connect", True, f"`aisland tunnel connect` not used here (nothing in {tunnel_state.state_dir()})"
        )
    if shutil.which("devtunnel") is None:
        return CheckResult(
            "tunnel-connect",
            False,
            "no `devtunnel` on PATH — `aisland tunnel connect` reaches the tunnel through it",
            tunnel_connect.install_hint(),
        )
    imported, error = _import_probe("import msgpack")
    if not imported:
        return CheckResult(
            "tunnel-connect",
            False,
            f"msgpack does not import ({error}) — `aisland tunnel connect` speaks the tunnel's messages with it",
            "pip install 'aisland[connect]'",
        )
    try:
        account = tunnel_connect.account()
    except click.ClickException as exc:
        return CheckResult("tunnel-connect", True, f"could not ask devtunnel whether it is signed in: {exc.message}")
    if account is None:
        return CheckResult(
            "tunnel-connect",
            False,
            "devtunnel is not signed in — `aisland tunnel connect` lists and reaches tunnels as that account",
            "devtunnel user login   # -g for a GitHub account; the account the remote tunnel runs under",
        )
    return CheckResult("tunnel-connect", True, f"devtunnel ready: {account}")


#: Each VS Code window's Python language-server log, below `<logs>/<session>/window<N>/exthost/`.
_PYLANCE_LOG = Path("ms-python.python") / "Python Language Server.log"

#: What V8 prints when the language server's heap is full; every worker thread prints it, so a crash is the
#: lines of one second.
_PYLANCE_OUT_OF_MEMORY = "JavaScript heap out of memory"

#: What Pylance logs when it starts on a Node.js of its own (`python.analysis.nodeExecutable`), never on VS Code's.
_PYLANCE_OWN_NODE = "Client: Running with node: "

#: The fix while Pylance runs on VS Code's Node.js (a heap of about 1 GB): its own, with `--max-old-space-size=8192`
#: (its default `python.analysis.nodeArguments`). Both settings are machine-scoped: VS Code ignores them in a
#: workspace file, so they go in the User settings.
_PYLANCE_OWN_NODE_REMEDY = (
    'add "python.analysis.nodeExecutable": "auto" to your VS Code User settings (Cmd/Ctrl+Shift+P → '
    '"Preferences: Open User Settings (JSON)"), then "Developer: Reload Window"'
)

#: The fix when Pylance ran out of memory on its own Node.js too: a larger heap than its default 8 GB.
_PYLANCE_HEAP_REMEDY = (
    'add "python.analysis.nodeArguments": ["--max-old-space-size=16384"] to your VS Code User settings '
    '(Cmd/Ctrl+Shift+P → "Preferences: Open User Settings (JSON)"), then "Developer: Reload Window"'
)


def _vscode_logs_root() -> Path:
    """Where VS Code keeps its logs: `~/Library/Application Support/Code/logs` on macOS, `%APPDATA%\\Code\\logs` on
    Windows, `$XDG_CONFIG_HOME/Code/logs` (else `~/.config/Code/logs`) elsewhere."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "logs"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return (Path(appdata) if appdata else Path.home() / "AppData" / "Roaming") / "Code" / "logs"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "Code" / "logs"


def _pylance_log(logs: Path, workspace: Path) -> Optional[Tuple[Path, List[str]]]:
    """The newest language-server log of a window opened on `workspace`, and its lines; None when there is none.

    A window's log names each of its folders (`Starting service instance … for workspace "<folder>"`), so the
    workspace's own folder picks its windows out; a log that cannot be read is passed over."""
    if not logs.is_dir():
        return None
    marker = f'for workspace "{workspace}"'
    candidates = sorted(logs.glob(f"*/window*/exthost/{_PYLANCE_LOG.as_posix()}"), key=lambda p: p.stat().st_mtime)
    for path in reversed(candidates):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if marker in text:
            return path, text.splitlines()
    return None


def check_pylance_memory() -> CheckResult:
    """VS Code's Python language server does not keep running out of memory in this workspace's window.

    Pylance runs on VS Code's own Node.js, whose heap is about 1 GB; a workspace of many projects fills it while
    the server first reads them, it dies with `JavaScript heap out of memory`, VS Code restarts it, and it dies
    again — no completion, hover or symbol search, with nothing on screen saying why (2026-10-30: seven crashes
    in a morning, every half minute at worst). On a Node.js of its own (`python.analysis.nodeExecutable: auto`)
    it gets 8 GB. Read from the newest log of a window opened on the workspace: a crash after which Pylance
    started on its own Node.js passes; one before any such start fails with the setting to add, and one on its
    own Node.js too with a larger heap. A machine with no VS Code log of the workspace (a remote one: the logs
    are the local VS Code's) passes with a note.
    """
    found = _pylance_log(_vscode_logs_root(), locations.workspace_root())
    if found is None:
        return CheckResult("pylance-memory", True, "no VS Code window of this workspace has logged Pylance here")
    log, lines = found
    crashes = [index for index, line in enumerate(lines) if _PYLANCE_OUT_OF_MEMORY in line]
    if not crashes:
        return CheckResult("pylance-memory", True, f"Pylance has not run out of memory ({log})")
    last_crash = crashes[-1]
    own_node = [index for index, line in enumerate(lines) if _PYLANCE_OWN_NODE in line]
    crashed_at = lines[last_crash][:19]
    if own_node and own_node[-1] > last_crash:
        since = lines[own_node[-1]][:19]
        return CheckResult(
            "pylance-memory",
            True,
            f"Pylance ran out of memory at {crashed_at} and runs on its own Node.js since {since}",
        )
    if own_node:
        return CheckResult(
            "pylance-memory",
            False,
            f"Pylance ran out of memory at {crashed_at} on its own Node.js too ({log})",
            _PYLANCE_HEAP_REMEDY,
        )
    times = len({lines[index][:19] for index in crashes})
    return CheckResult(
        "pylance-memory",
        False,
        f"Pylance ran out of memory {times} times, the last at {crashed_at} ({log}) — no completion or symbol "
        "search while it restarts",
        _PYLANCE_OWN_NODE_REMEDY,
    )


#: Every check the doctor runs, in report order.
CHECKS: Tuple[Callable[[], CheckResult], ...] = (
    check_opencv_single_variant,
    check_huggingface_hub_floor,
    check_mcp_server_api,
    check_docker_credsstore,
    check_colima_vm_trust,
    check_jenkins_java,
    check_torch_extension_lock,
    check_tunnel_relay,
    check_tunnel_connect,
    check_pylance_memory,
)


def run_checks() -> List[CheckResult]:
    """Run every registered check and return the results (no output)."""
    return [check() for check in CHECKS]


def run_doctor() -> int:
    """Run every check, print a report, and return a process exit code.

    Returns:
        0 when every check passed, 1 otherwise — so a CI step can gate on it.
    """
    results = run_checks()
    console.echo(f"Environment doctor — {sys.executable}")
    for result in results:
        mark = "✓" if result.ok else "✗"
        console.echo(f"  {mark} {result.name:16} {result.detail}")
        if not result.ok and result.remedy:
            console.echo(f"      fix: {result.remedy}")

    failed = [result for result in results if not result.ok]
    if failed:
        console.echo(f"\n{len(failed)} of {len(results)} checks failed.")
        return 1
    console.echo(f"\nAll {len(results)} checks passed.")
    return 0


__all__ = [
    "CHECKS",
    "CheckResult",
    "check_colima_vm_trust",
    "check_docker_credsstore",
    "check_huggingface_hub_floor",
    "check_jenkins_java",
    "check_mcp_server_api",
    "check_opencv_single_variant",
    "check_pylance_memory",
    "check_torch_extension_lock",
    "run_checks",
    "run_doctor",
]
