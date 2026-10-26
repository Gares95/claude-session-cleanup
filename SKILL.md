---
name: claude-session-cleanup
description: Read-only preview of which local Claude Code sessions a retention policy would select, using age, latest-count, and exact current-project rules, plus a report of how Claude Code's own cleanupPeriodDays cleanup is configured. Use when the user asks which Claude Code sessions are old, wants to see what keeping only recent sessions would affect, asks how much space sessions use, or asks when Claude Code deletes transcripts. It cannot delete, archive, or move sessions. Do not use for memory, settings, history, plugins, worktrees, background-agent management, or general Claude Code cleanup.
---

# Claude Session Cleanup (read-only)

Run `scripts/cleanup_sessions.py` (next to this file) to preview a retention
policy. This version only reports; it never changes any file and never
launches Claude Code.

## Interpret the request

Choose only these arguments:

- "older than 48 hours / 7 days" → `--older-than 48h` / `--older-than 7d`
  (positive whole hours or days).
- "keep my latest 5" → `--keep-latest 5`.
- Both constraints → pass both; they combine by intersection (a session group
  must be older than the cutoff *and* outside the latest N).
- Use the default `--project current` unless the user explicitly asks about
  all projects; then add `--project all`.
- At least one selector is required. For a vague request ("clean up my
  sessions"), ask for an age or a count, and say this tool only previews.
- Add `--verbose` when the user wants reasons for protected or excluded
  sessions.

Do not invent flags. There is no `--apply`, `--action`, archive, quarantine,
restore, or delete option in this version.

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

- how many session groups would be selected, protected, and excluded, with
  sizes;
- the main protection reasons (latest N, newer than cutoff, this session,
  running, recently active);
- any excluded groups and why (the tool refuses to judge unknown or unsafe
  metadata);
- the "Claude Code's own cleanup" section: configured period, and any
  sessions the policy keeps that Claude Code may delete anyway. Present this
  as an estimate; never state a date when Claude Code will delete something.

End by saying no changes were made. If the user wants sessions removed, say
this version cannot do it and that `claude purge <path>` removes a whole
project's Claude Code data (including its auto memory), not individual
sessions. Never delete or move files under `~/.claude` yourself.

If the user wants Claude Code to keep transcripts longer, explain that
`"cleanupPeriodDays": 3650` in `~/.claude/settings.json` does that, that
project or managed settings can override it, and let the user decide. Do not
edit settings on their behalf as part of this skill.

## Safety stops

If the helper prints `Safety stop:` it exits with status 1 and made no
changes. Report the message as is. Do not work around it by inspecting
session files manually.
