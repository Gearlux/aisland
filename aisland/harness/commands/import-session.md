---
description: Continue a session exported by /export-session on another machine — restore its whole chat here and list it in the desktop app's sidebar, so the conversation goes on in it. No checks.
argument-hint: "<handoff .tar.gz or folder>"
---
<!-- Shipped by aisland (aisland/harness/commands/import-session.md), installed into .agents/workflows/ by `aisland setup` (aisland/setup/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Import Session (pick up a session another machine exported)

`/export-session` wrote a handoff: the session's chat, a brief (`HANDOFF.md`), each repository's branch and commit
(`state.json`) and `attachments/`. This command puts the chat back on this machine and nothing else: it checks no
code, environment or attachment, reads no brief and starts no work. The conversation goes on in the restored chat.

When the user invokes `/import-session <archive or folder>`:

1. **Restore the chat.** No argument: use the newest `.handoff/*.tar.gz` in the workspace root, naming it. From the
   workspace root run

   ```bash
   python3 .agents/scripts/session_handoff.py restore <archive or folder>
   ```

   An archive is unpacked beside itself. The chat is written where Claude Code keeps this machine's chats, and
   `desktop` names the session record that lists it in the Claude desktop app under `title`. A run that refuses with
   "carries no chat" ends the command: say the export carried no chat and name its `HANDOFF.md`.

2. **Say where it is, in one line, and stop.**
   - `desktop` set: "**<title>** is in the sidebar." Add: if the sidebar does not show it, quit the desktop app (⌘Q)
     and open it again — the app reads its session list when it starts.
   - `desktop` null (no desktop app here): give the one command that opens it, `cd <start_folder> && claude --resume
     <session_id>` (`start_folder` null: the workspace root).
