"""Regression test for the `init_pipelines` write-config-while-running race.

Pre-fix, `setup_global_properties` was called while Jenkins was still running,
and Jenkins's in-memory model would silently overwrite the new
`<globalNodeProperties>` block on its next save — leaving `env.WORKSPACE_ROOT`
unset and breaking every subsequent build with `null/.venv/bin/python3: No
such file or directory`.

The fix stops Jenkins before any config.xml mutation and cold-starts it
after. We assert the call order by source inspection (mocking the real
`init_pipelines` is brittle because it resolves the workspace root via
`Path(__file__).resolve().parents[3]`).
"""

from __future__ import annotations

import inspect

from aisland.services.jenkins import JenkinsService


def test_init_pipelines_stops_before_writing_global_config() -> None:
    """`stop()` must appear before `setup_global_properties()` in init_pipelines."""
    src = inspect.getsource(JenkinsService.init_pipelines)
    # The stop must precede setup_global_properties to prevent Jenkins
    # from overwriting our config.xml edits with its in-memory state.
    assert "self.stop()" in src, "init_pipelines must stop Jenkins before mutating config.xml"
    assert "self.setup_global_properties()" in src
    stop_pos = src.index("self.stop()")
    setup_props_pos = src.index("self.setup_global_properties()")
    assert stop_pos < setup_props_pos, (
        "stop() must be called BEFORE setup_global_properties() — otherwise the "
        "running Jenkins overwrites globalNodeProperties on its next save"
    )


def test_init_pipelines_final_launch_is_after_writing_global_config() -> None:
    """The FINAL `launch()` must appear after `setup_global_properties()` — the
    warm-start launch at the top of init_pipelines doesn't count, because we
    immediately stop it again before mutating config.xml.
    """
    src = inspect.getsource(JenkinsService.init_pipelines)
    assert "self.launch(" in src
    setup_props_pos = src.index("self.setup_global_properties()")
    # rfind = last occurrence, which is the cold-start that loads our edits.
    final_launch_pos = src.rfind("self.launch(")
    assert setup_props_pos < final_launch_pos, "final launch() must follow setup_global_properties()"


def test_init_pipelines_warm_starts_when_jenkins_is_not_running() -> None:
    """On a clean reset Jenkins is not running, so we must warm-start it
    so config.xml gets generated BEFORE setup_global_properties runs.
    Without this warm-start, setup_global_properties early-returns silently
    (because config.xml doesn't exist), env.WORKSPACE_ROOT stays null, and
    every subsequent build fails with `null/.venv/bin/python3: No such file`.
    """
    src = inspect.getsource(JenkinsService.init_pipelines)
    assert (
        "if not self.is_running()" in src
    ), "init_pipelines must warm-start Jenkins (when not already running) before mutating config.xml"
    # The warm-start launch must precede the stop+writes.
    warm_pos = src.index("if not self.is_running()")
    setup_props_pos = src.index("self.setup_global_properties()")
    assert warm_pos < setup_props_pos
    # And waiting for config.xml to appear is what makes it deterministic.
    assert (
        "config_file.exists()" in src or 'config_file = self.service_data_dir / "config.xml"' in src
    ), "warm-start must wait for config.xml to materialize, otherwise the stop may race the file creation"
