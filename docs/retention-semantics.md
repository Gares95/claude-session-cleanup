# Retention semantics

This document defines exactly how `claude-session-cleanup` decides what a
policy selects. The implementation is `claude_session_cleanup/policy.py`; the
agent-neutral test vectors in `tests/vectors/retention/` pin the behaviour.

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
contains the session id is reported as an *unknown associated artifact*. It
does not change selection, but it is a mutation blocker: a future cleanup
action could not account for it.

## Root session groups

Units are joined into **groups** by links Claude Code itself records:

- a `continued-in` transcript record (written when a conversation moves to a
  background session) links the origin to its continuation;
- a symbolic link inside a unit's `projects/<storage>/<id>/` directory that
  points into another session's `projects/<storage>/<other-id>/` directory.
  Claude Code creates these when a background continuation reuses its
  origin's subagent transcripts. Removing the origin would leave the
  continuation's links dangling, so both belong to one group.

The **root** is the member no link marks as a successor (the origin); ties
fall back to the smallest session id. Links to sessions that no longer exist
are reported as `missing_links` and as a mutation blocker.

Subagents are not separate sessions: they live inside their parent's unit.

## Last activity

A unit's last activity is the newest of:

- the transcript's modification time;
- the modification time of every file and directory in the unit (subagents,
  tool results, checkpoints, sidecars, links);
- the newest valid message timestamp in the transcript.

A group's last activity is the newest of its members'. Taking the maximum
means every imprecision errs toward keeping: an old parent with a subagent
written minutes ago is recent. This is deliberately different from Claude
Code's own cleanup, which looks only at the top-level transcript's
modification time.

The report names the evidence that won (`last_activity_source`, for example
`subagent-mtime` or `content-timestamp`).

## Scope

- `--project current` (default) resolves the invoking directory with
  `realpath` and Unicode NFC normalization. A group is in scope if any member
  is verified to belong to exactly that path, or is stored in the storage
  folder that path encodes to.
- `--project all` considers every group.

A unit's project identity is **verified** only when a working directory
recorded in its transcript (the first or the last one seen) encodes to
exactly the storage folder that holds the transcript. The last recorded
directory is preferred because `/cd` moves a session into the new directory's
storage. Otherwise identity is **unverified** (a recorded directory exists but
does not match) or **missing** (no recorded directory).

Consequences:

- Parent directories, child directories, and sibling git worktrees never
  match: each has its own storage folder.
- A repository moved to a new path does not match its old sessions.
- Two paths with the same storage name (`/home/a-b` and `/home/a/b`) are told
  apart by the recorded directory; the other project's sessions are excluded
  with `project-path-collision`.
- Paths longer than 200 encoded characters match the 200-character prefix
  plus an alphanumeric hash suffix, then require the recorded directory.
- Sessions stored under a name chosen with `CLAUDE_CODE_PROJECT_DIR_NAME`
  cannot be verified and are excluded.

## Decision order

1. **Exclusion.** A group is `excluded`, never ranked and never selected, if
   any member has: `missing-cwd-evidence`, `project-identity-unverified`,
   `symlink-in-session` (a link that leaves session data, or a symlinked
   transcript), `non-regular-file`, `foreign-owner`, `unreadable`,
   `empty-transcript`, `unparseable-transcript`, or `duplicate-session-id`;
   or, in `current` scope, `project-path-collision` or
   `related-session-outside-project`.
2. **Ranking.** All other in-scope groups are sorted by last activity, newest
   first, then by group id ascending. Ranks start at 1.
3. **Protection.** A ranked group is `protected` if any of these applies, and
   all that apply are listed in this order:
   `invoking-session`, `registered-running`, `registered-possibly-running`,
   `background-session-record`, `recently-active` (activity within
   `--min-idle`, default 1 hour), `within-latest` (rank ≤ N),
   `newer-than-cutoff` (last activity not strictly older than now minus
   `--older-than`).
4. **Selection.** Every other ranked group is `selected`, with reasons
   `older-than` and/or `outside-latest` for the selectors in use.

## Latest-N semantics

- N counts **root session groups**, not individual sessions. A group with an
  origin and a background continuation counts once.
- Groups are ranked by group last activity (see above), newest first; equal
  times are ordered by group id ascending, so results are deterministic.
- Protected groups (this session, running sessions, recently active)
  **occupy** ranks, matching `codex-session-cleanup`: "keep my latest 5"
  keeps the five most recent groups, whatever else protects them.
- Excluded groups **do not** occupy ranks. Their recency is not trusted, and
  leaving them out can only shift slots to older groups, which keeps more,
  never less.
- With both selectors, a group is selected only if it is older than the
  cutoff **and** ranked outside the latest N (intersection).

## Determinism

Directory listings are sorted, groups and reasons are emitted in a fixed
order, and the planning clock is captured once per run. Two runs over
unchanged data with the same clock produce byte-identical JSON.
