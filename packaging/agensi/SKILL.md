---
name: claude-session-cleanup
description: Preview which local Claude Code sessions a retention policy would select, using age, latest-count, and exact current-project rules, report how Claude Code's own cleanupPeriodDays cleanup is configured, and, on Linux and only after the user explicitly confirms a reviewed saved plan, permanently delete the selected sessions. Use when the user asks which Claude Code sessions are old, wants to see what keeping only recent sessions would affect, asks how much space sessions use, asks when Claude Code deletes transcripts, or explicitly asks to delete old Claude Code sessions. It cannot archive, quarantine, or restore sessions. Do not use for Codex, memory, settings, prompt history, plugins, worktrees, background-agent management, or general cleanup.
version: 1.1.0
summary: Preview which local Claude Code sessions an age or keep-latest policy would remove, then permanently delete them on Linux through a reviewed, single-use saved plan. Read-only by default; touches Claude Code session history only. Open source (MIT).
license: MIT
compatibility: Requires Python 3.10+ and a local agent environment with access to the user's Claude Code configuration directory (~/.claude or $CLAUDE_CONFIG_DIR). Previews run on Linux, macOS, and Windows; permanent deletion is Linux-only.
tags:
  - claude-code
  - session-cleanup
  - retention
  - privacy
  - disk-space
  - python
keywords:
  - claude code sessions
  - delete old claude conversations
  - claude code history cleanup
  - cleanupPeriodDays
  - transcript retention
  - keep latest sessions
use_cases:
  - Preview which Claude Code conversations are older than a given age
  - Keep only the most recent Claude Code conversations of a project
  - Check how much disk space Claude Code session history uses
  - Explain when Claude Code's own cleanupPeriodDays sweep may delete transcripts
  - Permanently delete old Claude Code conversations on Linux after reviewing a plan
docs_url: https://github.com/Gares95/claude-session-cleanup
---

# Claude Session Cleanup

This skill ships one self-contained Python script, `scripts/cleanup_sessions.py`.
It uses only the Python standard library, never uses the network, and never
starts another program (not even `claude`). Every run is a read-only preview
unless `--apply-plan` is passed. Permanent deletion always takes three
steps: preview, save a plan, and apply that plan after the user has reviewed
it and explicitly said yes.

The script is generated from the open-source project
<https://github.com/Gares95/claude-session-cleanup> (MIT License, version
1.1.0): it is the project's own implementation, bundled into one file. The
full source, tests, and documentation are public there; this package adds no
features and no private code.

## Running the script

Write `scripts/cleanup_sessions.py` unchanged to a temporary file and run it
with `python3 -I` from the user's project directory:

```bash
python3 -I /path/to/temporary/cleanup_sessions.py --older-than 30d
```

- Run it from the project directory the user means: the default scope is
  exactly the current directory.
- `-I` keeps other files in the temporary directory off Python's import
  path. Remove the temporary file afterwards.
- It needs Python 3.10 or newer (`python3 --version`). If only an older
  Python is available, stop and say so.
- It reads the user's Claude Code data in `~/.claude` (or
  `$CLAUDE_CONFIG_DIR`). It must run on the user's own machine. In a cloud
  sandbox or a container without that directory it prints
  `Safety stop: no Claude Code configuration directory at ...`; report that
  and stop.
- The script calls itself `claude-session-cleanup` in its help and messages.
  When a message says to run `claude-session-cleanup --apply-plan PATH`,
  run this script with `--apply-plan PATH` instead.

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

| Request | What to do |
|---|---|
| "Preview Claude sessions older than 7 days." | Preview `--older-than 7d` |
| "Keep my latest 5 Claude conversations." | Preview `--keep-latest 5`; ask whether the user wants the others deleted |
| "Delete Claude sessions older than 30 days." | Preview `--older-than 30d --action delete`, then follow "Delete" below |
| "Delete old Claude conversations for this project." | Ask for an age or a count first; then as above |
| "Clean up Claude." | Ask what to clean; this skill only handles session retention |

Do not invent flags. There is no `--apply`, archive, quarantine, or restore:
the script rejects `--action delete --apply`, `--action archive`, and
`--action quarantine`. Never offer `claude purge` as a substitute for
deleting selected sessions: it removes a whole project's data, including its
auto memory.

## Explain a preview

Prefer the text output; use `--json` only when you need to compute something
from the result (JSON omits titles unless `--include-titles` is passed).
Summarize:

- how many root groups would be selected, retained, protected, and
  excluded, with sizes;
- why groups are retained (within the latest N, newer than the cutoff) and
  protected (this session, running, possibly running, background session,
  recently active);
- any excluded groups and why (the script refuses to judge unknown or unsafe
  metadata);
- the "Claude Code's own cleanup" section: the configured period, and any
  sessions the policy keeps that Claude Code may delete anyway. Present this
  as an estimate; never state a date when Claude Code will delete something.

End a preview by saying no changes were made.

## Delete (only when the user asked for it)

Permanent deletion works only on Linux, where the script can check running
Claude Code processes through `/proc`. On macOS and Windows the preview
footer says deletion is unavailable and saving a plan stops with
`Safety stop: deletion is not supported here`; explain that and stop.

1. Preview with `--action delete --verbose` and the user's policy. Show what
   would be deleted, what cannot be deleted and why (`cannot be deleted:`
   lines), and what is left in place. If the footer says deletion is
   unavailable here, stop and explain why.
2. Save a plan to a new file in a private directory outside the project and
   outside `~/.claude`, for example:

   ```bash
   mkdir -p ~/.cache/claude-session-cleanup
   python3 -I /path/to/temporary/cleanup_sessions.py --older-than 30d --action delete --save-plan ~/.cache/claude-session-cleanup/delete-plan-1.json
   ```

   The script never overwrites a file: choose a new name if it exists.
3. Tell the user exactly how many groups, sessions, and files the plan
   deletes, that deletion is permanent with no backup, and ask for explicit
   confirmation. Do not apply without a clear yes to this specific plan.
4. Apply from the same project directory:

   ```bash
   python3 -I /path/to/temporary/cleanup_sessions.py --apply-plan ~/.cache/claude-session-cleanup/delete-plan-1.json
   ```

5. Report the result as printed. Exit 3 (stale plan) means nothing was
   deleted because sessions changed; preview again and save a new plan.
   Exit 2 means the plan is unusable (already used, expired after 24 hours,
   edited, or from another directory). Exit 4 means another run holds the
   plan. Exit 1 with `STOPPED` means deletion stopped part-way; report what
   remains and preview again before anything else.

Applying re-checks everything: running sessions, new activity, new
subagents or continuations, and every target's type, size, timestamps, and
inode. It deletes nothing unless the sessions are exactly as reviewed. It
removes only each session's own files, never project memory, prompt
history, settings, credentials, plugins, other projects, or Codex data. The
details are in `references/safety.md`.

Never delete, move, or edit files under `~/.claude` yourself, never edit a
saved plan, and never retype or modify the script.

If the user wants Claude Code to keep transcripts longer, explain that
`"cleanupPeriodDays": 3650` in `~/.claude/settings.json` does that, that
project or managed settings can override it, and let the user decide. Do not
edit settings on their behalf as part of this skill.

## Safety stops

If the script prints `Safety stop:` it exits with status 1 and made no
changes. Report the message as is. Do not work around it by inspecting
session files manually.

## Limitations

- Permanent deletion is Linux-only. Previews work on Linux, macOS, and
  Windows.
- Claude Code has no native archive, quarantine, or single-conversation
  delete; this skill offers none of those beyond deletion on Linux.
- It depends on Claude Code's internal, undocumented storage layout, checked
  against Claude Code 2.1.310, 2.1.314, and 2.1.316. Unrecognized data
  blocks deletion of that conversation.
- Running-session detection cannot see sessions on another machine, under
  another configuration directory, or in another PID namespace.
