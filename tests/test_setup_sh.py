"""Pins for the interpreter-provisioning step of ``setup.sh``.

The step runs BEFORE any venv exists, so it cannot be Python — it is bash, and it is the one
part of the bootstrap that must work on a machine with nothing installed. It used to fall
back to ``brew install python@X``, which is macOS-only: on Ubuntu 24.04 (WSL2, 2026-08-17)
it died with ``Homebrew is not installed`` and no path forward. It now provisions the pinned
interpreter through ``uv`` on every platform, installing ``uv`` itself first when missing.

The block is delimited by ``# --- python-provisioning ---`` markers so these tests can run
it in a real ``bash`` against a scratch ``PATH`` holding stub ``uv`` / ``curl`` / ``pythonX``
executables that record their argv, instead of asserting on the shell text.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

SETUP_SH = Path(__file__).resolve().parents[1] / "setup.sh"
START = "# --- python-provisioning ---"
STOP = "# --- /python-provisioning ---"


def _block() -> str:
    text = SETUP_SH.read_text()
    assert START in text and STOP in text, "setup.sh lost its provisioning markers"
    return text.split(START, 1)[1].split(STOP, 1)[0]


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/bash\n" + body)
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
    return subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, env=env, check=False)


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


def test_a_missing_uv_is_installed_first(sandbox: tuple[Path, Path], tmp_path: Path) -> None:
    """Neither python3.13 nor uv on PATH (the WSL2 case): the installer runs, and the uv it
    puts in ~/.local/bin is what provisions the interpreter — without a new shell."""
    bindir, log = sandbox
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    # The stub "installer" is what `curl … | sh` pipes into `sh`: it drops a uv into ~/.local/bin,
    # exactly where the real astral installer puts it.
    uv_body = (
        "#!/bin/bash\n"
        'echo "uv $*" >> "$CALLS"\n'
        '[ "$1 $2 $3" = "python find --system" ] && echo /store/python3.13\n'
        "exit 0\n"
    )
    installer = tmp_path / "installer.sh"
    installer.write_text(f"cat > '{local_bin}/uv' <<'UVEOF'\n{uv_body}UVEOF\nchmod +x '{local_bin}/uv'\n")
    _stub(bindir, "curl", f'echo "curl $*" >> "$CALLS"\ncat "{installer}"')

    result = _run(bindir, log, home=tmp_path)

    assert result.returncode == 0, result.stderr + result.stdout
    calls = _calls(log)
    assert calls[0].startswith("curl "), calls
    assert "astral.sh/uv/install.sh" in calls[0]
    assert calls[1:] == ["uv python install 3.13", "uv python find --system 3.13"]
    assert "PYTHON_BIN=/store/python3.13" in result.stdout


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


def test_running_under_sh_re_execs_under_bash(tmp_path: Path) -> None:
    """`sh aisland/setup.sh` (dash on Ubuntu) used to half-run: `&>` forks a background job,
    `[[` and `source` are "not found". The POSIX guard at the top hands the whole invocation to
    bash instead — the stub bash records what it was exec'd with and stops there."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bindir, "bash", 'echo "bash $*" >> "$CALLS"; exit 42')

    result = subprocess.run(
        ["sh", str(SETUP_SH), "extra-arg"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "CALLS": str(log)},
        check=False,
    )

    assert result.returncode == 42, result.stdout + result.stderr  # bash's exit, i.e. exec happened
    assert log.read_text().splitlines() == [f"bash {SETUP_SH} extra-arg"]
    assert "not found" not in result.stderr


def test_setup_sh_no_longer_depends_on_homebrew_for_python() -> None:
    """The brew fallback is what left Linux/WSL dead; it must not creep back."""
    code_lines = [ln for ln in SETUP_SH.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("brew" in ln for ln in code_lines), [ln for ln in code_lines if "brew" in ln]
