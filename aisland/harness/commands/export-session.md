---
description: Export this session so the same chat goes on on another machine — same code, pulled from the remotes. Writes a handoff folder and a .tar.gz — the whole chat, a brief of what the session knows, each repository's branch and commit, and the files it relied on. Carries no code and sends nothing anywhere unless told where.
argument-hint: "[<short name>] [--to <folder or host:folder>] [--no-transcript]"
---
<!-- Shipped by aisland (aisland/harness/commands/export-session.md), installed into .agents/workflows/ by `aisland setup` (aisland/setup/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Export Session (a handoff another machine can continue from)

The other machine has the same repositories and gets the code from their remotes: every commit is pushed, so no code
travels. What it lacks is this conversation, this machine's scratch folders and memory files. The handoff carries
those: the chat itself, which `/import-session` restores there so the conversation goes on, `HANDOFF.md`, the brief
only this session can write, and what `.agents/scripts/session_handoff.py` gathers mechanically.

When the user invokes `/export-session [<name>] [--to <dest>] [--no-transcript]`:

1. **Settle what is still running.** A test run, build, download, subagent or other session this session started does
   not travel. Wait for one about to finish; otherwise name it in the brief (what it is, where its output lands, how
   to read it) and say so in the report. A server this session started is named with how to start it again.

2. **List what to attach** — files outside the repositories that the work depends on and the other machine lacks:
   scratch outputs, measurements, screenshots and rendered images the brief cites, a log the next step reads, a
   memory file this session wrote or relied on. A large data file the other machine can reach by its own path is
   named in the brief, not attached.

3. **Export.** From the workspace root, with `<stamp>` the local time as `YYYY-MM-DD-HHMM` and `<slug>` a few words of
   the task (the `<name>` given, else one you choose):

   ```bash
   python3 .agents/scripts/session_handoff.py export .handoff/<stamp>-<slug> --attach <file> …
   ```

   `--no-transcript` when the user asked for it: the chat then stays here, and the other machine starts a new session
   from the brief. The JSON it prints is `state.json`, each repository's branch and commit. Read it: a repository
   with `unpushed` commits or `changed` files holds work that does NOT travel — name each in the report with
   `/push-all` (or a commit first) as the way to send it; the export goes on.

4. **Write `.handoff/<stamp>-<slug>/HANDOFF.md`** for a reader who has seen nothing of this conversation. The restored
   chat is how the work goes on; the brief is what `/import-session` checks the machine against, and the fallback
   when the chat cannot be resumed there (another Claude Code version, `--no-transcript`). Use these headings, leaving
   none out (write "none" under an empty one):

   - **Task** — what the user asked for, their own words quoted, and each later change of direction quoted too.
   - **Decisions and constraints** — every choice the user made and every limit they set, quoted, with what it rules
     out; the rules or conventions the work turned out to hinge on, with their files.
   - **Where it stands** — done (each with its commit hash or file), in progress (how far), not started. Mark each
     claim **verified** (by what: a test, a command and its output) or **not verified**.
   - **What was found** — the facts the next step needs: causes, measurements with their baselines, what was ruled
     out and how, the command that reproduces each, failing output quoted. Name files as `path:line`, functions and
     settings in backticks, and a data file by its full path written from the environment variable that roots it,
     never from this machine's home folder.
   - **Files** — the files to read first and why; the commits this session made and what each does.
   - **Next step** — the one action to take next, concrete enough to start on, then the rest of the plan in order.
   - **Open questions** — what waits on the user, each as a question with the options and your recommendation.
   - **Approvals given here** — what the user allowed in this session (a push, a deletion, a message), as history:
     an approval does not travel, and the next session asks again.
   - **Environment** — what the work needs on the machine: environment variables (names only, never values),
     services, data folders, installed tools, the branch each repository must be on; anything this machine has that
     the other may lack.
   - **Attachments** — each attached file, what it is and which section cites it.

   Never write a secret into it (a token, a password, a key), even one the conversation showed.

5. **Read the brief as a stranger would.** For each section ask: could a session that has only these files and the
   repositories do the next step without asking what this session already knew? Fill each gap.

6. **Pack** — the last step, so the chat it copies holds the whole export but the pack and the report:
   `python3 .agents/scripts/session_handoff.py pack .handoff/<stamp>-<slug>` copies the chat into the folder (its
   transcript, its subagents' chats and its tool results) and writes `.handoff/<stamp>-<slug>.tar.gz`; `chat: false`
   in what it prints means the transcript was not found, so the chat does not travel — say so. With `--to`, copy the
   archive there (`cp` for a folder, `scp` for `host:folder`) — the user's own destination; never send it anywhere
   else, and never commit or upload it.

7. **Report**, in a few lines: the archive's full path and size; what it carries (the chat or not, attachments); what
   did not travel (unpushed commits and uncommitted files by repository, a run still going, a file too large); that
   the chat holds whatever the conversation showed, so the archive is kept private; and how to continue — push what
   is unpushed, move the archive to the other machine and run there, in the workspace root:

   ```text
   /import-session <path to the .tar.gz>
   ```

   The chat goes on there; this session stops here, since a reply in it would not reach the other machine.

   Change nothing else: no commit, no push, no edit outside `.handoff/`.
