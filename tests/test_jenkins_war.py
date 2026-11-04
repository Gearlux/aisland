"""The Jenkins WAR is a release pinned by version and sha256: a download that is not it is refused, and a refused or
interrupted one leaves no WAR behind."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterator, List

import pytest

from aisland import failures, paths
from aisland.services import jenkins

WAR = b"PK-a-war-that-is-not-jenkins"


@pytest.fixture(autouse=True)
def clean_slate() -> Iterator[None]:
    failures.reset()
    yield
    failures.reset()


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> jenkins.JenkinsService:
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    war = jenkins.JenkinsService()
    war.war_file = tmp_path / "bin" / "jenkins.war"
    return war


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """The WAR's download is answered with ``WAR``; the urls asked for are recorded."""
    asked: List[str] = []

    def fetch(url: str, target: Path) -> None:
        asked.append(url)
        target.write_bytes(WAR)

    monkeypatch.setattr(jenkins, "download", fetch)
    return asked


def test_the_release_is_named_by_its_version_and_its_digest() -> None:
    assert jenkins.JenkinsService.URL == (
        f"https://get.jenkins.io/war-stable/{jenkins.JenkinsService.WAR_VERSION}/jenkins.war"
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+", jenkins.JenkinsService.WAR_VERSION)
    assert re.fullmatch(r"[0-9a-f]{64}", jenkins.JenkinsService.WAR_SHA256)


def test_a_download_with_the_pinned_digest_is_installed_whole(
    service: jenkins.JenkinsService, served: List[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jenkins.JenkinsService, "WAR_SHA256", hashlib.sha256(WAR).hexdigest())

    assert service.ensure_war() is True

    assert served == [jenkins.JenkinsService.URL]
    assert service.war_file.read_bytes() == WAR
    assert sorted(path.name for path in service.war_file.parent.iterdir()) == ["jenkins.war"]
    assert failures.reported() == 0


def test_a_download_with_another_digest_is_refused_and_nothing_is_installed(
    service: jenkins.JenkinsService, served: List[str]
) -> None:
    assert service.ensure_war() is False

    assert not service.war_file.exists()
    assert list(service.war_file.parent.iterdir()) == []
    assert failures.reported() == 1


def test_a_download_that_fails_leaves_no_part_file(
    service: jenkins.JenkinsService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fetch(url: str, target: Path) -> None:
        target.write_bytes(WAR[:5])
        raise OSError("connection reset")

    monkeypatch.setattr(jenkins, "download", fetch)

    assert service.ensure_war() is False

    assert list(service.war_file.parent.iterdir()) == []
    assert failures.reported() == 1


def test_a_war_already_in_the_bin_folder_is_used_as_it_is(service: jenkins.JenkinsService, served: List[str]) -> None:
    service.war_file.parent.mkdir(parents=True)
    service.war_file.write_bytes(b"another release")

    assert service.ensure_war() is True

    assert served == []
    assert service.war_file.read_bytes() == b"another release"
