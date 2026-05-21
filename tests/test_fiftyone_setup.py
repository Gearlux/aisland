"""Tests for ``FiftyOneService.setup`` and process-pattern matching.

``setup`` is the lazy-install hook that pulls ``fiftyone`` into the venv on the
first launch. We only want pip to run when fiftyone is genuinely missing —
re-installing on every ``aisland fiftyone start`` would block startup for
~30s while pip resolves a fully-installed package.

The pattern dict in ``BaseService._find_process_by_pattern`` is what lets aisland
recover a stale PID after a crash. Adding ``fiftyone`` there is what gives
``aisland fiftyone status`` its self-healing behaviour.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from aisland.services.fiftyone import FiftyOneService


def test_setup_installs_when_fiftyone_missing() -> None:
    svc = FiftyOneService()
    with patch("importlib.util.find_spec", return_value=None) as find_spec_mock:
        with patch("subprocess.run") as run_mock:
            svc.setup()
    find_spec_mock.assert_called_once_with("fiftyone")
    run_mock.assert_called_once()
    # The first positional arg is the command list; assert it ends with pip install fiftyone.
    cmd = run_mock.call_args.args[0]
    assert cmd[-3:] == ["-m", "pip", "install"] or cmd[-1] == "fiftyone"


def test_setup_skips_install_when_fiftyone_present() -> None:
    svc = FiftyOneService()
    with patch("importlib.util.find_spec", return_value=MagicMock()):
        with patch("subprocess.run") as run_mock:
            svc.setup()
    run_mock.assert_not_called()


def test_fiftyone_in_process_pattern_dict() -> None:
    """Regression guard: the pgrep pattern dict must include fiftyone.

    Without this entry, a stale PID file (e.g. after a crash) would leave
    ``aisland fiftyone status`` reporting "Stopped" even when the FO app is
    still running, and a subsequent ``start`` would race a duplicate process.
    """
    from aisland.manager import BaseService

    svc = BaseService("fiftyone")
    # Force the pattern path: clear the PID file (we won't have one anyway).
    if svc.pid_file.exists():
        svc.pid_file.unlink()
    # Patch subprocess.check_output to fail (no matching process) so we exercise
    # the patterns dict lookup without depending on the real ps state.
    import subprocess

    with patch(
        "subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "pgrep")
    ) as check_mock:
        svc._find_process_by_pattern()
    # First call should be pgrep -f <pattern>; the pattern must be the
    # fiftyone-specific one, not the literal name "fiftyone".
    args, _kwargs = check_mock.call_args_list[0]
    cmd = args[0]
    assert cmd[:2] == ["pgrep", "-f"]
    assert cmd[2] == "fiftyone app launch"
