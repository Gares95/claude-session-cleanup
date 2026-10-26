# claude-session-cleanup

Read-only inspection and retention planning for local Claude Code sessions.

`claude-session-cleanup` shows which of your Claude Code sessions a retention
policy such as "older than 30 days, but keep my latest 10" would select, which
are protected and why, and which it refuses to judge because their metadata is
unknown or unsafe. It also tells you how Claude Code's own `cleanupPeriodDays`
cleanup is configured, and which sessions you are keeping that it may delete
anyway.

> [!IMPORTANT]
> **Version 0.1 is a read-only first implementation.** It has no archive,
> quarantine, restore, or delete functionality. It never moves, renames,
> edits, or deletes anything, and it never launches Claude Code. Any future
> cleanup action is blocked on the safety requirements listed in
> [docs/safety-model.md](docs/safety-model.md).

## Why

Claude Code deletes old transcripts by itself, silently, based on file
modification time. In Claude Code 2.1.310 that cleanup:

- has no preview and no per-session selection;
- cannot keep "my latest N sessions";
- deletes an old session's whole data directory, including subagent
  transcripts written minutes earlier;
- reads `cleanupPeriodDays` from the settings of whichever project you start
  Claude Code in, so one repository's `.claude/settings.json` can shorten
  retention for every project.

`claude purge` removes a whole project (including its auto memory), not
individual sessions. This tool fills the gap with a precise, explainable
preview.

## What it does

- Discovers every session under `~/.claude/projects/` (or
  `$CLAUDE_CONFIG_DIR`), together with everything Claude Code keeps for it:
  subagent transcripts, tool results, checkpoints, per-session environment
  data.
- Selects sessions with `--older-than` and `--keep-latest`, combined by
  intersection, for the exact current directory or, explicitly, all projects.
- Decides per **root session group**: a session plus its subagents and any
  sessions Claude Code links to it (background continuations). Recent
  activity anywhere in a group protects the whole group.
- Protects the session you are running it from, sessions registered as
  running, background-session records, and anything active within the last
  hour.
- Excludes, rather than guesses about, sessions with missing or unverifiable
  project identity, symbolic links that leave session data, unreadable or
  empty transcripts, and other unknown metadata.
- Reports sizes per session and per kind of data.
- Prints a terminal preview or a versioned JSON report
  ([docs/json-contract.md](docs/json-contract.md)).

## Requirements

- Python 3.10 or newer is the intended floor. Standard library only; no
  dependencies.
- Checked against the storage layout of Claude Code 2.1.310. Other versions
  run with a warning.

Validation status:

| Environment | Status |
|---|---|
| Linux (Ubuntu 24.04 under WSL2), Python 3.12 | Test suite passes; read-only run against a real configuration directory |
| Python 3.10, 3.11, 3.13 | Not yet validated (CI job configured, not yet run) |
| macOS | Not yet validated (CI job configured, not yet run) |
| Windows | Not yet validated (CI job configured, not yet run) |

## Install

The repository is not published yet; the URLs below are its intended home.
Until then, use a local checkout.

As a Claude Code skill (user scope):

```bash
git clone https://github.com/Gares95/claude-session-cleanup \
  ~/.claude/skills/claude-session-cleanup
```

Then ask Claude, for example: "Which of my sessions in this project are older
than 30 days?" The skill runs the preview and explains it.

As a command-line tool:

```bash
pipx install git+https://github.com/Gares95/claude-session-cleanup
claude-session-cleanup --help
```

Or without installing anything:

```bash
python3 path/to/claude-session-cleanup/scripts/cleanup_sessions.py --help
```

Uninstall by deleting the cloned directory (or `pipx uninstall
claude-session-cleanup`). The tool writes no files of its own.

## Usage

```bash
# Sessions in this exact directory inactive for more than 7 days
claude-session-cleanup --older-than 7d

# Everything except the 5 most recently active session groups
claude-session-cleanup --keep-latest 5

# Older than 30 days AND outside the latest 10, with details
claude-session-cleanup --older-than 30d --keep-latest 10 --verbose

# All projects, as JSON for auditing or scripting
claude-session-cleanup --older-than 90d --project all --json
```

| Option | Meaning |
|---|---|
| `--older-than DURATION` | Select groups whose last activity is older than `DURATION` (`48h`, `30d`; whole hours or days) |
| `--keep-latest N` | Keep the `N` most recently active session groups |
| `--project current\|all` | `current` (default): the exact current directory only. `all`: every project |
| `--min-idle DURATION` | Protect groups with any activity within `DURATION` (default `1h`) |
| `--json` | Print the JSON report instead of text |
| `--include-titles` | Include session titles in JSON (omitted by default; titles can contain prompt text) |
| `--verbose` | List protected and excluded groups, reasons, and per-kind sizes |
| `--full-ids` / `--no-titles` | Text-output display options |
| `--config-dir PATH` | Inspect another Claude Code configuration directory |

At least one of `--older-than` or `--keep-latest` is required. Exit status is
0 when a preview was printed, 1 for a safety stop, 2 for a usage error.

Example output (synthetic data; see [examples/](examples/)):

```text
Examined 6 groups (6 sessions, 5.6 KB)
                GROUPS  SESSIONS       SIZE
  Would select       2         2     2.3 KB
  Protected          3         3     3.1 KB
  Excluded           1         1      216 B

Selected (eligible for a future cleanup action; nothing is changed):
  LAST ACTIVITY     AGE    SIZE      SESSION   TITLE
  2026-09-21 12:00  34d      1008 B  aaaa0002  Add Stripe webhooks
  2026-09-10 12:00  45d      1.3 KB  aaaa0001  Refactor upload pipeline

Protected: newer than cutoff: 3 · within latest N: 2
Excluded: no recorded working directory: 1

Claude Code's own cleanup (estimate; this tool does not control it):
  Configured: user settings 30 days
  Lowest applicable period found: 30 days
  Transcripts last modified longer ago than that period: 3 groups; within 7 days of it: 1 group
  2 groups this policy keeps are past or near that period; Claude Code may delete them anyway.
  Estimates only. Claude Code deletes transcripts on its own schedule; this tool cannot predict exact deletion times or prevent the deletion.
```

## How decisions are made

Full rules: [docs/retention-semantics.md](docs/retention-semantics.md).

- **Last activity** is the newest evidence across a group: transcript and
  subagent modification times, tool results, checkpoints, and message
  timestamps. Imprecision always errs toward keeping.
- **`--keep-latest N`** ranks groups newest first, ties broken by session id.
  A group counts once however many sessions it contains. Protected groups
  occupy ranks; excluded groups do not, so unknown data can only cause more
  sessions to be kept.
- **Exact project matching.** The invoking directory is resolved through
  symlinks. A session belongs to it only if a working directory recorded in
  the transcript encodes to exactly the storage folder that holds it, and
  equals the invoking directory. Parent and child directories, sibling git
  worktrees, moved repositories, and storage-name collisions (`/a-b` vs.
  `/a/b`) never match.

## Claude Code's own cleanup

The report separates three things
([docs/native-retention.md](docs/native-retention.md)):

- **Observed**: transcript modification times and Claude Code's
  `.last-cleanup` marker.
- **Configured**: every `cleanupPeriodDays` value the tool could read
  (managed settings, cached server settings, user settings, and the project
  settings of projects it knows about), plus what it could not inspect.
- **Estimated risk**: whether each transcript is already older than the
  lowest applicable period, or within 7 days of it. It never computes a
  deletion date.

To make Claude Code keep transcripts much longer, set `"cleanupPeriodDays":
3650` in `~/.claude/settings.json`. Project and managed settings can still
override it. This tool never changes your settings.

## Privacy and safety

- Local only: no network access, no subprocesses, no telemetry.
- Reads only the metadata it needs; message content is used only to show a
  title on your terminal. JSON omits titles unless `--include-titles` is
  given.
- Never follows symbolic links inside session data; titles and paths are
  stripped of terminal control characters.
- See [docs/safety-model.md](docs/safety-model.md) for limitations, including
  why active-session detection is incomplete.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The suite runs on synthetic configuration directories, fails any test that
touches the real `~/.claude`, starts a process, or sends a signal, and needs
no dependencies. Retention behaviour is also captured as agent-neutral
vectors in `tests/vectors/retention/` for reuse by
[codex-session-cleanup](https://github.com/Gares95/codex-session-cleanup).

## License

MIT. Copyright (c) 2026 Gares95.
