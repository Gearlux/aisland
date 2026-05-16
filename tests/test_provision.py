"""Tests for aisland.manager.provision_system and shell-switch offer.

Covers the deterministic early-return branches of `_maybe_offer_shell_switch`.
The interactive sudo / chsh paths are not exercised here — they require
mocking subprocess + tty + interactive prompts and the behavior is shell-
specific in a way unit tests can't usefully verify.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aisland import manager


def test_shell_switch_skipped_on_non_darwin(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """On Linux / Windows the chsh flow is irrelevant — must early-return silently."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Linux", raising=False)
    manager._maybe_offer_shell_switch({"bash"})
    assert capsys.readouterr().out == ""


def test_shell_switch_skipped_when_bash_not_required(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """If `bash` is not in the project's sys-deps, the offer must not appear."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    manager._maybe_offer_shell_switch({"libomp", "openjdk"})
    assert capsys.readouterr().out == ""


def test_shell_switch_skipped_when_brew_bash_missing(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """If neither /opt/homebrew/bin/bash nor /usr/local/bin/bash exists, warn + skip."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    manager._maybe_offer_shell_switch({"bash"})
    out = capsys.readouterr().out
    assert "Homebrew bash not found" in out


def test_shell_switch_silent_when_already_current(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """If SHELL already points to brew bash, no offer should appear (idempotency)."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    # Pretend /opt/homebrew/bin/bash exists, /usr/local one doesn't.
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/opt/homebrew/bin/bash":
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/bash")
    manager._maybe_offer_shell_switch({"bash"})
    assert capsys.readouterr().out == ""


def test_shell_switch_declined_exits_cleanly(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """User declines the prompt — function must return without touching the system."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin", raising=False)
    real_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        if str(self) == "/opt/homebrew/bin/bash":
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setenv("SHELL", "/bin/bash")
    # click.confirm reads from stdin; mock it to "no".
    monkeypatch.setattr(manager.click, "confirm", lambda *a, **kw: False)

    # Spy subprocess.run/check_call so we can assert nothing was invoked.
    calls: list = []

    def _record(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise AssertionError("subprocess should NOT be invoked when user declines")

    monkeypatch.setattr(manager.subprocess, "run", _record)
    monkeypatch.setattr(manager.subprocess, "check_call", _record)

    manager._maybe_offer_shell_switch({"bash"})
    out = capsys.readouterr().out
    assert "Modern bash is installed" in out
    assert calls == []
