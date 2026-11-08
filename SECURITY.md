# Security

## Reporting a vulnerability

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**.
Please don't open a public issue for it. A report is answered within a week, and a fix is released as soon as it is
ready, credited to you unless you would rather not be named.

## Supported versions

Only the latest release gets security fixes. aisland follows [Semantic Versioning](https://semver.org/), so moving
to the latest release within a major version breaks nothing listed in [docs/stability.md](docs/stability.md).

## What aisland guards

These are the promises a report can hold aisland to:

- **Downloads are checked.** Every program aisland downloads (the Jenkins WAR and its plugin manager, the VS Code CLI,
  process-compose on Linux) is installed only when its SHA-256 matches a pinned value or the one its publisher lists.
- **Nothing is installed with `sudo` unasked.** A system tool is installed only after you agree to it.
- **Services stay on this machine.** Jenkins listens on `127.0.0.1` only and keeps its login and CSRF protection.
  aisland names the file that holds the initial admin password, and never prints the password itself.
- **Secrets stay private.** A new `.env` is readable by its owner only. Jenkins' `config.xml`, which holds values
  from `.env`, is written whole and readable by its owner only.
- **Archives can't escape their folder.** A wheels zip whose member names a path outside its `wheels/` folder is
  refused, and a downloaded tarball is unpacked with links and devices that point outside its folder blocked.
- **git never prompts unseen.** A git command that talks to a remote runs with its credential prompts off, so a
  missing credential fails at once instead of waiting for input that nobody sees.
