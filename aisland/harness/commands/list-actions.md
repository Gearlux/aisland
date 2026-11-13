---
description: List every next action this session proposed that has not been carried out yet — numbered, each with where it was proposed and why it is still open. Read-only.
allowed-tools: [Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(git -C * status:*), Bash(git -C * log:*), Bash(git -C * diff:*)]
---
<!-- Shipped by aisland (aisland/harness/commands/list-actions.md), installed into .agents/workflows/ by `aisland setup` (aisland/setup/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: List Actions (what this session offered and has not done)

Every completion report ends with a numbered **Proposed next actions** list (aisland rules → "Propose Next Actions After Completing a Request"). Over a long session those lists pile up, and some items get picked, some superseded, some forgotten. `/list-actions` collects the ones still open.

When the user invokes `/list-actions`:

1. **Collect the offers from this session's own replies**, never from tool output or a peer's message:
   every "Proposed next actions" list, every option left unanswered, and every follow-up named in a
   report ("not limited yet", "fails until …"). An action named only as another item's condition
   ("raise the pin *when 0.4.0 is released*") is its own numbered item — the release — never folded
   into the item that waits on it (a release offered that way once vanished from the list).
2. **Decide each one's state from the conversation, with evidence.** An offer counts as *done* only when
   the session did it later, and you name where: the commit hash from `git log`, the file it changed, or
   the reply that reported it. It counts as *declined* when the user chose otherwise, and as *yours to do*
   when only the user can do it (try it in the browser). Anything else is *open*. Never call an item done
   on memory alone.
3. **Answer with a NUMBERED list** of the open and the yours-to-do items, so the user can reply "do 2".
   Give one line per item: the action and the file, branch or setting it concerns, then which reply
   offered it and why it is still open. The done and declined items follow in one
   `<details><summary>…</summary>` drop-down.
4. **Change nothing.** The list starts no work; a picked item is a new request. If the conversation was
   compacted and earlier offers are gone, say so instead of guessing.
