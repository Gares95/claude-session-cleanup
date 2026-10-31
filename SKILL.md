---
name: claude-session-cleanup
description: Preview which local Claude Code sessions a retention policy would select, using age, latest-count, and exact current-project rules, report how Claude Code's own cleanupPeriodDays cleanup is configured, and, on Linux and only after the user explicitly confirms a reviewed plan, permanently delete the selected sessions. Use when the user asks which Claude Code sessions are old, wants to see what keeping only recent sessions would affect, asks how much space sessions use, asks when Claude Code deletes transcripts, or explicitly asks to delete old Claude Code sessions. It cannot archive, quarantine, or restore sessions. Do not use for memory, settings, prompt history, plugins, worktrees, background-agent management, or general Claude Code cleanup.
---

# Claude Session Cleanup

Run `scripts/cleanup_sessions.py` (next to this file). Every run is a
read-only preview unless `--apply-plan` is passed. Permanent deletion always
takes three steps: preview, save a plan, apply that plan after the user has
reviewed it and explicitly said yes.

## Interpret the request

Choose only these arguments:

- "older than 48 hours / 7 days" → `--older-than 48h` / `--older-than 7d`
  (positive whole hours or days).
- "keep my latest 5" → `--keep-latest 5`.
- Both constraints → pass both; they combine by intersection (a root group
  must be older than the cutoff *and* outside the latest N). Latest N counts
  root groups: a session with its subagents and linked continuations counts
  once.
- Use the default `--project current` unless the user explicitly asks about
  all projects; then add `--project all`.
- At least one selector is required. For a vague request ("clean up my
  sessions"), ask for an age or a count.
- Add `--verbose` when the user wants reasons for retained, protected, or
  excluded sessions.
- Add `--action delete` only when the user asks to delete sessions. "Clean
  up" alone is not a request to delete: preview and ask.

Do not invent flags. There is no `--apply`, archive, quarantine, or restore.
Never offer `claude purge` as a substitute for deleting selected sessions: it
removes a whole project's data, including its auto memory.

## Run the preview

```bash
python3 <skill-directory>/scripts/cleanup_sessions.py --older-than 30d --keep-latest 10
```

Run it from the user's current project directory, because the default scope
is the exact current directory. Prefer the text output for conversation; use
`--json` only when you need to compute something from the result. JSON omits
titles unless `--include-titles` is passed.

## Explain the result

Summarize:

- how many root groups would be selected, retained, protected, and
  excluded, with sizes;
- why groups are retained (within the latest N, newer than the cutoff) and
  protected (this session, running, possibly running, background session,
  recently active);
- any excluded groups and why (the tool refuses to judge unknown or unsafe
  metadata);
- the "Claude Code's own cleanup" section: configured period, and any
  sessions the policy keeps that Claude Code may delete anyway. Present this
  as an estimate; never state a date when Claude Code will delete something.

End a preview by saying no changes were made.

## Delete (only when the user asked for it)

1. Preview with `--action delete --verbose` and the user's policy. Show what
   would be deleted, what cannot be deleted and why (`cannot be deleted:`
   lines), and what is left in place. If the footer says deletion is
   unavailable here, stop and explain why.
2. Save a plan to a private ignored location, for example:

   ```bash
   python3 <skill-directory>/scripts/cleanup_sessions.py --older-than 30d --action delete --save-plan .local/delete-plan.json
   ```

3. Tell the user exactly how many groups, sessions, and files the plan
   deletes, that deletion is permanent with no backup, and ask for explicit
   confirmation. Do not apply without a clear yes to this specific plan.
4. Apply from the same directory:

   ```bash
   python3 <skill-directory>/scripts/cleanup_sessions.py --apply-plan .local/delete-plan.json
   ```

5. Report the result as printed. Exit 3 (stale plan) means nothing was
   deleted because sessions changed; preview again and save a new plan.
   Exit 2 means the plan is unusable (already used, expired, edited, or
   from another directory). Exit 1 with `STOPPED` means deletion stopped
   part-way; report what remains and preview again before anything else.

Never delete, move, or edit files under `~/.claude` yourself, and never edit
a saved plan.

If the user wants Claude Code to keep transcripts longer, explain that
`"cleanupPeriodDays": 3650` in `~/.claude/settings.json` does that, that
project or managed settings can override it, and let the user decide. Do not
edit settings on their behalf as part of this skill.

## Safety stops

If the helper prints `Safety stop:` it exits with status 1 and made no
changes. Report the message as is. Do not work around it by inspecting
session files manually.
