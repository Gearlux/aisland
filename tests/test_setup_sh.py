"""Pins for the interpreter-provisioning step of ``setup.sh``.

The step runs BEFORE any venv exists, so it cannot be Python — it is bash, and it is the one
part of the bootstrap that must work on a machine with nothing installed. It used to fall
back to ``brew install python@X``, which is macOS-only: on Ubuntu 24.04 (WSL2, 2026-08-17)
it died with ``Homebrew is not installed`` and no path forward. It now provisions the pinned
interpreter through ``uv`` on every platform, installing ``uv`` itself first when missing: its pinned release
archive, checked against the SHA-256 the release lists.

The block is delimited by ``# --- python-provisioning ---`` markers so these tests can run
it in a real ``bash`` against a scratch ``PATH`` holding stub ``uv`` / ``curl`` / ``pythonX``
executables that record their argv, instead of asserting on the shell text.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Optional

import pytest


def _bash() -> str:
    """The newest bash available — Homebrew's, not the one macOS ships.

    ``/bin/bash`` on macOS is 3.2.57, frozen at the last GPLv2 release (2007). CI
    runs on Ubuntu with bash 5.x, so exercising ``setup.sh`` under 3.2 tests a shell
    that nothing this workspace deploys to actually runs, and hides anything the
    script does with post-3.2 syntax. ``shutil.which`` is not enough on its own:
    ``/bin`` precedes ``/opt/homebrew/bin`` on a default macOS PATH, so it returns
    the old one. The explicit prefixes are checked first, then the PATH lookup
    (which is what answers on Linux, where there is only one bash).
    """
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash"):  # arm64 brew, intel brew
        if Path(candidate).is_file():
            return candidate
    return shutil.which("bash") or "/bin/bash"


BASH = _bash()

SETUP_SH = Path(__file__).resolve().parents[1] / "setup.sh"
START = "# --- python-provisioning ---"
STOP = "# --- /python-provisioning ---"


def _block() -> str:
    text = SETUP_SH.read_text()
    assert START in text and STOP in text, "setup.sh lost its provisioning markers"
    return text.split(START, 1)[1].split(STOP, 1)[0]


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!{BASH}\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A scratch bin dir + an argv log every stub appends to."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    return bindir, log


def _run(bindir: Path, log: Path, *, home: Path) -> subprocess.CompletedProcess[str]:
    script = _block() + '\necho "PYTHON_BIN=$PYTHON_BIN"\n'
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",  # coreutils + bash, none of the tools under test
        "HOME": str(home),
        "CALLS": str(log),
        "WORKSPACE_PYTHON": "3.13",
    }
    return subprocess.run([BASH, "-e", "-c", script], capture_output=True, text=True, env=env, check=False)


def _calls(log: Path) -> list[str]:
    return [line for line in log.read_text().splitlines() if line]


def test_a_present_interpreter_is_used_and_nothing_is_installed(sandbox: tuple[Path, Path], tmp_path: Path) -> None:
    """The con case: a machine that already has python3.13 (e.g. a Homebrew Mac) never enters
    the provisioning branch — byte-identical behaviour to before."""
    bindir, log = sandbox
    _stub(bindir, "python3.13", 'echo "python3.13 $*" >> "$CALLS"; echo "Python 3.13.9"')
    _stub(bindir, "uv", 'echo "uv $*" >> "$CALLS"')
    _stub(bindir, "curl", 'echo "curl $*" >> "$CALLS"')

    result = _run(bindir, log, home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert f"PYTHON_BIN={bindir}/python3.13" in result.stdout
    assert not any(c.startswith(("uv", "curl")) for c in _calls(log)), _calls(log)


def test_a_missing_interpreter_is_provisioned_by_an_existing_uv(sandbox: tuple[Path, Path], tmp_path: Path) -> None:
    """uv present, python3.13 absent: `uv python install` then `uv python find` supplies the
    path; no installer is fetched."""
    bindir, log = sandbox
    store = tmp_path / "store" / "cpython-3.13" / "bin" / "python3.13"
    _stub(
        bindir,
        "uv",
        'echo "uv $*" >> "$CALLS"\n' f'[ "$1 $2 $3" = "python find --system" ] && echo "{store}"\n' "exit 0",
    )
    _stub(bindir, "curl", 'echo "curl $*" >> "$CALLS"')

    result = _run(bindir, log, home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert _calls(log) == ["uv python install 3.13", "uv python find --system 3.13"]
    assert f"PYTHON_BIN={store}" in result.stdout


def _uv_release(tmp_path: Path, digest: Optional[str] = None) -> Path:
    """A folder standing for uv's release page: the Linux x86-64 archive holding a stub ``uv`` (which records its
    argv and names an interpreter) and the ``.sha256`` file the release lists beside it — the archive's own digest,
    or ``digest``."""
    release = tmp_path / "release"
    folder = release / "uv-x86_64-unknown-linux-gnu"
    folder.mkdir(parents=True)
    (folder / "uv").write_text(
        "#!/bin/bash\n"
        'echo "uv $*" >> "$CALLS"\n'
        '[ "$1 $2 $3" = "python find --system" ] && echo /store/python3.13\n'
        "exit 0\n"
    )
    (folder / "uvx").write_text("#!/bin/bash\n")
    for tool in ("uv", "uvx"):
        (folder / tool).chmod(0o755)
    archive = release / "uv-x86_64-unknown-linux-gnu.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder, arcname=folder.name)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    (release / f"{archive.name}.sha256").write_text(f"{digest or actual}  {archive.name}\n")
    return release


def _stub_download(bindir: Path, release: Path) -> None:
    """``curl -fLsS -o <file> <url>``: records its argv and writes the release's file the url names; ``uname`` says
    Linux x86-64, the archive :func:`_uv_release` holds."""
    _stub(bindir, "curl", f'echo "curl $*" >> "$CALLS"\ncp "{release}/${{4##*/}}" "$3"')
    _stub(bindir, "uname", '[ "$1" = "-s" ] && echo Linux || echo x86_64')


def test_a_missing_uv_is_installed_first(sandbox: tuple[Path, Path], tmp_path: Path) -> None:
    """Neither python3.13 nor uv on PATH (a fresh Linux machine): uv's pinned release archive is downloaded, checked
    against the SHA-256 its release lists, and the uv it puts in ~/.local/bin provisions the interpreter — without a
    new shell, and without piping a script from the network into a shell."""
    bindir, log = sandbox
    _stub_download(bindir, _uv_release(tmp_path))

    result = _run(bindir, log, home=tmp_path)

    assert result.returncode == 0, result.stderr + result.stdout
    calls = _calls(log)
    base = "https://github.com/astral-sh/uv/releases/download/0.12.0/uv-x86_64-unknown-linux-gnu.tar.gz"
    assert [call.split()[-1] for call in calls[:2]] == [base, f"{base}.sha256"], calls
    assert calls[2:] == ["uv python install 3.13", "uv python find --system 3.13"]
    assert (tmp_path / ".local" / "bin" / "uvx").is_file()
    assert "PYTHON_BIN=/store/python3.13" in result.stdout


def test_a_uv_archive_whose_digest_differs_is_never_installed(sandbox: tuple[Path, Path], tmp_path: Path) -> None:
    bindir, log = sandbox
    _stub_download(bindir, _uv_release(tmp_path, digest="0" * 64))

    result = _run(bindir, log, home=tmp_path)

    assert result.returncode != 0
    assert "does not match the SHA-256 its release lists" in result.stderr
    assert not (tmp_path / ".local" / "bin" / "uv").exists()
    assert not any(call.startswith("uv ") for call in _calls(log))


def test_a_uv_already_in_local_bin_is_found_without_refetching_the_installer(
    sandbox: tuple[Path, Path], tmp_path: Path
) -> None:
    """A previous run installed uv into ~/.local/bin, but this shell's PATH lacks that dir
    (a fresh non-login shell): the block must find it there rather than curl the installer
    again — the second real run on 2026-08-17 did exactly that."""
    bindir, log = sandbox
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    _stub(
        local_bin,
        "uv",
        'echo "uv $*" >> "$CALLS"\n[ "$1 $2 $3" = "python find --system" ] && echo /store/python3.13\nexit 0',
    )
    _stub(bindir, "curl", 'echo "curl $*" >> "$CALLS"')

    result = _run(bindir, log, home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert _calls(log) == ["uv python install 3.13", "uv python find --system 3.13"]
    assert "PYTHON_BIN=/store/python3.13" in result.stdout


def _shell_without_bash_version() -> str | None:
    """A shell that leaves ``BASH_VERSION`` unset, so setup.sh's re-exec guard fires.

    ``sh`` is NOT good enough, and the difference is what made this test destructive.
    macOS ``/bin/sh`` IS bash (3.2.57), and bash exports ``BASH_VERSION`` even in
    sh-compat mode, so ``[ -z "$BASH_VERSION" ]`` is false there, the guard is
    skipped, and the whole bootstrap runs for real. Ubuntu's ``/bin/sh`` is dash,
    the guard fires, and the stub stops it at once — green in CI, ruinous on a Mac.
    """
    for name in ("dash", "zsh", "ksh"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture
def sandboxed_setup_sh(tmp_path: Path) -> Path:
    """A COPY of ``setup.sh``, placed so its own path resolution stays inside `tmp_path`.

    ``setup.sh`` derives its target from where it SITS, not from the environment::

        WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
        VENV_DIR="$WORKSPACE_ROOT/.venv"

    so running the real file means the real workspace whatever ``PATH`` and ``HOME``
    say. That is how this suite once rebuilt the developer's ``.venv`` and pointed it
    at an interpreter inside a pytest ``tmp_path`` that pytest later collected,
    leaving every workspace command with a dangling interpreter. Running a copy from
    ``tmp_path/aisland/`` puts ``WORKSPACE_ROOT`` at ``tmp_path``: even a bootstrap
    that runs to completion can only build a venv it is welcome to build.
    """
    directory = tmp_path / "aisland"
    directory.mkdir()
    copy = directory / "setup.sh"
    copy.write_bytes(SETUP_SH.read_bytes())
    copy.chmod(copy.stat().st_mode | stat.S_IXUSR)
    return copy


def test_the_sandboxed_copy_cannot_resolve_the_real_workspace(sandboxed_setup_sh: Path, tmp_path: Path) -> None:
    """Containment, pinned independently of whether the re-exec guard works.

    This is the assertion that keeps its value if those ten lines of shell ever
    change again: wherever the bootstrap gets to, it is aimed at `tmp_path`.
    """
    resolved = subprocess.run(
        [BASH, "-c", 'cd "$(dirname "$1")/.." && pwd', "_", str(sandboxed_setup_sh)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert Path(resolved) == tmp_path
    assert Path(resolved) != SETUP_SH.resolve().parents[1]


def test_running_under_a_posix_sh_re_execs_under_bash(tmp_path: Path, sandboxed_setup_sh: Path) -> None:
    """`sh aisland/setup.sh` (dash on Ubuntu) used to half-run: `&>` forks a background job,
    `[[` and `source` are "not found". The POSIX guard at the top hands the whole invocation to
    bash instead — the stub bash records what it was exec'd with and stops there."""
    shell = _shell_without_bash_version()
    if shell is None:
        pytest.skip("no shell without BASH_VERSION available — the re-exec guard cannot be made to fire")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bindir, "bash", 'echo "bash $*" >> "$CALLS"; exit 42')

    result = subprocess.run(
        [shell, str(sandboxed_setup_sh), "extra-arg"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "CALLS": str(log)},
        check=False,
    )

    assert result.returncode == 42, result.stdout + result.stderr  # bash's exit, i.e. exec happened
    assert log.read_text().splitlines() == [f"bash {sandboxed_setup_sh} extra-arg"]
    assert "not found" not in result.stderr


def test_setup_sh_no_longer_depends_on_homebrew_for_python() -> None:
    """The brew fallback is what left Linux/WSL dead; it must not creep back."""
    code_lines = [ln for ln in SETUP_SH.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("brew" in ln for ln in code_lines), [ln for ln in code_lines if "brew" in ln]
