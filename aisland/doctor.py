"""Preflight health checks for the shared workspace ``.venv``.

Some breakages in this workspace are *environment* skew, not code: the venv is shared by
every project and built incrementally, so one `pip install` can leave a package pair
inconsistent. Those failures surface far from their cause — a `dlopen` error inside a pytest
COLLECTION step, an `ImportError` three libraries deep in an unrelated import chain — and cost
a long diagnosis every time they recur.

Each check here encodes one such skew that `AGENTS.md` documents as recurring, together with
the remediation recorded there. The point is to turn a twenty-minute forensic session into
one line of output before a build starts.

Adding a check: write a ``check_*`` function returning a :class:`CheckResult` and add it to
:data:`CHECKS`. A check MUST be cheap, read-only, and specific — it reports a known-recurring
skew with the exact command that fixes it, never a generic "something looks off".
"""

import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Callable, List, Optional, Tuple

import click

#: The OpenCV variant the workspace keeps. Both distributions install the SAME files
#: (`cv2/cv2.abi3.so` + `cv2/.dylibs/*` appear in BOTH RECORDs) with different bundled native
#: libraries, so only one may be present. `manager._ROOT_UV_BASE` enforces that by dropping
#: the GUI wheel workspace-wide (`opencv-python ; sys_platform == 'never'`).
_OPENCV_KEPT = "opencv-python-headless"

#: The variant the uv override drops. Its presence means something installed OUTSIDE the
#: override — `pip` does not read `uv.toml`, and some libraries self-install requirements.
_OPENCV_DROPPED = "opencv-python"


#: The exact symbol every MCP server in the workspace imports. The 2.x SDK renamed BOTH the
#: module and the class (`mcp.server.fastmcp.FastMCP` -> `mcp.server.mcpserver.MCPServer`) and
#: the two spellings NEVER coexist — 1.x ships only the old one, 2.x only the new. Because the
#: consuming projects declare an unbounded floor, either version resolves cleanly and the
#: mismatch surfaces only when someone actually starts a server.
_MCP_SERVER_IMPORT = "from mcp.server.mcpserver import MCPServer"


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
    also keeps heavy modules out of the CLI's own process.
    """
    completed = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
    )
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
    `manager._ROOT_UV_BASE` resolves the conflict workspace-wide by dropping the GUI wheel
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


#: Every check the doctor runs, in report order.
CHECKS: Tuple[Callable[[], CheckResult], ...] = (
    check_opencv_single_variant,
    check_huggingface_hub_floor,
    check_mcp_server_api,
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
    click.echo(f"Environment doctor — {sys.executable}")
    for result in results:
        mark = "✓" if result.ok else "✗"
        click.echo(f"  {mark} {result.name:16} {result.detail}")
        if not result.ok and result.remedy:
            click.echo(f"      fix: {result.remedy}")

    failed = [result for result in results if not result.ok]
    if failed:
        click.echo(f"\n{len(failed)} of {len(results)} checks failed.")
        return 1
    click.echo(f"\nAll {len(results)} checks passed.")
    return 0


__all__ = [
    "CHECKS",
    "CheckResult",
    "check_huggingface_hub_floor",
    "check_mcp_server_api",
    "check_opencv_single_variant",
    "run_checks",
    "run_doctor",
]
