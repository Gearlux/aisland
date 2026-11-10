# Security

## Reporting a vulnerability

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**.
That is the one channel for a report; please don't open a public issue for it. A report is answered within a week, and a fix is released as soon as it is
ready, credited to you unless you would rather not be named.

## Supported versions

Only the latest release gets security fixes. aisland follows [Semantic Versioning](https://semver.org/) and is
before 1.0: a patch release (0.1.0 → 0.1.1) changes nothing [docs/stability.md](docs/stability.md) lists as kept, but
a minor release (0.1 → 0.2) may, and its CHANGELOG entry then says what changed and what to do about it. A fix is
released as a patch of the latest minor release.

## What aisland guards

These are the promises a report can hold aisland to:

- **Downloads are checked.** Every program aisland downloads (the Jenkins WAR and its plugin manager, the VS Code CLI,
  and on Linux process-compose and Node.js) is installed only when its SHA-256 matches a pinned value or the one its
  publisher lists. The one exception is Jenkins' plugins: the plugin manager fetches their current versions from the
  Jenkins update center, and aisland pins neither their versions nor their digests.
- **Nothing runs with `sudo` unasked, and aisland's own tools wait for a yes.** `aisland setup` installs the system
  tools your projects declare (`aisland workspace provision` lists them). aisland's own tools are installed the first
  time a command needs one, only after you agree; without a terminal, and under `setup -y`, nothing is asked and
  nothing of aisland's is installed: the command prints the line that does it.
- **Services stay on this machine.** Jenkins listens on `127.0.0.1` only and keeps its login and CSRF protection.
  aisland names the file that holds the initial admin password, and never prints the password itself.
- **Secrets stay private.** A new `.env` is readable by its owner only. Jenkins' `config.xml`, which holds values
  from `.env`, is written whole and readable by its owner only.
- **Archives can't escape their folder.** A wheels zip whose member names a path outside its `wheels/` folder is
  refused, and a downloaded tarball is unpacked with links and devices that point outside its folder blocked.
- **git never prompts unseen.** A git command that talks to a remote runs with its credential prompts off, so a
  missing credential fails at once instead of waiting for input that nobody sees.
