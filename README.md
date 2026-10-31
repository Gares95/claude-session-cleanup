# claude-session-cleanup

[![tests](https://github.com/Gares95/claude-session-cleanup/actions/workflows/tests.yml/badge.svg)](https://github.com/Gares95/claude-session-cleanup/actions/workflows/tests.yml)

Retention previews and reviewed permanent deletion for local Claude Code
sessions.

`claude-session-cleanup` shows which of your Claude Code sessions a retention
policy such as "older than 30 days, but keep my latest 10" would select, which
it keeps and why, and which it refuses to judge because their metadata is
unknown or unsafe. It also tells you how Claude Code's own `cleanupPeriodDays`
cleanup is configured, and which sessions you are keeping that it may delete
anyway. On Linux it can then permanently delete the selected conversations,
but only through a plan you saved and reviewed.

> [!IMPORTANT]
> **Every run is a read-only preview** unless you pass `--apply-plan`.
> Permanent deletion is irreversible, Linux-only, and always takes three
> steps: preview, save a plan, apply that plan. There is no `--apply`, no
> archive, and no quarantine or restore. The tool never launches Claude Code.
> See [docs/safety-model.md](docs/safety-model.md).

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

`claude purge` removes a whole project (including its auto memory, task
lists, and prompt history), not individual sessions, and Claude Code has no
command that deletes one conversation. This tool fills the gap with a
precise, explainable preview and, if you want it, a selective deletion of
exactly what you reviewed.

## What it does

- Discovers every session under `~/.claude/projects/` (or
  `$CLAUDE_CONFIG_DIR`), together with everything Claude Code keeps for it:
  subagent transcripts, tool results, checkpoints, per-session environment
  data.
- Selects sessions with `--older-than` and `--keep-latest`, combined by
  intersection, for the exact current directory or, explicitly, all projects.
- Decides per **root group**: a session plus its subagents and any sessions
  Claude Code links to it (background continuations). Recent activity
  anywhere in a group keeps the whole group.
- Reports every group as **selected**, **retained** (kept by the policy),
  **protected** (kept by a safety rule), or **excluded** (unknown or unsafe
  metadata).
- Protects the session you are running it from, sessions registered as
  running, background-session records, anything active within the last
  hour, and, on Linux, every session of a project in which another Claude
  Code process is running.
- Excludes, rather than guesses about, sessions with missing or unverifiable
  project identity, symbolic links that leave session data, unreadable or
  empty transcripts, and other unknown metadata.
- Reports sizes per session and per kind of data.
- On Linux, deletes the selected groups' transcripts, subagent transcripts,
  tool results, checkpoints, and per-session environment data through a
  reviewed saved plan, after re-checking every file
  ([Permanent deletion](#permanent-deletion)).
- Prints a terminal preview or the versioned JSON preview shared with
  `codex-session-cleanup` ([contract/](contract/SPEC.md),
  [docs/json-contract.md](docs/json-contract.md)).

## Requirements

- Python 3.10 or newer. Standard library only; no dependencies.
- Checked against the storage layout of Claude Code 2.1.310, 2.1.314, and
  2.1.316. Other versions run with a warning.

Validation status:

| Environment | Status |
|---|---|
| Linux (Ubuntu 24.04 under WSL2), Python 3.12 | Test suite passes; previews and staged permanent deletion validated against a real configuration directory (Claude Code 2.1.316) |
| Windows 10, Python 3.12 and 3.14 | Test suite passes (symlink and FIFO tests skip without symlink privilege); read-only run against a real Windows configuration directory |
| Linux, macOS, Windows × Python 3.10–3.13 | Test suite runs in [CI](https://github.com/Gares95/claude-session-cleanup/actions/workflows/tests.yml) on every push |
| macOS | Not yet run against a real configuration directory |

## Install

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

# Everything except the 5 most recently active root groups
claude-session-cleanup --keep-latest 5

# Older than 30 days AND outside the latest 10, with details
claude-session-cleanup --older-than 30d --keep-latest 10 --verbose

# All projects, as JSON for auditing or scripting
claude-session-cleanup --older-than 90d --project all --json
```

| Option | Meaning |
|---|---|
| `--older-than DURATION` | Select groups whose last activity is older than `DURATION` (`48h`, `30d`; whole hours or days) |
| `--keep-latest N` | Retain the `N` most recently active root groups |
| `--project current\|all` | `current` (default): the exact current directory only. `all`: every project |
| `--min-idle DURATION` | Protect groups with any activity within `DURATION` (default `1h`) |
| `--json` | Print the JSON report instead of text |
| `--include-titles` | Include session titles in JSON (omitted by default; titles can contain prompt text) |
| `--verbose` | List retained, protected, and excluded groups, reasons, and per-kind sizes |
| `--full-ids` / `--no-titles` | Text-output display options |
| `--config-dir PATH` | Inspect another Claude Code configuration directory |
| `--action delete` | Mark selected groups for permanent deletion in the preview (`archive` and `quarantine` are refused) |
| `--save-plan PATH` | With `--action delete`: write the deletion plan to a new private file |
| `--apply-plan PATH` | Delete exactly what a saved plan lists, after re-checking; takes no policy options |

At least one of `--older-than` or `--keep-latest` is required for a preview.
Exit status: 0 success, 1 safety stop or a deletion that stopped part-way, 2
usage error or unusable plan, 3 stale plan (nothing deleted), 4 plan locked
by another run.

Example output (synthetic data; see [examples/](examples/)):

```text
Examined 6 groups (6 sessions, 5.6 KB)
                GROUPS  SESSIONS       SIZE
  Would select       2         2     2.3 KB
  Retained           3         3     3.1 KB
  Protected          0         0        0 B
  Excluded           1         1      216 B

Selected (eligible for cleanup; nothing is changed):
  LAST ACTIVITY     AGE    SIZE      SESSION   TITLE
  2026-09-21 12:00  34d      1008 B  aaaa0002  Add Stripe webhooks
  2026-09-10 12:00  45d      1.3 KB  aaaa0001  Refactor upload pipeline

Retained: newer than cutoff: 3 · within latest N: 2
Excluded: no recorded working directory: 1

Claude Code's own cleanup (estimate; this tool does not control it):
  Configured: user settings 30 days
  Lowest applicable period found: 30 days
  Transcripts last modified longer ago than that period: 3 groups; within 7 days of it: 1 group
  2 groups this policy keeps are past or near that period; Claude Code may delete them anyway.
  Estimates only. Claude Code deletes transcripts on its own schedule; this tool cannot predict exact deletion times or prevent the deletion.

Deletion: available here, only through a reviewed saved plan (--action delete --save-plan PATH, then --apply-plan PATH).
Active-session detection is partial; see docs/safety-model.md.
No changes made.
```

## Permanent deletion

```bash
cd /path/to/project

# 1. Preview what would be deleted, with reasons and sizes
claude-session-cleanup --older-than 30d --action delete --verbose

# 2. Save the plan to a private, ignored location
claude-session-cleanup --older-than 30d --action delete --save-plan .local/delete-plan.json

# 3. Review the printed preview (and the plan file if you like)

# 4. Apply it from the same directory, within 24 hours
claude-session-cleanup --apply-plan .local/delete-plan.json
```

- **What is removed** for each member session of a selected group: its
  transcript and set-aside copies, its `projects/<storage>/<id>/` directory
  (subagent transcripts, tool results, MCP task state, title sidecar),
  `file-history/<id>/`, `session-env/<id>/`, and `debug/<id>.txt`.
- **What is never touched:** project auto memory, prompt history
  (`history.jsonl`), `~/.claude.json`, settings, credentials, plugins, other
  projects, Codex state, and your project's files. `tasks/<id>`,
  `image-cache/<id>`, `uploads/<id>`, and `dev-mods/<id>` are left in place
  and listed, because their ownership by one conversation is not documented.
- **Groups that cannot be deleted** stay `selected` with a reason under
  `mutation_blockers`, for example an unrecognized file in the session's
  data or data shared with another session through links.
- **Applying re-checks everything.** It rediscovers your sessions, re-runs
  the activity checks and the saved policy with the saved cutoff, and
  compares every group, member, and file (type, size, timestamps, inode)
  with the plan. Any difference refuses the whole plan with nothing deleted;
  preview again and save a new plan. A plan works once and expires after 24
  hours.
- **Running sessions are never deleted.** On Linux the tool inspects your
  Claude Code processes: if one is working in the project, names a session,
  or holds its files open, those sessions are protected. Run the deletion
  when no other Claude Code session is open in that project; the session
  you run it from is protected automatically.
- **Nothing is copied.** Deletion is permanent and makes no backup.
  Deleted sessions disappear from `claude --resume`.
- **Plans are private.** They contain local paths and full session ids
  (never titles or messages), are created with mode `0600`, and should live
  under an ignored directory such as `.local/`.

## How decisions are made

These semantics are a shared contract with
[codex-session-cleanup](https://github.com/Gares95/codex-session-cleanup):
[contract/SPEC.md](contract/SPEC.md), with Claude Code specifics in
[docs/retention-semantics.md](docs/retention-semantics.md).

- **Last activity** is the newest evidence across a group: transcript and
  subagent modification times, tool results, checkpoints, and message
  timestamps. Imprecision always errs toward keeping.
- **`--keep-latest N`** ranks groups newest first, ties broken by root id.
  A group counts once however many sessions it contains. Protected groups
  occupy ranks; excluded groups do not, so unknown data can only cause more
  sessions to be kept.
- **Statuses** take precedence in this order: excluded, protected, retained,
  selected.
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

## Archive, quarantine, and delete

These words mean different things here and in
[codex-session-cleanup](https://github.com/Gares95/codex-session-cleanup), and
are never used interchangeably ([contract/SPEC.md](contract/SPEC.md)):

- **Archive** is an operation the agent itself provides and can undo. Codex
  has one; Claude Code does not, so this tool offers no archive.
- **Quarantine** would be a reversible move managed by this tool. It is not
  implemented.
- **Permanent delete** is irreversible. This tool deletes only through a
  reviewed saved plan (`--apply-plan`), on Linux. Claude Code's own
  `cleanupPeriodDays` sweep and `claude purge` are separate permanent
  deletes performed by Claude Code.

"Selected" means "eligible for the requested action"; in a preview nothing
happens to selected sessions.

## Limitations

- **Deletion is Linux-only.** It needs the Linux process scan; on macOS and
  Windows the preview reports `delete` as unsupported and `--save-plan`
  refuses.
- **Active-session detection is partial.** Claude Code keeps no complete list
  of running sessions. The tool combines its session registry, background
  session records, the invoking session, a one-hour minimum-idle guard, and
  on Linux a scan of your Claude Code processes. Sessions under another
  `CLAUDE_CONFIG_DIR`, on another machine, or in another PID namespace (a
  container sharing the directory) are not visible.
- **Some data stays after deletion.** Prompt-history lines, a
  `lastSessionId` in `~/.claude.json`, and the per-session `tasks/`,
  `image-cache/`, `uploads/`, and `dev-mods/` directories are not removed.
- **Background continuations** that share subagent data through links
  cannot be deleted yet (`cross-session-symlink`).
- **Undocumented storage.** The layout under `~/.claude/` is internal to
  Claude Code and can change in any release. Transcripts from untested
  versions produce a `claude-code-version-untested` warning.
- **No pinning.** Claude Code has no pin or favourite marker, so pinned
  sessions cannot be protected (`capabilities.pinning: false`).
- **Estimates for Claude Code's own cleanup.** The tool cannot see settings of
  projects it does not know about, nor predict when Claude Code next runs its
  sweep.

## Privacy and safety

- Local only: no network access, no subprocesses, no telemetry.
- Writes nothing except a plan file you asked for, and deletes nothing
  except what an applied plan lists.
- Reads only the metadata it needs; message content is used only to show a
  title on your terminal. JSON omits titles unless `--include-titles` is
  given.
- Never follows symbolic links inside session data; titles and paths are
  stripped of terminal control characters.
- JSON previews and deletion plans contain local paths and full session ids
  (previews also titles with `--include-titles`). Treat them as private: if
  you save one inside a repository, put it under an ignored directory such
  as `.local/`.
- See [docs/safety-model.md](docs/safety-model.md) for limitations, including
  why active-session detection is incomplete.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The suite runs on synthetic configuration directories, fails any test that
touches the real `~/.claude`, starts a process, or sends a signal, and needs
no dependencies. It also runs the agent-neutral conformance fixtures in
[`contract/fixtures/`](contract/fixtures/), which
[codex-session-cleanup](https://github.com/Gares95/codex-session-cleanup) also
runs against its own engine (see
[contract/compatibility-matrix.md](contract/compatibility-matrix.md)).

## License

MIT. Copyright (c) 2026 Gares95.
