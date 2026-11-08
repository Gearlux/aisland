# A project not on GitHub gets wheels that say nothing of GitHub (2026-11-03)

## Context

A project whose own remote is a company's server commits its `wheels/` there (`--commit`), and the company's
repository must not reveal the workspace on GitHub it is developed in. The folder did: `wheels.yaml` and the README
named every wheel's `dev/main <sha>`, and each wheel's METADATA carried its project's README (dozens of links to the
GitHub repositories), its `Project-URL`s on GitHub and an author naming the workspace or its GitHub organisation. A
hand edit of the committed files is undone by the next build.

## Decision

For a target not in `github.project_repositories` (the same test the CI scaffold uses), the run
[wheels-off-github]:

- writes `wheels.yaml` without `commit:` and the README without its Branch and Commit columns, its intro saying
  "its last commit" (`render_readme` / `render_wheels_yaml`, `commits=False`); `install.py` never read either;
- rewrites every wheel it built, the target's own included, before anything else sees it
  (`wheels.scrub.scrub_wheel`): METADATA without its description (body, `Description`, `Description-Content-Type`),
  without the URL fields (`Project-URL`, `Home-page`, `Download-URL`) naming `github.com`, and without the author and
  maintainer fields naming the workspace's name or a GitHub owner of its projects (`WheelsPlan.hidden_names`); RECORD's
  line for METADATA follows, every other member is copied with its own time and attributes.

What installs is kept: the code, every `Requires-Dist` (a third-party `git+https://github.com/…` requirement is a
requirement, and allowed), the entry points. The scrub is a function of the wheel, so an unchanged project rebuilds
byte for byte. A target on GitHub gets the files and wheels it always got, byte for byte.

Rejected: dropping every URL and author field (a docs site or a person's name reveals nothing), editing the code
inside a wheel (a wheel would differ from the commit it names), and leaving it to the projects' own metadata (their
GitHub URLs are right on PyPI).

## Consequences

- The README's table shows no commit, so which commit a wheel holds is the report's (terminal or `--json`) alone.
- Text in a wheel's CODE naming the workspace is not scrubbed; it is fixed in its project.

## Example

```python
from pathlib import Path

from aisland.wheels.scrub import scrub_wheel

scrub_wheel(Path("app/wheels/models-0.1.0-py3-none-any.whl"), ("acme", "acmeorg"))
# True: its METADATA lost the README, the github.com Project-URLs and "Author: Acme"
```

## What you may change

- The fields scrubbed, with `tests/test_wheels_scrub.py`.
- Never: a scrub that touches code, requirements or entry points, or one that runs for a target on GitHub.

---

[The architecture and every decision](../architecture.md#the-decisions)
