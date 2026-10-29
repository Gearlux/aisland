"""Which Java runs Jenkins — the one its own WAR file accepts, never just `java` on PATH.

Homebrew's `openjdk` formula follows the newest Java (26 on 2026-10-06), Jenkins supports
only the long-term releases it names ("Supported Java versions are: [21, 25]"), and a shell
profile usually puts the newest JDK first on PATH. These tests build fake JDKs — a `release`
file plus a `java` script answering `java -jar jenkins.war --version` the way Jenkins'
launcher does — and drive the choice, the refusal, `aisland jenkins start`, the plugin
download, provisioning and the doctor check through them.
"""

import subprocess
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
from click.testing import CliRunner

from aisland import doctor
from aisland.cli import main
from aisland.services import jenkins as jenkins_module
from aisland.services import jenkins_java
from aisland.services.jenkins import JenkinsService
from aisland.services.jenkins_java import JavaRuntime, JenkinsJava

#: What the fake Jenkins accepts, as Jenkins 2.555.2 does.
SUPPORTED = (21, 25)


def make_war(path: Path, jenkins_version: str = "2.555.2") -> Path:
    """A WAR carrying only the manifest line Jenkins' launcher reads its version from."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as war:
        war.writestr("META-INF/MANIFEST.MF", f"Manifest-Version: 1.0\nJenkins-Version: {jenkins_version}\n")
    return path


def make_jdk(
    home: Path,
    version: str,
    *,
    accepted: bool,
    supported: Sequence[int] = SUPPORTED,
) -> Path:
    """A fake JDK at ``home``: its `release` file, and a `java` that answers the WAR's version probe.

    An accepted JDK prints the Jenkins version and exits 0; a refused one prints the launcher's
    refusal (exit 1) — or, with ``supported`` empty, dies before the launcher runs, as Java 8
    does on a class file compiled for 11. Every call's argv is appended to ``home/calls``.
    """
    (home / "bin").mkdir(parents=True)
    (home / "release").write_text(f'IMPLEMENTOR="Test"\nJAVA_VERSION="{version}"\n')
    if accepted:
        body = "echo 2.555.2\nexit 0\n"
    elif supported:
        listed = ", ".join(str(major) for major in supported)
        body = (
            f'echo "Running with Java {version} from {home}, which is not yet fully supported."\n'
            f'echo "Supported Java versions are: [{listed}]"\n'
            "exit 1\n"
        )
    else:
        body = 'echo "java.lang.UnsupportedClassVersionError: executable/Main" >&2\nexit 1\n'
    java = home / "bin" / "java"
    java.write_text(f'#!/bin/sh\necho "$@" >> "{home}/calls"\n{body}')
    java.chmod(0o755)
    return java


@pytest.fixture()
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """An empty machine: no JAVA_HOME, a PATH of one empty dir, a Homebrew prefix with no kegs.

    The system JDK folders are redirected into ``tmp_path`` so no test sees the real machine's JDKs.
    """
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    brew = tmp_path / "homebrew"
    (brew / "opt").mkdir(parents=True)
    system = tmp_path / "JavaVirtualMachines"
    system.mkdir()
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setenv("HOMEBREW_PREFIX", str(brew))
    monkeypatch.setattr(jenkins_java, "SYSTEM_JDK_GLOBS", (str(system / "*" / "Contents" / "Home" / "bin" / "java"),))
    return {
        "tmp": tmp_path,
        "path": path_dir,
        "brew": brew,
        "system": system,
        "war": make_war(tmp_path / "jenkins.war"),
    }


def put_on_path(machine: Dict[str, Any], java: Path) -> Path:
    """Link ``java`` into the PATH dir, as a shell profile's `export PATH=<jdk>/bin:$PATH` would expose it."""
    link: Path = machine["path"] / "java"
    link.symlink_to(java)
    return link


def brew_keg(machine: Dict[str, Any], formula: str, version: str, *, accepted: bool) -> Path:
    """A Homebrew keg: the JDK in the Cellar, reached through `<prefix>/opt/<formula>`."""
    brew: Path = machine["brew"]
    cellar = brew / "Cellar" / formula / version
    make_jdk(cellar, version, accepted=accepted)
    (brew / "opt" / formula).symlink_to(cellar)
    return brew / "opt" / formula / "bin" / "java"


# ---------------------------------------------------------------------------
# Reading a JDK and the WAR
# ---------------------------------------------------------------------------


class TestRuntimeAt:
    def test_reads_the_version_from_the_jdks_release_file(self, tmp_path: Path) -> None:
        java = make_jdk(tmp_path / "jdk", "25.0.3", accepted=True)
        runtime = jenkins_java.runtime_at(java)
        assert runtime == JavaRuntime(java, "25.0.3")
        assert runtime is not None and runtime.major == 25

    def test_java_8_reports_major_8(self, tmp_path: Path) -> None:
        runtime = jenkins_java.runtime_at(make_jdk(tmp_path / "jdk", "1.8.0_481", accepted=False, supported=()))
        assert runtime is not None and runtime.major == 8

    def test_a_java_without_a_release_file_is_not_a_runtime(self, tmp_path: Path) -> None:
        """macOS' `/usr/bin/java` stub has no JDK home — running it would pop an install dialog."""
        java = make_jdk(tmp_path / "jdk", "25.0.3", accepted=True)
        (tmp_path / "jdk" / "release").unlink()
        assert jenkins_java.runtime_at(java) is None

    def test_a_missing_java_is_not_a_runtime(self, tmp_path: Path) -> None:
        assert jenkins_java.runtime_at(tmp_path / "nowhere" / "bin" / "java") is None

    def test_a_release_file_without_a_version_is_not_a_runtime(self, tmp_path: Path) -> None:
        java = make_jdk(tmp_path / "jdk", "25.0.3", accepted=True)
        (tmp_path / "jdk" / "release").write_text('IMPLEMENTOR="Test"\n')
        assert jenkins_java.runtime_at(java) is None


class TestJenkinsVersion:
    def test_read_from_the_war_manifest(self, tmp_path: Path) -> None:
        assert jenkins_java.jenkins_version(make_war(tmp_path / "jenkins.war", "2.555.2")) == "2.555.2"

    def test_a_war_without_the_line_reports_unknown(self, tmp_path: Path) -> None:
        war = tmp_path / "jenkins.war"
        with zipfile.ZipFile(war, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        assert jenkins_java.jenkins_version(war) == "unknown"

    def test_a_file_that_is_not_a_war_reports_unknown(self, tmp_path: Path) -> None:
        war = tmp_path / "jenkins.war"
        war.write_text("half a download")
        assert jenkins_java.jenkins_version(war) == "unknown"


class TestProbe:
    def test_an_accepted_java_answers_with_the_jenkins_version(self, machine: Dict[str, Any]) -> None:
        java = make_jdk(machine["tmp"] / "jdk", "25.0.3", accepted=True)
        assert jenkins_java.probe(machine["war"], java) == (True, ())
        assert (machine["tmp"] / "jdk" / "calls").read_text().split() == ["-jar", str(machine["war"]), "--version"]

    def test_a_refused_java_hands_back_the_versions_jenkins_names(self, machine: Dict[str, Any]) -> None:
        java = make_jdk(machine["tmp"] / "jdk", "26.0.2.1", accepted=False)
        assert jenkins_java.probe(machine["war"], java) == (False, (21, 25))

    def test_a_java_that_cannot_run_the_launcher_names_nothing(self, machine: Dict[str, Any]) -> None:
        java = make_jdk(machine["tmp"] / "jdk", "1.8.0_481", accepted=False, supported=())
        assert jenkins_java.probe(machine["war"], java) == (False, ())

    def test_a_hung_java_is_refused(self, machine: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        def _hang(*args: Any, **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd="java", timeout=kwargs.get("timeout", 0))

        monkeypatch.setattr(jenkins_java.subprocess, "run", _hang)
        assert jenkins_java.probe(machine["war"], machine["tmp"] / "java") == (False, ())


# ---------------------------------------------------------------------------
# Finding the installed JDKs
# ---------------------------------------------------------------------------


class TestFindRuntimes:
    def test_java_home_then_path_then_homebrew_then_system(self, machine: Dict[str, Any], monkeypatch: Any) -> None:
        home_java = make_jdk(machine["tmp"] / "java-home", "21.0.8", accepted=True)
        monkeypatch.setenv("JAVA_HOME", str(machine["tmp"] / "java-home"))
        path_java = put_on_path(machine, make_jdk(machine["tmp"] / "on-path", "26.0.2.1", accepted=False))
        keg_java = brew_keg(machine, "openjdk@25", "25.0.3", accepted=True)
        system_java = make_jdk(machine["system"] / "corretto-11.jdk" / "Contents" / "Home", "11.0.29", accepted=False)

        runtimes = jenkins_java.find_runtimes()

        assert [runtime.java for runtime in runtimes] == [home_java, path_java, keg_java, system_java]
        assert [runtime.version for runtime in runtimes] == ["21.0.8", "26.0.2.1", "25.0.3", "11.0.29"]

    def test_one_jdk_reached_by_several_paths_is_listed_once_by_its_first_path(self, machine: Dict[str, Any]) -> None:
        """`opt/openjdk`, `opt/openjdk@26` and a JavaVirtualMachines link are all ONE Cellar JDK."""
        keg_java = brew_keg(machine, "openjdk", "26.0.2.1", accepted=False)
        (machine["brew"] / "opt" / "openjdk@26").symlink_to(machine["brew"] / "Cellar" / "openjdk" / "26.0.2.1")
        (machine["system"] / "openjdk.jdk" / "Contents").mkdir(parents=True)
        (machine["system"] / "openjdk.jdk" / "Contents" / "Home").symlink_to(
            machine["brew"] / "Cellar" / "openjdk" / "26.0.2.1"
        )
        put_on_path(machine, keg_java)

        runtimes = jenkins_java.find_runtimes()

        assert runtimes == [JavaRuntime(machine["path"] / "java", "26.0.2.1")]

    def test_homebrew_found_through_brew_on_path_without_homebrew_prefix(
        self, machine: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        keg_java = brew_keg(machine, "openjdk@25", "25.0.3", accepted=True)
        (machine["brew"] / "bin").mkdir()
        brew = machine["brew"] / "bin" / "brew"
        brew.write_text("#!/bin/sh\n")
        brew.chmod(0o755)
        monkeypatch.delenv("HOMEBREW_PREFIX")
        monkeypatch.setenv("PATH", f"{machine['path']}:{machine['brew'] / 'bin'}")

        assert [runtime.java for runtime in jenkins_java.find_runtimes()] == [keg_java]

    def test_no_homebrew_and_no_java_finds_nothing(self, machine: Dict[str, Any], monkeypatch: Any) -> None:
        monkeypatch.delenv("HOMEBREW_PREFIX")
        assert jenkins_java.find_runtimes() == []


# ---------------------------------------------------------------------------
# Choosing — the newest Java the WAR accepts
# ---------------------------------------------------------------------------


class TestChooseJava:
    def test_newest_accepted_java_wins_over_the_newer_one_first_on_path(self, machine: Dict[str, Any]) -> None:
        """The machine that broke on 2026-10-07: PATH says 26, Homebrew also holds 25."""
        put_on_path(machine, brew_keg(machine, "openjdk", "26.0.2.1", accepted=False))
        keg_25 = brew_keg(machine, "openjdk@25", "25.0.3", accepted=True)
        make_jdk(machine["system"] / "corretto-11.jdk" / "Contents" / "Home", "11.0.29", accepted=False)

        choice = jenkins_java.choose_java(machine["war"], jenkins_java.find_runtimes())

        assert choice.runtime == JavaRuntime(keg_25, "25.0.3")
        assert choice.supported == SUPPORTED
        assert choice.describe() == f"Jenkins 2.555.2 runs on Java 25.0.3 ({keg_25})"

    def test_a_path_java_jenkins_accepts_is_kept(self, machine: Dict[str, Any]) -> None:
        on_path = put_on_path(machine, make_jdk(machine["tmp"] / "jdk-25", "25.0.3", accepted=True))

        choice = jenkins_java.choose_java(machine["war"], jenkins_java.find_runtimes())

        assert choice.runtime == JavaRuntime(on_path, "25.0.3")

    def test_the_newest_of_two_accepted_javas_is_chosen_and_older_ones_are_not_probed(
        self, machine: Dict[str, Any]
    ) -> None:
        put_on_path(machine, make_jdk(machine["tmp"] / "jdk-21", "21.0.8", accepted=True))
        keg_25 = brew_keg(machine, "openjdk@25", "25.0.3", accepted=True)

        choice = jenkins_java.choose_java(machine["war"], jenkins_java.find_runtimes())

        assert choice.runtime is not None and choice.runtime.java == keg_25
        assert not (machine["tmp"] / "jdk-21" / "calls").exists()

    def test_no_accepted_java_names_the_newest_supported_formula(self, machine: Dict[str, Any]) -> None:
        on_path = put_on_path(machine, brew_keg(machine, "openjdk", "26.0.2.1", accepted=False))

        choice = jenkins_java.choose_java(machine["war"], jenkins_java.find_runtimes())

        assert choice.runtime is None
        assert choice.rejected == (JavaRuntime(on_path, "26.0.2.1"),)
        assert choice.formula == "openjdk@25"

    def test_no_java_at_all(self, machine: Dict[str, Any]) -> None:
        choice = jenkins_java.choose_java(machine["war"], [])
        assert choice == JenkinsJava("2.555.2", None, (), ())
        assert choice.formula is None


class TestMessages:
    """The words a user reads when Jenkins cannot start — the problem line and the fix."""

    REFUSED = JenkinsJava(
        "2.555.2",
        None,
        (21, 25),
        (
            JavaRuntime(Path("/opt/homebrew/opt/openjdk/bin/java"), "26.0.2.1"),
            JavaRuntime(Path("/Library/Java/JavaVirtualMachines/corretto-11.jdk/Contents/Home/bin/java"), "11.0.29"),
        ),
    )

    def test_refused_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Darwin")
        assert self.REFUSED.refusal() == (
            "Error: Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26.0.2.1, 11.0.29).\n"
            "  Java 26.0.2.1: /opt/homebrew/opt/openjdk/bin/java\n"
            "  Java 11.0.29: /Library/Java/JavaVirtualMachines/corretto-11.jdk/Contents/Home/bin/java\n"
            "Fix: brew install openjdk@25"
        )

    def test_refused_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Linux")
        assert self.REFUSED.remedy() == "install a Java 25 JDK and set JAVA_HOME to its home directory"

    def test_three_supported_versions_read_as_a_list(self) -> None:
        choice = JenkinsJava("2.479.3", None, (17, 21, 25), ())
        assert choice.problem() == "Jenkins 2.479.3 runs on Java 17, 21 or 25, and none is installed"

    def test_no_java_on_macos_points_at_provision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Darwin")
        choice = JenkinsJava("2.555.2", None, (), ())
        assert choice.refusal() == (
            "Error: Jenkins 2.555.2 needs a Java, and none is installed.\n"
            "Fix: aisland workspace provision install   # installs the Java this Jenkins supports"
        )

    def test_only_javas_too_old_to_ask_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Linux")
        choice = JenkinsJava("2.555.2", None, (), (JavaRuntime(Path("/usr/lib/jvm/java-8/bin/java"), "1.8.0_481"),))
        assert choice.problem() == "Jenkins 2.555.2 refuses every installed Java (found 1.8.0_481)"
        assert choice.remedy() == (
            "install a JDK this Jenkins supports (https://jenkins.io/redirect/java-support/) "
            "and set JAVA_HOME to its home directory"
        )


class TestNewestVersionedFormula:
    def test_the_highest_numbered_openjdk_formula(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: List[List[str]] = []

        def _search(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, "openjdk@11\nopenjdk@17\nopenjdk@25\nopenjdk@27\nopenjdk@8\n", ""
            )

        monkeypatch.setattr(jenkins_java.shutil, "which", lambda name: "/opt/homebrew/bin/brew")
        monkeypatch.setattr(jenkins_java.subprocess, "run", _search)

        assert jenkins_java.newest_versioned_formula() == "openjdk@27"
        assert calls == [["brew", "search", "--formula", "/^openjdk@[0-9]+$/"]]

    def test_none_without_homebrew(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jenkins_java.shutil, "which", lambda name: None)
        assert jenkins_java.newest_versioned_formula() is None

    def test_none_when_the_search_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jenkins_java.shutil, "which", lambda name: "/opt/homebrew/bin/brew")
        monkeypatch.setattr(
            jenkins_java.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "offline")
        )
        assert jenkins_java.newest_versioned_formula() is None


# ---------------------------------------------------------------------------
# `aisland jenkins start` and the plugin download
# ---------------------------------------------------------------------------


@pytest.fixture()
def jenkins_bin(machine: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A JenkinsService whose bin dir holds the fake WAR, with downloads and the supervisor stubbed."""
    bin_dir = machine["tmp"] / "bin"
    make_war(bin_dir / "jenkins.war")
    monkeypatch.setattr(jenkins_module, "BIN_DIR", bin_dir)
    monkeypatch.setattr(JenkinsService, "setup", lambda self: None)
    monkeypatch.setattr(JenkinsService, "check_credentials_and_print_instructions", lambda self, port: None)
    started: List[List[str]] = []
    monkeypatch.setattr(JenkinsService, "start", lambda self, cmd, **kwargs: started.append(cmd))
    return {**machine, "started": started}


class TestJenkinsStart:
    def test_starts_on_the_accepted_java_not_the_one_first_on_path(self, jenkins_bin: Dict[str, Any]) -> None:
        put_on_path(jenkins_bin, brew_keg(jenkins_bin, "openjdk", "26.0.2.1", accepted=False))
        keg_25 = brew_keg(jenkins_bin, "openjdk@25", "25.0.3", accepted=True)

        result = CliRunner().invoke(main, ["jenkins", "start", "--background"])

        assert result.exit_code == 0, result.output
        assert f"Jenkins 2.555.2 runs on Java 25.0.3 ({keg_25})" in result.output
        [cmd] = jenkins_bin["started"]
        assert cmd[0] == str(keg_25)
        assert cmd[-3:] == ["-jar", str(jenkins_bin["tmp"] / "bin" / "jenkins.war"), "--httpPort=9080"]

    def test_refuses_with_the_fix_when_no_installed_java_is_accepted(
        self, jenkins_bin: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Darwin")
        put_on_path(jenkins_bin, brew_keg(jenkins_bin, "openjdk", "26.0.2.1", accepted=False))

        result = CliRunner().invoke(main, ["jenkins", "start", "--background"])

        assert result.exit_code == 1
        assert "Error: Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26.0.2.1)." in result.output
        assert "Fix: brew install openjdk@25" in result.output
        assert jenkins_bin["started"] == []


class TestPluginDownload:
    def test_the_plugin_manager_runs_on_the_chosen_java(
        self, jenkins_bin: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        keg_25 = brew_keg(jenkins_bin, "openjdk@25", "25.0.3", accepted=True)
        service = JenkinsService()
        service.service_data_dir = jenkins_bin["tmp"] / "jenkins-home"
        monkeypatch.setattr(service, "_are_plugins_installed", lambda: False)
        choice = service.resolve_java()
        monkeypatch.setattr(service, "resolve_java", lambda: choice)
        ran: List[List[str]] = []

        def _run(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "Done", "")

        monkeypatch.setattr(subprocess, "run", _run)

        assert service.setup_plugins() is True
        assert ran[0][:2] == [str(keg_25), "-jar"]


# ---------------------------------------------------------------------------
# Provisioning — `aisland setup` / `aisland workspace provision install`
# ---------------------------------------------------------------------------


def _choices(*sequence: JenkinsJava) -> Any:
    """A `resolve_java` stand-in answering each call with the next choice in ``sequence``."""
    queue = list(sequence)
    return lambda: queue.pop(0)


ACCEPTED_25 = JenkinsJava(
    "2.555.2", JavaRuntime(Path("/opt/homebrew/opt/openjdk@25/bin/java"), "25.0.4.1"), (21, 25), ()
)
REFUSED_26 = JenkinsJava("2.555.2", None, (21, 25), (JavaRuntime(Path("/opt/homebrew/opt/openjdk/bin/java"), "26"),))
NO_JAVA = JenkinsJava("2.555.2", None, (), ())
REFUSED_27 = JenkinsJava("2.555.2", None, (21, 25), (JavaRuntime(Path("/opt/homebrew/opt/openjdk@27/bin/java"), "27"),))


@pytest.fixture()
def provisioning(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """A JenkinsService whose WAR is 'present', with Homebrew installs recorded instead of run."""
    service = JenkinsService()
    monkeypatch.setattr(service, "ensure_war", lambda: True)
    installs: List[set] = []
    monkeypatch.setattr(jenkins_module, "install_brew_packages", lambda packages: installs.append(set(packages)))
    monkeypatch.setattr(jenkins_module, "newest_versioned_formula", lambda: "openjdk@27")
    return {"service": service, "installs": installs}


class TestProvisionJava:
    def test_an_accepted_java_installs_nothing(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices(ACCEPTED_25))
        provisioning["service"].provision_java()
        assert provisioning["installs"] == []
        assert (
            "Jenkins 2.555.2 runs on Java 25.0.4.1 (/opt/homebrew/opt/openjdk@25/bin/java)" in capsys.readouterr().out
        )

    def test_a_refused_java_installs_the_formula_jenkins_names(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices(REFUSED_26, ACCEPTED_25))
        provisioning["service"].provision_java()
        assert provisioning["installs"] == [{"openjdk@25"}]

    def test_no_java_bootstraps_with_the_newest_formula_then_installs_the_named_one(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices(NO_JAVA, REFUSED_27, ACCEPTED_25))
        provisioning["service"].provision_java()
        assert provisioning["installs"] == [{"openjdk@27"}, {"openjdk@25"}]

    def test_only_a_java_too_old_to_ask_bootstraps_too(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        only_8 = JenkinsJava("2.555.2", None, (), (JavaRuntime(Path("/usr/bin/java8"), "1.8.0_481"),))
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices(only_8, REFUSED_27, ACCEPTED_25))
        provisioning["service"].provision_java()
        assert provisioning["installs"] == [{"openjdk@27"}, {"openjdk@25"}]

    def test_a_failed_war_download_skips_without_exiting(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(provisioning["service"], "ensure_war", lambda: False)
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices())
        provisioning["service"].provision_java()
        assert provisioning["installs"] == []
        assert "Skipped: without the Jenkins WAR there is no asking which Java it runs on." in capsys.readouterr().out

    def test_a_bootstrap_jenkins_accepts_is_the_only_install(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices(NO_JAVA, ACCEPTED_25))
        monkeypatch.setattr(jenkins_module, "newest_versioned_formula", lambda: "openjdk@25")
        provisioning["service"].provision_java()
        assert provisioning["installs"] == [{"openjdk@25"}]

    def test_still_nothing_accepted_prints_the_refusal_and_does_not_raise(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices(REFUSED_26, REFUSED_26))
        provisioning["service"].provision_java()
        assert provisioning["installs"] == [{"openjdk@25"}]
        assert "Fix: brew install openjdk@25" in capsys.readouterr().out

    def test_no_homebrew_formula_to_bootstrap_with(
        self, provisioning: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(jenkins_module, "newest_versioned_formula", lambda: None)
        monkeypatch.setattr(provisioning["service"], "resolve_java", _choices(NO_JAVA))
        provisioning["service"].provision_java()
        assert provisioning["installs"] == []
        assert "Error: Jenkins 2.555.2 needs a Java, and none is installed." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# `aisland doctor`
# ---------------------------------------------------------------------------


class TestDoctorCheck:
    def _check(self, monkeypatch: pytest.MonkeyPatch, choice: Optional[JenkinsJava]) -> doctor.CheckResult:
        monkeypatch.setattr(doctor, "_jenkins_java", lambda: choice)
        return doctor.check_jenkins_java()

    def test_no_jenkins_downloaded_is_not_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._check(monkeypatch, None)
        assert (result.name, result.ok) == ("jenkins-java", True)
        assert result.detail == "Jenkins not downloaded yet — no Java to check"

    def test_an_accepted_java_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = self._check(monkeypatch, ACCEPTED_25)
        assert result.ok
        assert result.detail == "Jenkins 2.555.2 runs on Java 25.0.4.1 (/opt/homebrew/opt/openjdk@25/bin/java)"

    def test_a_refused_java_fails_with_the_install_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(jenkins_java.platform, "system", lambda: "Darwin")
        result = self._check(monkeypatch, REFUSED_26)
        assert not result.ok
        assert result.detail == "Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26)"
        assert result.remedy == "brew install openjdk@25"

    def test_is_registered(self) -> None:
        assert doctor.check_jenkins_java in doctor.CHECKS

    def test_the_seam_reads_the_downloaded_war(self, jenkins_bin: Dict[str, Any]) -> None:
        keg_25 = brew_keg(jenkins_bin, "openjdk@25", "25.0.3", accepted=True)
        choice = doctor._jenkins_java()
        assert choice is not None and choice.runtime == JavaRuntime(keg_25, "25.0.3")

    def test_the_seam_is_none_without_a_war(self, jenkins_bin: Dict[str, Any]) -> None:
        (jenkins_bin["tmp"] / "bin" / "jenkins.war").unlink()
        assert doctor._jenkins_java() is None


def test_info_shows_the_java(jenkins_bin: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    keg_25 = brew_keg(jenkins_bin, "openjdk@25", "25.0.3", accepted=True)
    monkeypatch.setattr(jenkins_module.BaseService, "info", lambda self: None)
    monkeypatch.setattr(JenkinsService, "get_ports", lambda self: [])
    result = CliRunner().invoke(main, ["jenkins", "info"])
    assert result.exit_code == 0, result.output
    assert f"  Java          : Jenkins 2.555.2 runs on Java 25.0.3 ({keg_25})" in result.output
