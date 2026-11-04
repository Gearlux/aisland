"""`aisland.downloads.download`: every file aisland fetches has a time limit, and a failed fetch leaves no part file."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Iterator

import pytest

from aisland import downloads
from aisland.wheels import build as wheels


@pytest.fixture
def silent_server() -> Iterator[str]:
    """A server that accepts a connection and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted = []
    thread = threading.Thread(target=lambda: accepted.append(server.accept()), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/file"
    server.close()
    for connection, _ in accepted:
        connection.close()


def test_a_server_that_never_answers_fails_within_the_limit_and_leaves_no_file(
    silent_server: str, tmp_path: Path
) -> None:
    target = tmp_path / "file"
    with pytest.raises(OSError):
        downloads.download(silent_server, target, timeout=0.5)
    assert not target.exists()


def test_a_file_is_fetched_whole(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x" * 100_000)
    target = tmp_path / "target"
    downloads.download(source.as_uri(), target)
    assert target.read_bytes() == source.read_bytes()


def test_a_wheel_the_comparison_needs_is_fetched_whole_into_its_folder(tmp_path: Path) -> None:
    source = tmp_path / "source.whl"
    source.write_bytes(b"w" * 100_000)

    fetched = wheels._download(source.as_uri(), tmp_path / "pypi" / "nested" / "lib-1.0-py3-none-any.whl")

    assert fetched.read_bytes() == source.read_bytes()


def test_a_wheel_that_cannot_be_fetched_leaves_no_part_file(tmp_path: Path) -> None:
    target = tmp_path / "pypi" / "lib-1.0-py3-none-any.whl"

    with pytest.raises(OSError):
        wheels._download((tmp_path / "missing.whl").as_uri(), target)

    assert not target.exists()
