# aisland — backlog

Open work for this project. Cross-cutting / multi-project initiatives live in the
workspace root `TASKS.md`. Completed items are not archived here — git history is the record.

- [ ] **`aisland source` for a project on a private INDEX:** the `pypi` kind assumes a public index. A project published to a private index would need the source entry to carry (or resolve) that index, which today only reaches uv via the generated root `uv.toml`. (A private *repository* is already covered — use `github`, whose URL comes from `.gitmodules`.) @low @infrastructure
- [ ] **Transitive Gearlux deps of a `pypi`-sourced project** are left to the published wheel's own metadata. That is correct for a leaf like loggair, but a published project that itself depends on an unpublished Gearlux package would fail to resolve — validate this at `aisland source set` time rather than at install time. @low @infrastructure
- [ ] **A project's own name appears in its `Jenkinsfile.local` internal-dep list.** `annotaide` and `signals` declare each other in their `[dev]` extras, so the transitive closure `discover_gearlux_projects` walks loops back to the project being scaffolded and emits a `--no-deps -e <self>` line before `.[dev]`. Harmless (a duplicate editable install) but confusing to read — exclude the scaffolded project from its own dep list. Pre-existing; unrelated to the guard fix. @low @maintenance
- [ ] **Aisland Git Configuration:** Configure `git config pull.rebase false` during project setup in `aisland`. @medium @maintenance
