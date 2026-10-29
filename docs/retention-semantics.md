# Retention semantics (Claude Code backend)

The shared rules — root groups, last activity, scope, statuses, ranking,
tie-breaking, reason codes — are defined once in
[`contract/SPEC.md`](../contract/SPEC.md) §2–§3 and pinned by the fixtures in
[`contract/fixtures/`](../contract/fixtures/). This page describes how the
Claude Code backend maps its storage onto them. Implementation:
`claude_session_cleanup/discovery.py` and `policy.py`.

## Session units

A **session unit** is one top-level transcript,
`projects/<storage>/<session-id>.jsonl`, plus everything Claude Code keys by
that id:

| Path | Kind |
|---|---|
| `projects/<storage>/<id>.jsonl` | `transcript` |
| `projects/<storage>/<id>/subagents/…` | `subagent` |
| `projects/<storage>/<id>/tool-results/…` | `tool_result` |
| `projects/<storage>/<id>/mcp-tasks/…` | `mcp_task` |
| `projects/<storage>/<id>/custom-title.json` | `title_sidecar` |
| other entries under `projects/<storage>/<id>/` | `session_data_other` |
| `projects/<storage>/<id>.<suffix>` (superseded, orphaned, backup copies) | `transcript_variant` |
| `file-history/<id>/` | `file_history` |
| `session-env/<id>/` | `session_env` |
| `tasks/<id>/`, `image-cache/<id>/`, `uploads/<id>/`, `dev-mods/<id>/` | per name |
| `debug/<id>.txt` | `debug_log` |

Never part of a unit: `projects/<storage>/memory/` (auto memory),
`history.jsonl`, `paste-cache/`, `plans/`, settings, credentials,
`~/.claude.json`, `jobs/`, `daemon/`, `sessions/`.

Any other entry one level below a top-level configuration directory whose name
contains the session id is reported as an *unknown associated artifact*: a
mutation blocker, not a status change.

Subagents are data inside their parent's unit, not separate sessions.

## Root groups

Units are joined into root groups by links Claude Code itself records:

- a `continued-in` transcript record (written when a conversation moves to a
  background session) makes the origin the parent of its continuation;
- a symbolic link inside a unit's `projects/<storage>/<id>/` directory that
  points into another session's `projects/<storage>/<other-id>/` directory
  makes the target the parent. Claude Code creates these when a background
  continuation reuses its origin's subagent transcripts.

Members are `root` or `continuation`. If the links do not form one tree
(several origins, conflicting parents, unreachable members), the group is
excluded with `ancestry-ambiguous`. A link to a session id that exists in
several projects excludes the linking session with `duplicate-session-id`.
Links to sessions that no longer exist are reported as `missing_links` and a
mutation blocker.

## Last activity

A unit's `last_activity` is the newest of:

- the transcript's modification time;
- the modification time of every file and directory in the unit (subagents,
  tool results, checkpoints, sidecars, links);
- the newest valid message timestamp in the transcript.

Taking the maximum means every imprecision errs toward keeping. Claude Code
exposes no "last used" value, and modification times also advance on writes
that are not use (title records, failed resumes, copies); all such
distortions make sessions look more recent. Claude Code's own retention
sweep uses the transcript modification time alone; that is reported
separately ([native-retention.md](native-retention.md)).

## Project identity

A unit's project identity is **verified** only when a working directory
recorded in its transcript (the first or the last one seen) encodes to
exactly the storage folder that holds the transcript. The last recorded
directory is preferred because `/cd` moves a session into the new directory's
storage. Otherwise identity is **unverified** (a recorded directory exists but
does not match) or **unknown** (no recorded directory).

In `current` scope, sessions found in the scope's storage folder whose
identity is unknown or unverified are listed as excluded
(`project-identity-unknown`, `project-identity-unverified`); sessions stored
there that verifiably belong to a different path with the same storage name
(`/home/a-b` vs. `/home/a/b`) are excluded as `project-path-collision`.
Paths longer than 200 encoded characters match the 200-character prefix plus
an alphanumeric hash suffix, then require the recorded directory. Sessions
stored under a name chosen with `CLAUDE_CODE_PROJECT_DIR_NAME` cannot be
verified and are excluded.

## Exclusion codes from storage problems

| Storage issue | Contract reason |
|---|---|
| Symbolic link leaving session data, symlinked transcript, special file, file owned by another user | `unsafe-storage` |
| Unreadable, empty, or unparseable transcript | `metadata-malformed` |
| Session id in several projects | `duplicate-session-id` |

## Claude Code–specific protections

| Reason | Signal |
|---|---|
| `invoking-session` | `CLAUDE_CODE_SESSION_ID` of the session running the tool |
| `running` | `sessions/<pid>.json` with the PID's start time matching `/proc/<pid>/stat` (Linux) |
| `possibly-running` | registry entry whose PID exists but cannot be verified (macOS, Windows, no start time) |
| `background-session` | a `jobs/*/state.json` record references the session |
| `recently-active` | any activity within `--min-idle` (default 1 hour) |

See [safety-model.md](safety-model.md) for why this detection is incomplete.
