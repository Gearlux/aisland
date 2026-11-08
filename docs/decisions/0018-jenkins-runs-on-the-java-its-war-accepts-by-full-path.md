# Jenkins runs on the Java its WAR accepts, by full path

*2026-10-07*

## Context

Jenkins runs only on the long-term Java releases its launcher names, and refuses any other
before it starts:

```text
$ java -jar aisland/bin/jenkins.war --version        # java 26 first on PATH
Running with Java 26 from /opt/homebrew/Cellar/openjdk/26.0.2.1/…, which is not yet fully supported.
Run the command again with the --enable-future-java flag to enable preview support for future Java versions.
Supported Java versions are: [21, 25]
```

aisland declared Homebrew's `openjdk` as a system dependency and started Jenkins with the bare
`java`. That formula follows the newest Java, and `aisland setup`'s `brew install openjdk`
upgrades it. Homebrew's own caveat tells users to put that keg first on `PATH`. On 2026-10-06
setup moved it to 26.0.2.1. The supervisor ran Jenkins three times, each exited 1, and the only
trace was `Exited exit_code=1 process=jenkins` in the supervisor log. A Java 25 JDK was
installed on the same machine the whole time, and Jenkins never looked at it. The breakage
recurs every six months, whenever Java moves past the releases the downloaded WAR knows.

## Decision

`services/jenkins_java.py` asks the WAR. Every installed JDK is found: `$JAVA_HOME`, `java` on
`PATH`, each Homebrew `openjdk*` keg, `/Library/Java/JavaVirtualMachines/*`, `/usr/lib/jvm/*`. Its
version comes from its `release` file, so finding JDKs starts no JVM, and a JDK reached by several
paths is listed once. The JDKs are probed newest first with `java -jar jenkins.war --version`. The
first one the launcher accepts (exit 0, no "Supported Java versions" line) is the one Jenkins and
its plugin manager run on, by the path it was found under (`/opt/homebrew/opt/openjdk@25/bin/java`;
the `opt` link survives a `brew upgrade`).

- **No Java version is written into aisland.** The releases come from the launcher's refusal
  line, and the Homebrew formula to install is `openjdk@<the newest one named>`.
- **`aisland jenkins start` refuses up front** when no JDK is accepted: it prints the refused JDKs
  and the install command, then exits 1 before the supervisor is touched.
- **macOS provisioning installs the named formula** (`JenkinsService.provision_java`, called by
  `aisland workspace provision install` and, after asking, by `aisland jenkins start`; `aisland setup`
  ran it until 2026-10-28, record "aisland installs its own tools when a feature first needs one,
  after asking"). The WAR names its releases only when some JDK can run its launcher, so a
  machine with no such JDK (none at all, or only Java 8) first gets the newest `openjdk@N` to ask
  with. Provisioning goes on when the WAR cannot be downloaded or no JDK is accepted; it never exits.
- **The bare `openjdk` formula is never one of aisland's tools** (a test reads `provision.AISLAND_TOOLS`;
  aisland's `pyproject.toml` held them until 2026-10-23).
- **The WAR is a pinned release (2026-11-03).** `JenkinsService.WAR_VERSION` and `WAR_SHA256` name one long-term-support
  release; the download goes to a `.part` file and moves into place only when its sha256 is the pinned one, so a
  tampered or truncated WAR is never installed. A WAR already in `bin/` is used as it is. Moving to a newer release
  changes the two constants together (jenkins.io publishes the digest as `jenkins.war.sha256`), and the Java it
  needs is still asked of the WAR, never written into aisland (`tests/test_jenkins_war.py`).

Rejected:

- `openjdk@25` written into the sys-dependencies. It is correct today, and it breaks silently
  once the pinned WAR is moved to a release that drops 25.
- `--enable-future-java`. Jenkins calls it preview support, and plugins are not tested on
  non-LTS Java.
- Reading the supported releases from `executable/Main.class` without a JVM. That is parsing
  bytecode constants: fragile, for a 0.1 s saving.

## Consequences

- Jenkins keeps running whatever `java` the user's shell prefers. The user's `PATH` and the bare
  `openjdk` keg are left alone.
- Each start probes one JDK or more, about 0.1 s each, newest first, stopping at the first
  accepted.
- Provisioning downloads the WAR (about 95 MB, once) to ask it; `aisland jenkins start` asks before
  downloading it.
- On a machine with no JDK that can ask, provisioning installs one JDK Jenkins may then refuse (the
  newest `openjdk@N`, 27 on 2026-10-07) beside the one it accepts.
- Linux installs no Java; start and the doctor name the release to install.

## Example

```python
from aisland.services.jenkins import JenkinsService

choice = JenkinsService().resolve_java()          # probes the installed JDKs, newest first
choice.describe()   # 'Jenkins 2.555.2 runs on Java 25.0.3 (/opt/homebrew/opt/openjdk@25/bin/java)'
choice.supported    # (21, 25) — named by the launcher when it refused 26.0.2.1
```

```text
# the same machine with Java 25 removed: `aisland jenkins start` exits 1 before starting anything
Error: Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26.0.2.1, 11.0.29).
  Java 26.0.2.1: /opt/homebrew/opt/openjdk/bin/java
  Java 11.0.29: /Library/Java/JavaVirtualMachines/amazon-corretto-11.jdk/Contents/Home/bin/java
Fix: brew install openjdk@25
```

## What you may change

- Where JDKs are looked for (`candidate_javas`, `SYSTEM_JDK_GLOBS`), the message wording, and the
  probe timeout, with `tests/test_jenkins_java.py`.
- What must not change: Jenkins starts by full path on a JDK the WAR accepted; the WAR is the only
  source of the supported releases; the bare `openjdk` formula is not declared; provisioning never
  exits.

## The WAR is named by its version (2026-11-07)

A WAR was kept as `bin/jenkins.war` and an existing file was used as it was, so a new `WAR_VERSION` never reached a
machine that had downloaded an older one. The file is now `bin/jenkins-<WAR_VERSION>.war`: a new pin downloads its
own, and once it is in place the WARs of other versions (and an older aisland's `jenkins.war`) are removed, as the
plugin manager's jars already were. A WAR of the pinned version already there is still used as it is.

---

[The architecture and every decision](../architecture.md#the-decisions)
