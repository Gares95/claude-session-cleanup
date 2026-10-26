# JSON output contract

`--json` prints one document described by
[`schemas/preview-v1.json`](../schemas/preview-v1.json). The envelope is
agent-neutral so that a future unified Codex/Claude Code skill can merge
reports from several backends.

## Identification

```json
{
  "schema": "session-cleanup/preview",
  "schema_version": 1,
  "agent": "claude-code",
  "tool": {"name": "claude-session-cleanup", "version": "0.1.0"},
  "mode": "preview",
  "read_only": true
}
```

Consumers must check `schema` and `schema_version` before reading anything
else.

## Compatibility rules

- Within `schema_version` 1, fields may be **added** (including new enum
  values for `reasons`, `warnings[].code`, `mutation_blockers`, source kinds,
  and `links[].kind`). Consumers must ignore unknown fields and treat unknown
  reason codes as protective.
- Removing a field, changing its type, or changing its meaning requires
  `schema_version` 2.
- Timestamps are UTC, ISO 8601, second precision, with a `Z` suffix.
- Sizes are bytes. `bytes` may be `null` for a backend that cannot measure
  size (for example, a Codex backend).
- Ordering is deterministic: groups by status (`selected`, `protected`,
  `excluded`), then last activity (newest first), then group id.

## Top-level fields

| Field | Content |
|---|---|
| `generated_at` | The planning clock |
| `environment` | Configuration directory and how it was chosen; Claude Code versions seen in transcripts; versions this release was tested with |
| `policy` | Project scope and path, `older_than_seconds`, `keep_latest`, `min_idle_seconds`, `combination: "intersection"`, `ranking_unit: "root-session-group"`, `action` (always `null` in v0.1) |
| `totals` | Counts of groups, sessions, and bytes per status, and reason counts per status |
| `groups` | One entry per root session group (below) |
| `native_retention` | `observed`, `configured`, `estimates`, and a disclaimer ([native-retention.md](native-retention.md)) |
| `activity_detection` | Sources used, what was not used, and limitations; `complete` is false |
| `mutation` | `supported: false` and global `blockers` |
| `warnings` | `{code, path}` items about the configuration directory as a whole |

## Groups

```json
{
  "group_id": "<root session id>",
  "status": "selected | protected | excluded",
  "reasons": ["older-than", "outside-latest"],
  "rank": 7,
  "last_activity": "2026-09-10T12:00:00Z",
  "last_activity_evidence": {"session_id": "…", "source": "subagent-mtime"},
  "bytes": 1536,
  "session_count": 1,
  "missing_links": [],
  "native_retention": {"risk": "past-period | approaching | within-period"},
  "mutation_blockers": [],
  "sessions": [ … ]
}
```

`rank` is `null` for excluded groups. Reason codes are listed in
[retention-semantics.md](retention-semantics.md#decision-order).

## Sessions

Each group lists its members, root first (`role: "root"`, then
`"linked"`): session id, `title` (only with `--include-titles`) and
`title_source`, `project` (`storage_dir`, `identity`, verified `path`),
`created_at`, `last_activity` and its source, `transcript` (modification time
and size), total `bytes` and `bytes_by_kind`, `artifact_count`,
`subagent_count`, `entrypoints`, `claude_code_versions`, `links`
(`continued-in`, `shared-session-data`), `issues` (`{code, path}`, paths
relative to the configuration directory), `warnings`,
`unknown_associated_artifacts`, and `native_retention`
(`transcript_modified_at`, `risk`).

## Mapping to a unified backend

| Concept | Claude Code backend | Codex backend (expected) |
|---|---|---|
| Group | Root session plus subagents and linked continuations | Root thread plus spawned descendants |
| Last activity | Max over transcript/artifact mtimes and message timestamps | Max over root and descendant `recencyAt` |
| Reversible action | `quarantine` (tool-managed, future) | `archive` (native) |
| Irreversible action | not offered | `delete` |
| Size | Measured | `null` |

`policy.action` and the action vocabulary (`archive`, `quarantine`,
`delete`) are reserved so that a unified report never presents one backend's
guarantee as another's.
