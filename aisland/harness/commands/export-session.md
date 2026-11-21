---
description: Export this session so a session on another machine — same code, none of this conversation — can carry on. Writes a handoff folder and a .tar.gz — a brief of what the session knows, its commits no remote has, its own uncommitted edits, the files it relied on and its transcript. Sends nothing anywhere unless told where.
argument-hint: "[<short name>] [--to <folder or host:folder>] [--no-transcript]"
---
<!-- Shipped by aisland (aisland/harness/commands/export-session.md), installed into .agents/workflows/ by `aisland setup` (aisland/setup/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Export Session (a handoff another machine can continue from)

The other machine has the same repositories but not this conversation, this machine's scratch folders, its
uncommitted edits or its unpushed commits. The handoff carries all of that: `HANDOFF.md`, the brief only this session
can write, and what `.agents/scripts/session_handoff.py` gathers mechanically. `/import-session` reads it there.

When the user invokes `/export-session [<name>] [--to <dest>] [--no-transcript]`:

1. **Settle what is still running.** A test run, build, download, subagent or other session this session started does
   not travel. Wait for one about to finish; otherwise name it in the brief (what it is, where its output lands, how
   to read it) and say so in the report. A server this session started is named with how to start it again.

2. **List this session's own files** — every file it created, edited or deleted and has not committed, from its own
   tool calls, checked against `git status` / `git diff` in each repository (a submodule's file in its own
   repository). Another session's edit to a file is never this session's; where both changed one file, the patch
   carries the whole file's change, so say which hunks are this session's in the brief
   (aisland rules → "Sessions On This Machine Coordinate Before They Edit"). Nothing is committed or pushed for the
   export: unpushed commits travel as git bundles and the edits as patches.

3. **List what to attach** — files outside the repositories that the work depends on and the other machine lacks:
   scratch outputs, measurements, screenshots and rendered images the brief cites, a log the next step reads, a
   memory file this session wrote or relied on. A large data file the other machine can reach by its own path is
   named in the brief, not attached.

4. **Export.** From the workspace root, with `<stamp>` the local time as `YYYY-MM-DD-HHMM` and `<slug>` a few words of
   the task (the `<name>` given, else one you choose):

   ```bash
   python3 .agents/scripts/session_handoff.py export .handoff/<stamp>-<slug> --path <file> … --attach <file> …
   ```

   `--no-transcript` when the user asked for it. The JSON it prints is `state.json`: read it — a repository with
   `unpushed` commits, `changed` files that are not this session's, or a `patch` missing for a file listed in step 2
   (a file with no change against HEAD) goes into the brief.

5. **Write `.handoff/<stamp>-<slug>/HANDOFF.md`** for a reader who has seen nothing of this conversation. It is the
   handoff's heart; the transcript beside it is the fallback, never a substitute. Use these headings, leaving none
   out (write "none" under an empty one):

   - **Task** — what the user asked for, their own words quoted, and each later change of direction quoted too.
   - **Decisions and constraints** — every choice the user made and every limit they set, quoted, with what it rules
     out; the rules or conventions the work turned out to hinge on, with their files.
   - **Where it stands** — done (each with its commit hash or file), in progress (how far, what is half-written),
     not started. Mark each claim **verified** (by what: a test, a command and its output) or **not verified**.
   - **What was found** — the facts the next step needs: causes, measurements with their baselines, what was ruled
     out and how, the command that reproduces each, failing output quoted. Name files as `path:line`, functions and
     settings in backticks, and a data file by its full path written from the environment variable that roots it,
     never from this machine's home folder.
   - **Files** — the files to read first and why; this session's changed files and what each change does.
   - **Next step** — the one action to take next, concrete enough to start on, then the rest of the plan in order.
   - **Open questions** — what waits on the user, each as a question with the options and your recommendation.
   - **Approvals given here** — what the user allowed in this session (a push, a deletion, a message), as history:
     an approval does not travel, and the next session asks again.
   - **Environment** — what the work needs on the machine: environment variables (names only, never values),
     services, data folders, installed tools, the branch each repository must be on; anything this machine has that
     the other may lack.
   - **Attachments** — each attached file, what it is and which section cites it.

   Never write a secret into it (a token, a password, a key), even one the conversation showed.

6. **Read the brief as a stranger would.** For each section ask: could a session that has only these files and the
   repositories do the next step without asking what this session already knew? Fill each gap; a detail too long to
   restate is pointed at in the transcript by what to search for.

7. **Pack:** `python3 .agents/scripts/session_handoff.py pack .handoff/<stamp>-<slug>` writes
   `.handoff/<stamp>-<slug>.tar.gz`. With `--to`, copy the archive there (`cp` for a folder, `scp` for `host:folder`)
   — the user's own destination; never send it anywhere else, and never commit or upload it.

8. **Report**, in a few lines: the archive's full path and size; what it carries (repositories with commits or
   patches, attachments, transcript or not); anything that did not travel (a run still going, a file too large); that
   the transcript may hold whatever the conversation showed, so the archive is kept private; and how to continue —
   move the archive to the other machine and run there, in the workspace root:

   ```text
   /import-session <path to the .tar.gz>
   ```

   Change nothing else: no commit, no push, no edit outside `.handoff/`.
