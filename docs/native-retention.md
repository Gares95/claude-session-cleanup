# Claude Code's own retention

Claude Code deletes old transcripts by itself. This tool cannot control that
cleanup, and it cannot predict exactly when it runs. It reports what it can
observe and what is configured, and labels everything else an estimate.

## Behaviour this model is based on

Verified on Claude Code 2.1.310 with synthetic data in an isolated
`CLAUDE_CONFIG_DIR`, and from the official documentation:

- The setting is `cleanupPeriodDays`: default 30, minimum 1. `0` is rejected;
  use a large value such as `3650` for long retention.
- A background sweep runs after a session starts, at most once per session,
  and is delayed if one ran in the last 24 hours. It is silent.
- A transcript's age is its **file modification time**. A transcript with old
  messages but a recent modification time was kept; one with recent messages
  but an old modification time was deleted.
- Deleting a transcript also deletes the whole `projects/<storage>/<id>/`
  directory, **including subagent transcripts and tool results written
  minutes earlier**, plus `file-history/<id>/` and `session-env/<id>/`.
- Session names give no protection.
- Auto memory (`projects/<storage>/memory/`) is kept.
- `cleanupPeriodDays` may be set in any settings file. The sweep uses the
  merged settings of the session that runs it, so starting Claude Code in a
  repository whose committed `.claude/settings.json` sets a low value deletes
  older transcripts of **every** project. Observed with `claude -p` (which
  skips the folder-trust prompt). In interactive mode, an untrusted folder's
  trust prompt held the session and no sweep ran while it was shown; after
  trust was accepted, no sweep ran within 13 minutes in an isolated test (no
  model request was ever made and a sweep had run on the machine within 24
  hours), so the interactive case remains unverified.
- Managed settings override every other source. If a settings file cannot be
  read or is invalid, Claude Code may pause the sweep.
- Claude Desktop and Cowork transcripts follow
  `desktopSessionCleanupPeriodDays` (not modelled here).
- Behaviour has changed across releases (the changelog records sweeps that
  deleted more than intended), so treat this model as version-specific.

## What the report contains

### Observed

- Each session's transcript modification time
  (`sessions[].native_retention.transcript_modified_at`): the value the sweep
  uses.
- The `.last-cleanup` marker Claude Code writes after a sweep, if present.
  This tells you when a sweep last ran on this configuration directory, not
  when the next one will.

### Configured

Every `cleanupPeriodDays` source the tool could read, each with its path and
status (`set`, `unset`, `missing`, `invalid`, `unreadable`):

| Kind | Path |
|---|---|
| `managed-file` | `/etc/claude-code/managed-settings.json` (Linux, WSL), `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), `C:\Program Files\ClaudeCode\managed-settings.json` (Windows) |
| `managed-drop-in` | `managed-settings.d/*.json` next to it |
| `server-managed-cache` | `<config>/remote-settings.json` |
| `user` | `<config>/settings.json` |
| `project-shared` | `<project>/.claude/settings.json` |
| `project-local` | `<project>/.claude/settings.local.json`, and the repository root's when the project is a subdirectory |

Projects read: the invoking directory and every project path verified among
the reported groups.

- When any managed source sets a value, it is the only applicable value (the
  lowest one if several managed sources set different values).
- Otherwise the applicable values are the user value (or the default, 30)
  plus every project value found. `lowest_applicable_days` is the smallest:
  the worst case among the sources the tool can see.
- `sweep_may_pause` is true when a non-managed source is invalid or
  unreadable.
- `complete` is always false. Not inspected: MDM profiles, Windows registry
  policies, server-managed settings not cached on disk, `--settings` and
  `--setting-sources` flags, embedding hosts, and any project directory not
  listed above.

### Estimated risk

Each transcript is classified against `lowest_applicable_days`:

| Risk | Meaning |
|---|---|
| `past-period` | Modified longer ago than the period. Claude Code may delete it the next time its cleanup runs with that setting. |
| `approaching` | Within 7 days of the period. |
| `within-period` | Otherwise. |

A group's risk is the worst of its members'. The report counts protected and
selected groups at risk. A protected group at risk means the policy you
previewed would keep it, but Claude Code may delete it anyway.

The tool never outputs a projected deletion time: whether and when a sweep
runs depends on when sessions start, which project they start in, managed
policy, and the Claude Code version.

## Guidance

- To make this tool's policy the effective one, raise Claude Code's own
  period, for example `"cleanupPeriodDays": 3650` in
  `~/.claude/settings.json`.
- Check repositories you open for committed `cleanupPeriodDays` values.
- Managed settings cannot be overridden locally.
- The tool never edits settings.
