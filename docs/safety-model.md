# Safety model

## Version 0.1 guarantees

- **No writes.** The tool opens files read-only and creates, modifies,
  renames, and deletes nothing. It writes only to standard output and
  standard error.
- **No processes.** It never runs `claude`, `git`, or any other program. In
  particular it does not run `claude agents --json`: every Claude Code launch
  may start Claude Code's own retention sweep and writes `~/.claude.json`.
- **No network.**
- **No symlink following** inside session data. Links are inspected with
  `lstat`/`readlink`; transcripts are opened with `O_NOFOLLOW` where the
  platform supports it, and with `O_NONBLOCK` so a special file cannot hang
  the run.
- **Minimal content handling.** Only the first 100 records, the last 256 KiB,
  and short metadata lines (titles, continuation links) of each transcript
  are parsed, and only a few fields are kept. A title (custom title, AI title, or
  first prompt) is shown on the terminal; JSON includes titles only with
  `--include-titles`. Titles and paths are stripped of control and
  bidirectional formatting characters before printing.
- **Fail toward keeping.** Unknown or unsafe metadata excludes a group
  instead of guessing; last activity is the newest evidence available.

These are enforced by the test suite: every test runs under a guard that
fails on access to the real `~/.claude`, on any process launch, and on any
signal, and the read-only tests compare complete directory snapshots before
and after a run.

## Project identity

See [retention-semantics.md](retention-semantics.md#scope). Storage folder
names are a lossy encoding of paths; the tool never infers a project from the
folder name alone, never widens `current` scope to parents, children, or
worktrees, and excludes sessions it cannot verify.

## Symbolic links

- A symlinked `projects/` directory stops the run (`Safety stop`).
- A symlinked project storage folder is skipped with a warning.
- A symlinked transcript, or any link inside a unit that does not point into
  another session's `projects/<storage>/<id>/` directory, excludes the group
  (`unsafe-storage`; the issue code is `symlink-in-session`).
- Links that do point into another session's data (Claude Code creates them
  for background continuations) are not followed; they join the two sessions
  into one group and add the `cross-session-symlink` mutation blocker.

## Active-session detection (incomplete)

Signals used, all protective:

| Signal | Strength |
|---|---|
| `CLAUDE_CODE_SESSION_ID` of the invoking session | Authoritative for that session |
| `sessions/<pid>.json` registry with the PID's start time matching `/proc/<pid>/stat` | Strong, Linux only |
| Registry entry whose PID exists but cannot be verified (macOS, Windows, no start time) | Weak; treated as possibly running |
| `jobs/*/state.json` background-session records | Heuristic; any record protects |
| Activity within `--min-idle` (default 1 hour) | Heuristic |

Not detected: sessions using another `CLAUDE_CONFIG_DIR`; sessions on another
machine sharing this directory; Claude Code versions or surfaces that do not
write the registry; sessions resumed after the preview ran.

`claude agents --json` is documented as the supported way to read session
state, but it is not known to list every running interactive session, and
running it launches Claude Code. It is therefore neither used nor relied on.

## Blockers before any mutation is added

A cleanup action (planned: a restorable quarantine) stays disabled until all
of these are resolved, tested, and documented:

1. **Active-session detection.** A source of truth that covers every
   interactive, background, IDE, and Desktop session on this configuration
   directory, or a supported Claude Code interface that refuses to act on a
   running session. Neither `claude agents --json` (completeness unverified)
   nor the registry is sufficient today.
2. **Supported mutation interface.** Claude Code has no supported
   per-session delete or archive. Any file-level action must handle every
   artifact kind, unknown associated artifacts, and cross-session symlinks.
3. **Concurrency.** Fingerprint revalidation immediately before each change;
   no lock prevents Claude Code from writing.
4. **Native retention.** Restored or kept data can still be deleted by
   Claude Code's own cleanup; every mutating preview must say so.
5. **Approved plans.** Exact candidate sets saved, reviewed, revalidated,
   expiring, and not replayable.
6. **End-to-end tests** against the real Claude Code binary in an isolated
   configuration directory, for each supported version.

## Compatibility

Checked against the storage layout of Claude Code 2.1.310. The transcript
format is internal to Claude Code and can change in any release; the tool
reports `claude-code-version-untested` when transcripts were written by a
version outside the tested list, and tolerates unknown records.

Validated so far on Linux (Ubuntu 24.04 under WSL2) with Python 3.12, and on
Windows 10 with Python 3.12 and 3.14 (symlink-dependent tests skip without
the Windows symlink privilege). The CI workflow runs the test suite on Linux,
macOS, and Windows with Python 3.10 to 3.13; macOS has not been run against a
real configuration directory. On Windows, process liveness is never probed (`os.kill`
terminates processes there), so every registry entry counts as possibly
running.
