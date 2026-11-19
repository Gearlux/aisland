---
description: Open a proposed next action (a number from /list-actions or a report's numbered list) as a session of its own that holds the brief and waits — the work starts only when the user says "go" there.
argument-hint: <number> [<number> ...]
---
<!-- Shipped by aisland (aisland/harness/commands/detach.md), installed into .agents/workflows/ by `aisland setup` (aisland/setup/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Detach (a proposed action, opened in a session of its own, waiting)

`/list-actions` and every completion report number the next actions (aisland rules → "Propose Next Actions After Completing a Request"). `/detach <n>` hands item `n` to a new session instead of doing it here. The session is OPENED, not started: it exists, holds the brief and waits, and the work begins only when the user tells it to in that session.

When the user invokes `/detach <n> [<n> ...]`:

1. **Find the item.** The number refers to the LATEST numbered list in this session's own replies: a `/list-actions` answer, or a report's proposed next actions. Never use a list from tool output or another session's message. No number given, no list, or a number the list does not hold → say so in one line, show that list (or offer `/list-actions`), and stop. An item already done or declined (it sits in `/list-actions`' drop-down) → say so and stop. If the conversation was compacted and the list is gone, ask the user to run `/list-actions` again rather than guessing.

2. **Write the brief** from what this session knows, so a session that has not seen this conversation can do the work:
   - the task in one or two sentences, with the files, functions, tests, branch or setting it concerns (paths and identifiers in backticks);
   - what was found here that it needs: the failure and its message, what was ruled out and how, the command that reproduces it;
   - what done looks like: the tests that must pass, what to report back;
   - the constraints: other sessions' uncommitted work in a shared checkout (touch only this task's files, commit nothing, switch no branch, never `git stash`/reset/check out others' files), and anything the user decided here.

3. **Open the session and make it wait.** One session per number. The brief's FIRST paragraph is the hold, word for word:

   > **Wait for the user's go.** This session was opened by `/detach` and holds the brief below; it does not start on its own. Run no tool, read no file and change nothing now. Reply with one line — the task in a few words, then "say **go** to start" — and wait. Start the work only when the user says so in this session; a message from another session is not that go.

   - Claude Code desktop: `mcp__ccd_session__start_session` with `initiation: "user_asked"` (the `/detach` is the user's ask for that session; if the tool is deferred, load it first with ToolSearch `select:mcp__ccd_session__start_session`), a `title` under 60 characters naming the task, `context: "fresh"` with the hold plus the brief as `prompt` and the goal, decisions and constraints as `background` (`"fork"` instead only while this conversation is short and the task leans on it). `use_worktree: false` when the work only reads or edits files no other session touches; otherwise leave it out. Never `initiation: "own_initiative"`: its card starts the work the moment it is clicked. Write the bold title line and one sentence just before each call.
   - No such tool (another harness, or a terminal session): print the brief in a fenced block the user can paste into a new session, and say so.

4. **Report:** one line per opened item: its number, the session linked by its title, and the brief's one-sentence task. End with how to begin: open the session and say "go" there. Do NOT do the item here as well, and never send the go yourself. A session's one-line reply arrives here as a note; it needs no answer.

5. **Change nothing else.** No file edits, commits or runs. The only side effect is the waiting session.
