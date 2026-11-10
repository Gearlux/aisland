# The Jenkins jobs' `config.xml` comes from templates that escape every value (2026-11-05)

*2026-11-05* (the record [jenkins-job-templates]; follows "The CI files come from a template set")

## Context

`services/jenkins_jobs.py` wrote each job's `config.xml` — the multibranch job, the local job, the orchestrator, the
nightly check — as a Python f-string, escaping each value by hand with `xml.sax.saxutils.escape`. A value that was
forgotten reached the XML as markup: the nightly check's description held the workspace's name unescaped, so a name
with `&` or `<` wrote a file Jenkins cannot read.

## Decision

The four files are templates in `aisland/templates/jenkins/` (package data), rendered by `ci.jenkins_job_config.render_job` in
a Jinja environment with `autoescape=True`, `StrictUndefined` and `keep_trailing_newline`. Jinja's own braces are
kept: the templates are XML, and the pipeline scripts with their `${…}` and `{{` are VALUES, never template text.
Every value is escaped by the environment's `finalize` (`_xml_text`), XML's escape of `&`, `<` and `>`, and marked
`Markup` so autoescape does not escape it again; autoescape's own escape would also have turned every quote of a
script into `&#39;` or `&#34;`, changing every job's bytes. Values go into element text only, never an attribute.

## Consequences

For 87 configs rendered before and after (urls with `&`, `<` and quotes, scripts with `<`, `&`, `${x}`, `{{`, `{%`,
`]]>`) the files are byte for byte the same but one: the nightly check of a workspace whose name holds `&` or `<`,
whose description is now escaped (it was invalid XML). `markupsafe` becomes a declared dependency (jinja2 already
brought it). `tests/test_jenkins_job_templates.py` renders every template with hostile values and reads each back
through ElementTree.

## What you may change

The templates' XML, and a job's values. Never escape a value before handing it to `render_job` (it would be escaped
twice), never mark one `|safe`, and never put a value in an attribute without escaping its quotes.

---

[The architecture and every decision](../architecture.md#the-decisions)
