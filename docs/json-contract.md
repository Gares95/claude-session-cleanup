# JSON output

`--json` prints the shared preview document defined by the cross-agent
contract: [`contract/SPEC.md`](../contract/SPEC.md) §4 and
[`contract/preview-v1.schema.json`](../contract/preview-v1.schema.json)
(`schema: "session-cleanup/preview"`, `schema_version: 1`), shared with
`codex-session-cleanup`; read the contract for field definitions, ordering,
nullability, and the compatibility policy. A preview is never an
authorization to delete: deletion needs a saved plan, a separate private
format described in [safety-model.md](safety-model.md#saved-plans).

This page lists only what is specific to the Claude Code backend.

## Core field values

| Field | Value from this tool |
|---|---|
| `agent` | `"claude-code"` |
| `agent_version` | `null`: the tool never runs Claude Code. Versions seen in transcripts are under `extensions.claude_code` |
| `changes_made` | always `false` (previews never change anything) |
| `policy.requested_action` | `"delete"` with `--action delete`, otherwise `null` |
| `groups[].proposed_action` | `"delete"` for selected groups when delete was requested, otherwise `null` |
| `policy.min_idle_seconds` | the `--min-idle` guard |
| `activity_timestamp` | `kind: "derived"`, `conservative: true`, source `max(transcript-mtime, session-artifact-mtimes, message-timestamps)` |
| `capabilities.actions` | `delete`: `supported: true`, `requires_saved_plan: true`, `reversibility: "irreversible"` on Linux with the process scan available; elsewhere `supported: false` with `unsupported_reason: "blocked-by-safety-requirements"`. `archive` and `quarantine`: unsupported (`not-offered-by-backend`) |
| `capabilities` | `sizes: true`, `titles: true` (opt-in), `pinning: false`, `active_session_detection: "partial"` |
| `sessions[].relation` | `root` or `continuation` (subagents are data inside a session, not members) |
| `sessions[].title` | `null` unless `--include-titles` |
| `sessions[].title_source` | `custom-title`, `custom-title-sidecar`, `ai-title`, `first-prompt`, `last-prompt` |
| `sessions[].last_activity_source` | which evidence won, e.g. `transcript-mtime`, `subagent-mtime`, `content-timestamp` |
| `sessions[].issues` | `{code, subject}`; `subject` is a path relative to the configuration directory |

## `extensions.claude_code`

Top level:

| Field | Content |
|---|---|
| `config_dir`, `config_dir_source` | Configuration directory and how it was chosen |
| `invoking_directory` | Canonical invoking directory |
| `claude_code_versions_observed` | Versions recorded in transcripts |
| `tested_claude_code_versions` | Versions this release was checked against |
| `mutation_blockers` | Why deletion is unavailable on this machine: `active-session-detection-incomplete` (no process scan on this platform) or `process-scan-incomplete` (a process could not be inspected); empty when deletion is available |
| `native_retention` | Claude Code's own `cleanupPeriodDays` sweep: `observed`, `configured`, `estimates`, `disclaimer` ([native-retention.md](native-retention.md)) |
| `activity_detection` | Signals used (including `claude-code-process-scan`), signals deliberately not used, limitations |

Per group: `native_retention_risk` (worst member), `missing_links`,
`left_in_place_on_delete` (per-session data a deletion would leave, such as
`tasks/<id>`).

Per session: `storage_dir`, `transcript_modified_at`, `transcript_bytes`,
`bytes_by_kind`, `artifact_count`, `subagent_count`, `entrypoints`,
`claude_code_versions`, `links` (`continued-in`, `shared-session-data`),
`unknown_associated_artifacts`, `warnings`, `native_retention_risk`.

## Group mutation blockers

`unknown-associated-artifact`, `linked-session-not-found`,
`cross-session-symlink`, `unrecognized-session-data`,
`storage-inside-project`. They never change a status: a selected group with
a blocker is reported, listed in a saved plan as blocked, and never deleted
([safety-model.md](safety-model.md#what-deletion-removes)).
