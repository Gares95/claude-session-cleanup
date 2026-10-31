# Safety model

## Guarantees

- **Previews never write.** Every run without `--apply-plan` opens files
  read-only and changes nothing. `--save-plan` additionally creates one new
  file, the plan, outside the Claude Code configuration directory.
- **Permanent deletion only through a reviewed saved plan.** There is no
  `--apply`. `--action delete --save-plan PATH` records exactly what would be
  removed; `--apply-plan PATH` removes exactly that, after re-checking
  everything, or nothing at all.
- **No processes.** The tool never runs `claude`, `git`, or any other
  program. In particular it does not run `claude agents --json` or
  `claude purge`: every Claude Code launch may start Claude Code's own
  retention sweep and writes `~/.claude.json`, and `purge` removes a whole
  project's state, not one conversation.
- **No network.**
- **No symlink following** inside session data. Links are inspected with
  `lstat`/`readlink`; transcripts are opened with `O_NOFOLLOW` where the
  platform supports it, and with `O_NONBLOCK` so a special file cannot hang
  the run. Deletion walks every path component through directory
  descriptors opened with `O_NOFOLLOW`.
- **Minimal content handling.** Only the first 100 records, the last 256 KiB,
  and short metadata lines (titles, continuation links) of each transcript
  are parsed, and only a few fields are kept. A title is shown on the
  terminal; JSON includes titles only with `--include-titles`; plans never
  contain titles or message content. Titles and paths are stripped of
  control and bidirectional formatting characters before printing.
- **Fail toward keeping.** Unknown or unsafe metadata excludes a group
  instead of guessing; last activity is the newest evidence available; any
  doubt during deletion stops the run.

These are enforced by the test suite: every test runs under a guard that
fails on access to the real `~/.claude` (including removals relative to a
directory descriptor), on any process launch, and on any signal; previews
compare complete directory snapshots before and after a run; and the
deletion tests compare content digests of everything outside the planned
targets.

## What deletion removes

Claude Code has no command that deletes one conversation: `claude purge`
removes a project's transcripts, auto memory, task lists, file history,
prompt-history lines, and its `~/.claude.json` entry, and `claude rm` only
removes background-session records. The tool therefore removes the exact
files it attributes to each member session of a selected group:

| Removed | Notes |
|---|---|
| `projects/<storage>/<id>.jsonl` | The transcript, removed last |
| `projects/<storage>/<id>.<suffix>` | Set-aside copies of the same transcript |
| `projects/<storage>/<id>/` | Only if it contains nothing but `subagents/agent-*.jsonl` and `agent-*.meta.json`, `tool-results/*`, `mcp-tasks/*`, and `custom-title.json` |
| `file-history/<id>/`, `session-env/<id>/` | Flat directories of regular files named for the session |
| `debug/<id>.txt` | The session's debug log |

| Left in place | Why |
|---|---|
| `tasks/<id>/`, `image-cache/<id>/`, `uploads/<id>/`, `dev-mods/<id>/` | Ownership by a single conversation is not documented; reported as `left_in_place` |
| `projects/<storage>/memory/` | Project auto memory, shared by every session of the project |
| `history.jsonl` | Global prompt history; lines that mention a deleted session stay |
| `~/.claude.json` | Its project entry may keep a `lastSessionId` naming a deleted session; Claude Code ignores it (`--continue` starts a new conversation) |
| Everything else | Settings, credentials, plugins, MCP configuration, plans, caches, other projects |

A group cannot be deleted (it stays `selected` with a `mutation_blockers`
entry) when any member has:

| Blocker | Meaning |
|---|---|
| `unrecognized-session-data` | Something in its data directory that is not on the list above, or a directory where a file belongs |
| `unknown-associated-artifact` | A file elsewhere in the configuration directory named after the session |
| `cross-session-symlink` | Data linked from another session (background continuations) |
| `linked-session-not-found` | A continuation link to a session that does not exist |
| `storage-inside-project` | The configuration directory lies inside the project being cleaned |

## Saved plans

A plan (`claude-session-cleanup-plan`, version 1, JSON, mode `0600`, never
overwrites an existing file, refused inside the configuration directory)
records:

- the policy with its **absolute** cutoff, scope, and minimum idle period;
- the tool version, platform, PID namespace (`pidDomain`), and the
  configuration directory's path and inode;
- for each deletable group: members with parents and last activity, every
  target with type, size, modification and change times in nanoseconds,
  device and inode, and what is left in place;
- the selected groups that cannot be deleted, with their blockers;
- a SHA-256 digest of all of the above, and a state (`saved`, `applying`,
  `applied`, `failed`).

`--apply-plan` refuses the plan, with nothing deleted, when:

| Check | Exit |
|---|---|
| Not valid JSON, wrong format or version, digest mismatch, malformed or duplicate targets, path traversal | 2 |
| State is not `saved` (replay, interrupted run) | 2 |
| Saved by another tool version, on another machine or PID namespace, for another configuration directory, from another directory (current scope), more than 24 hours ago, or in the future | 2 |
| Another run holds the plan's lock | 4 |
| Rediscovery and the saved policy, with the saved cutoff and the current activity checks, do not produce exactly the same deletable groups, blocked groups, members, last activity, left-in-place data, and target metadata | 3 |
| A target is outside its session's storage, inside Codex state, behind a symbolic link, or changed type, size, timestamps, or inode | 3 |

Only then is the state set to `applying` and targets are removed one at a
time, children before parents, descendants before roots, each transcript
last. Each target is re-checked through its parent's descriptor immediately
before `unlink` or `rmdir`; directories are removed with `rmdir`, so
anything that appeared since the check stops the run. On any surprise the
run stops, the state becomes `failed` with the list of removed paths, the
output says `STOPPED` with what remains, and the exit status is 1. Nothing
is copied anywhere: deletion is not a backup or a move.

## Active-session detection

Signals used, all protective:

| Signal | Strength |
|---|---|
| `CLAUDE_CODE_SESSION_ID` of the invoking session | Authoritative for that session |
| `sessions/<pid>.json` registry with the PID's start time matching `/proc/<pid>/stat` and, when recorded, its `pidDomain` matching this machine's | Strong, Linux only |
| Registry entry that cannot be verified (another PID namespace, macOS, Windows, no start time) | Weak; treated as possibly running |
| **Linux process scan** of the current user's Claude Code processes (native binary, `claude` launcher, or Node running `@anthropic-ai/claude-code`) | Strong for this PID namespace |
| `jobs/*/state.json` background-session records | Heuristic; any record protects |
| Activity within `--min-idle` (default 1 hour) | Heuristic |

The process scan reads `/proc/<pid>/{exe,cmdline,cwd,environ,fd,stat}` of
the current user's processes. A Claude Code process that uses this
configuration directory protects:

- every session of the project it works in (its working directory equals
  the project or lies inside it), because the session it is running cannot
  be identified with certainty from outside;
- every session it names on its command line (`--resume <id>`) or holds
  open under `projects/`;
- everything, when its working directory cannot be read while it is alive.

The process running the tool and its parents (the invoking Claude Code
session) are exempt from the project rule; the invoking session itself is
protected by its id. Exited and zombie processes are ignored. If any
current-user process cannot be inspected at all, deletion is disabled
(`process-scan-incomplete`).

Not detected: sessions using another `CLAUDE_CONFIG_DIR`; sessions on another
machine sharing this directory; sessions in another PID namespace (for
example a container that mounts this directory) unless their registry entry
records a different `pidDomain`; a session started in the milliseconds
between the final check and the removal.

## Platform support

| Platform | Preview | Delete |
|---|---|---|
| Linux with readable `/proc` | Yes | Yes (validated end to end, see below) |
| macOS | Yes | No: `capabilities.actions.delete.supported: false`, `unsupported_reason: "blocked-by-safety-requirements"`, mutation blocker `active-session-detection-incomplete` |
| Windows | Yes | No (same) |

On macOS and Windows only the registry, background records, and file
activity are available, which is not enough to exclude a running session.
On Windows, process liveness is never probed (`os.kill` terminates processes
there), so every registry entry counts as possibly running.

## Compatibility

Checked against the storage layout of Claude Code 2.1.310, 2.1.314, and
2.1.316. The transcript format is internal to Claude Code and can change in
any release; the tool reports `claude-code-version-untested` when
transcripts were written by a version outside the tested list, and tolerates
unknown records. Deletion depends on the layout described above: any entry
the tool does not recognize blocks deletion of that group.

Deletion was validated end to end on Linux (Ubuntu 24.04 under WSL2, Python
3.12) with Claude Code 2.1.316: a real project's conversations were deleted
in two stages, under an audit hook that recorded every filesystem event;
only planned targets were removed, nothing outside the plan's own file was
written, Codex state and the project's working tree were not accessed or
changed, and Claude Code reported the deleted sessions as not found.

Previews are validated on Windows 10 with Python 3.12 and 3.14
(symlink-dependent tests skip without the Windows symlink privilege). The CI
workflow runs the test suite on Linux, macOS, and Windows with Python 3.10
to 3.13; macOS has not been run against a real configuration directory.
