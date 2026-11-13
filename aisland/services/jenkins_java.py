"""Which Java runs Jenkins — the newest installed one its own WAR file accepts.

Jenkins supports only the long-term Java releases its launcher names, and refuses any other
before it starts::

    $ java -jar jenkins.war --version          # java 26 first on PATH
    Running with Java 26 from …, which is not yet fully supported.
    Supported Java versions are: [21, 25]

Homebrew's `openjdk` formula follows the newest Java whatever Jenkins supports, and a shell
profile usually puts that JDK first on PATH — so a `java` taken from PATH breaks Jenkins every
time Java moves past what the downloaded WAR knows (26 on 2026-10-06). The WAR is the only
authority on what it runs on, so this module asks it: every installed JDK is found (from its
`release` file — no JVM is started to find it), the newest are probed first with
`java -jar jenkins.war --version`, and the first one the launcher accepts is the one Jenkins
runs on, by its full path. No Java version is written into aisland. Rationale:
`docs/decisions/0018-jenkins-runs-on-the-java-its-war-accepts-by-full-path.md`.
"""

import glob
import os
import platform
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Set, Tuple

#: Where a JDK installed outside Homebrew and JAVA_HOME lives: the macOS installers' folder (which
#: also holds the links a Homebrew caveat asks for) and the Linux distributions' one.
SYSTEM_JDK_GLOBS: Tuple[str, ...] = (
    "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
    "/usr/lib/jvm/*/bin/java",
)

#: The Homebrew formulae holding one Java release each (`openjdk@25`); the bare `openjdk` follows
#: the newest Java and is never what aisland installs.
VERSIONED_FORMULA = "openjdk@"
#: How long `brew search` may take: it may read the formula index over the network.
BREW_SEARCH_SECONDS = 120

#: Seconds a `--version` probe may take; the launcher answers in about 0.1 s.
PROBE_TIMEOUT = 60

_SUPPORTED_RE = re.compile(r"Supported Java versions are: \[([0-9,\s]*)\]")
_JAVA_VERSION_RE = re.compile(r'^JAVA_VERSION="([^"]+)"', re.MULTILINE)
_JENKINS_VERSION_RE = re.compile(r"^Jenkins-Version:\s*(\S+)", re.MULTILINE)
_FORMULA_RE = re.compile(rf"^{re.escape(VERSIONED_FORMULA)}(\d+)$")

_JAVA_SUPPORT_URL = "https://jenkins.io/redirect/java-support/"


@dataclass(frozen=True)
class JavaRuntime:
    """One installed JDK.

    Args:
        java: The `java` executable, as it was found (`/opt/homebrew/opt/openjdk@25/bin/java`,
            not its Cellar path — the `opt` link survives a `brew upgrade`).
        version: `JAVA_VERSION` from the JDK's `release` file (`25.0.3`, `1.8.0_481`).
    """

    java: Path
    version: str

    @property
    def major(self) -> int:
        """The Java release number Jenkins speaks of: 25 for `25.0.3`, 8 for `1.8.0_481`."""
        parts = self._numbers
        return parts[1] if parts[0] == 1 and len(parts) > 1 else parts[0]

    @property
    def _numbers(self) -> Tuple[int, ...]:
        return tuple(int(number) for number in re.findall(r"\d+", self.version)) or (0,)

    def sort_key(self) -> Tuple[int, ...]:
        """Orders runtimes by version — major first, then the update numbers."""
        numbers = self._numbers
        return (self.major, *numbers[2:]) if numbers[0] == 1 else numbers


@dataclass(frozen=True)
class JenkinsJava:
    """What the WAR said about the installed JDKs.

    Args:
        jenkins_version: `Jenkins-Version` from the WAR's manifest.
        runtime: The newest JDK the launcher accepted; None when it accepted none.
        supported: The Java releases the launcher named on refusing one — empty when nothing
            was refused, or when no JDK could even run the launcher.
        rejected: The JDKs probed and refused, newest first.
    """

    jenkins_version: str
    runtime: Optional[JavaRuntime]
    supported: Tuple[int, ...]
    rejected: Tuple[JavaRuntime, ...]

    @property
    def formula(self) -> Optional[str]:
        """The Homebrew formula of the newest Java the launcher named (`openjdk@25`), if it named any."""
        return f"{VERSIONED_FORMULA}{max(self.supported)}" if self.supported else None

    def describe(self) -> str:
        """One line naming the chosen JDK: `Jenkins 2.555.2 runs on Java 25.0.3 (<java>)`."""
        if self.runtime is None:
            return self.problem()
        return f"Jenkins {self.jenkins_version} runs on Java {self.runtime.version} ({self.runtime.java})"

    def problem(self) -> str:
        """One line saying why no JDK was chosen (no trailing period)."""
        found = ", ".join(runtime.version for runtime in self.rejected)
        if self.supported:
            return f"Jenkins {self.jenkins_version} runs on Java {_spoken(self.supported)}, and none is installed" + (
                f" (found {found})" if found else ""
            )
        if self.rejected:
            return f"Jenkins {self.jenkins_version} refuses every installed Java (found {found})"
        return f"Jenkins {self.jenkins_version} needs a Java, and none is installed"

    def remedy(self) -> str:
        """The command (macOS) or step (elsewhere) that installs a Java this Jenkins accepts."""
        if platform.system() == "Darwin":
            formula = self.formula
            return (
                f"brew install {formula}"
                if formula
                else "aisland workspace provision install   # installs the Java this Jenkins supports"
            )
        if self.supported:
            return f"install a Java {max(self.supported)} JDK and set JAVA_HOME to its home directory"
        return f"install a JDK this Jenkins supports ({_JAVA_SUPPORT_URL}) and set JAVA_HOME to its home directory"

    def refusal(self) -> str:
        """The message `aisland jenkins start` stops with: the problem, each refused JDK, the fix."""
        lines = [f"Error: {self.problem()}."]
        lines += [f"  Java {runtime.version}: {runtime.java}" for runtime in self.rejected]
        lines.append(f"Fix: {self.remedy()}")
        return "\n".join(lines)


def _spoken(majors: Sequence[int]) -> str:
    """`21 or 25`, `17, 21 or 25`."""
    words = [str(major) for major in majors]
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"


def runtime_at(java: Path) -> Optional[JavaRuntime]:
    """The JDK whose `java` this is, read from the `release` file in its home — or None.

    None for a path that is not an executable, and for a `java` with no JDK home beside it —
    macOS' `/usr/bin/java` stub, which would open an install dialog if run.
    """
    if not (java.is_file() and os.access(java, os.X_OK)):
        return None
    release = java.resolve().parent.parent / "release"
    if not release.is_file():
        return None
    match = _JAVA_VERSION_RE.search(release.read_text(errors="replace"))
    return JavaRuntime(java, match.group(1)) if match else None


def _homebrew_prefix(env: Mapping[str, str]) -> Optional[Path]:
    """Homebrew's prefix: `HOMEBREW_PREFIX` (set by `brew shellenv`), else two levels above `brew` on PATH."""
    if env.get("HOMEBREW_PREFIX"):
        return Path(env["HOMEBREW_PREFIX"])
    brew = shutil.which("brew", path=env.get("PATH", ""))
    return Path(brew).resolve().parent.parent if brew else None


def candidate_javas(env: Mapping[str, str]) -> List[Path]:
    """Every `java` worth asking, in the order a user would name them.

    `$JAVA_HOME/bin/java`, the `java` on PATH, each Homebrew `openjdk*` keg, then the system JDK
    folders (:data:`SYSTEM_JDK_GLOBS`). Duplicates are removed later, by resolved path.
    """
    candidates: List[Path] = []
    if env.get("JAVA_HOME"):
        candidates.append(Path(env["JAVA_HOME"]) / "bin" / "java")
    on_path = shutil.which("java", path=env.get("PATH", ""))
    if on_path:
        candidates.append(Path(on_path))
    prefix = _homebrew_prefix(env)
    if prefix is not None:
        candidates += [Path(java) for java in sorted(glob.glob(str(prefix / "opt" / "openjdk*" / "bin" / "java")))]
    for pattern in SYSTEM_JDK_GLOBS:
        candidates += [Path(java) for java in sorted(glob.glob(pattern))]
    return candidates


def find_runtimes(env: Optional[Mapping[str, str]] = None) -> List[JavaRuntime]:
    """The installed JDKs, each listed once under the first path it was found by.

    Args:
        env: The environment to read `JAVA_HOME`, `PATH` and `HOMEBREW_PREFIX` from; the
            process's own when omitted.
    """
    seen: Set[Path] = set()
    runtimes: List[JavaRuntime] = []
    for java in candidate_javas(os.environ if env is None else env):
        runtime = runtime_at(java)
        if runtime is None:
            continue
        resolved = java.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        runtimes.append(runtime)
    return runtimes


def jenkins_version(war: Path) -> str:
    """`Jenkins-Version` from the WAR's manifest; `unknown` for a WAR without it, or a broken download."""
    if not zipfile.is_zipfile(war):
        return "unknown"
    with zipfile.ZipFile(war) as archive:
        if "META-INF/MANIFEST.MF" not in archive.namelist():
            return "unknown"
        match = _JENKINS_VERSION_RE.search(archive.read("META-INF/MANIFEST.MF").decode(errors="replace"))
    return match.group(1) if match else "unknown"


def probe(war: Path, java: Path) -> Tuple[bool, Tuple[int, ...]]:
    """Ask the WAR whether it runs on ``java``: `(accepted, the Java releases it names)`.

    `java -jar jenkins.war --version` prints the Jenkins version and exits 0 on a Java the
    launcher accepts; on any other it prints "Supported Java versions are: [21, 25]" and exits
    1 — or, on a Java too old to load the launcher at all (8), dies naming nothing.
    """
    try:
        completed = subprocess.run(
            [str(java), "-jar", str(war), "--version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, ()
    match = _SUPPORTED_RE.search(f"{completed.stdout}\n{completed.stderr}")
    supported = tuple(int(number) for number in re.findall(r"\d+", match.group(1))) if match else ()
    return completed.returncode == 0 and match is None, supported


def choose_java(war: Path, runtimes: Sequence[JavaRuntime]) -> JenkinsJava:
    """The newest of ``runtimes`` the WAR accepts — newest probed first, so older ones are never run."""
    rejected: List[JavaRuntime] = []
    supported: Tuple[int, ...] = ()
    for runtime in sorted(runtimes, key=JavaRuntime.sort_key, reverse=True):
        accepted, named = probe(war, runtime.java)
        if accepted:
            return JenkinsJava(jenkins_version(war), runtime, supported, tuple(rejected))
        rejected.append(runtime)
        supported = supported or named
    return JenkinsJava(jenkins_version(war), None, supported, tuple(rejected))


def newest_versioned_formula() -> Optional[str]:
    """The highest-numbered `openjdk@N` Homebrew offers — what to install when no Java can ask the WAR.

    None without Homebrew, or when the search fails (offline with no cached index) or gives no answer in time.
    """
    if shutil.which("brew") is None:
        return None
    try:
        completed = subprocess.run(
            ["brew", "search", "--formula", f"/^{VERSIONED_FORMULA}[0-9]+$/"],
            capture_output=True,
            text=True,
            env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"},
            timeout=BREW_SEARCH_SECONDS,
        )
    except subprocess.TimeoutExpired:  # a network that stops answering: no answer, as offline
        return None
    if completed.returncode != 0:
        return None
    numbers = [int(match.group(1)) for line in completed.stdout.split() if (match := _FORMULA_RE.match(line))]
    return f"{VERSIONED_FORMULA}{max(numbers)}" if numbers else None


__all__ = [
    "JavaRuntime",
    "JenkinsJava",
    "SYSTEM_JDK_GLOBS",
    "candidate_javas",
    "choose_java",
    "find_runtimes",
    "jenkins_version",
    "newest_versioned_formula",
    "probe",
    "runtime_at",
]
