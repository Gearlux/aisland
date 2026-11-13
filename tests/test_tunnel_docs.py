"""`docs/tunnel.md` — the tunnel's user page, its README pointer and its runnable twin.

What must hold, and why:

- the README points at the page by its ABSOLUTE address (a package index's page resolves no relative
  link), and every link inside the page is relative and lands on a file of the repository;
- the error the page quotes is the one `connect` prints, word for word — a page that quotes an old
  message sends the reader looking for text that no longer appears;
- the page's example runs and asserts what the page says.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Optional

import click
import pytest

from aisland.tunnel import connect as tunnel_connect

PROJECT = Path(__file__).resolve().parents[1]
PAGE = PROJECT / "docs" / "tunnel.md"
VERSION = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
PAGE_URL = f"https://github.com/Gearlux/aisland/blob/v{VERSION}/docs/tunnel.md"
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def test_the_readme_points_at_the_page_by_its_absolute_address() -> None:
    assert PAGE_URL in (PROJECT / "README.md").read_text()
    assert PAGE.is_file()


def test_every_link_in_the_page_is_relative_and_lands_on_a_file() -> None:
    links = _LINK.findall(PAGE.read_text())
    assert links, "the page links to the README, the example and the architecture record"
    for link in links:
        assert not link.startswith(("http://", "https://")), link
        target = (PAGE.parent / link.split("#")[0]).resolve()
        assert target.is_file(), link


def test_the_quoted_port_error_is_the_one_connect_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    quoted = next(line for line in PAGE.read_text().splitlines() if line.startswith("Error: Port "))
    holder = re.search(r"taken by (pid \d+ \([^)]*\))", quoted)
    assert holder is not None, quoted

    def taken(port: int) -> Optional[str]:
        return holder.group(1)

    monkeypatch.setattr(tunnel_connect, "port_holder", taken)
    target = tunnel_connect.Tunnel("brave-otter-7k2m9qx.euw", "box", online=True)
    with pytest.raises(click.ClickException) as raised:
        with tunnel_connect.forward(target):
            pass
    assert quoted == f"Error: {raised.value.message}"


def test_the_pages_example_runs_and_asserts_what_the_page_says() -> None:
    example = PROJECT / "examples" / "01_tunnel_terminal.py"
    result = subprocess.run([sys.executable, str(example)], capture_output=True, text=True, timeout=90, cwd=PROJECT)
    assert result.returncode == 0, result.stderr
    assert "exit status 3: terminal 40x132, start-up file read" in result.stdout
