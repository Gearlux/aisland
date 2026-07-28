# aisland — backlog

Open work for this project. Cross-cutting / multi-project initiatives live in the
workspace root `TASKS.md`. Completed items are not archived here — git history is the record.

- [ ] **`aisland source` for a private/corporate project:** the `pypi` kind assumes a public index. A project published to a private index would need the source entry to carry (or resolve) that index, which today only reaches uv via the generated root `uv.toml`. @low @infrastructure
- [ ] **Transitive Gearlux deps of a `pypi`-sourced project** are left to the published wheel's own metadata. That is correct for a leaf like loggair, but a published project that itself depends on an unpublished Gearlux package would fail to resolve — validate this at `aisland source set` time rather than at install time. @low @infrastructure
- [ ] **Regenerate the remaining projects' CI files from the updated scaffold template** at their next `aisland jenkins scaffold` run — behavior-neutral today (no other project has `examples/*/run.py`), so the sync was deliberately scoped to confluid + liquifai. @low @maintenance
- [ ] **Aisland Git Configuration:** Configure `git config pull.rebase false` during project setup in `aisland`. @medium @maintenance
