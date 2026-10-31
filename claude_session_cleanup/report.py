"""Build the shared preview document (contract/SPEC.md section 4, schema v1).

The terminal renderer consumes this same structure, so the human and JSON
outputs cannot disagree. Core fields follow the cross-agent contract;
everything specific to Claude Code lives under ``extensions.claude_code``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import SCHEMA_NAME, SCHEMA_VERSION, TESTED_CLAUDE_CODE_VERSIONS, __version__
from . import activity as activity_module
from . import native_retention as native
from .activity import ActivitySignals
from .deletion import group_blockers, unit_targets
from .discovery import Discovery, SessionGroup, SessionUnit
from .policy import STATUS_ORDER, GroupDecision, Plan

STATUSES = tuple(STATUS_ORDER)
CLAUDE = "claude_code"

ACTIVITY_TIMESTAMP = {
    "source": "max(transcript-mtime, session-artifact-mtimes, message-timestamps)",
    "kind": "derived",
    "resolution": "nanosecond file modification times; millisecond message timestamps",
    "conservative": True,
}

# Capabilities that do not depend on the platform. Delete support is decided
# at runtime by capabilities() from the activity signals.
CAPABILITIES = {
    "actions": {
        "archive": {"supported": False, "reversibility": "agent-native", "requires_saved_plan": False,
                    "unsupported_reason": "not-offered-by-backend"},
        "quarantine": {"supported": False, "reversibility": "tool-managed", "requires_saved_plan": False,
                       "unsupported_reason": "not-offered-by-backend"},
        "delete": {"supported": False, "reversibility": "irreversible", "requires_saved_plan": True,
                   "unsupported_reason": "blocked-by-safety-requirements"},
    },
    "sizes": True,
    "titles": True,
    "pinning": False,
    "active_session_detection": "partial",
}


def capabilities(signals: ActivitySignals) -> dict[str, Any]:
    """Capabilities as they are on this machine now: delete needs the Linux process scan."""
    actions = {name: dict(entry) for name, entry in CAPABILITIES["actions"].items()}
    if not signals.mutation_blockers:
        actions["delete"].update(supported=True, unsupported_reason=None)
    return {**CAPABILITIES, "actions": actions}


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _version_key(version: str) -> tuple:
    return tuple((0, int(piece), "") if piece.isdigit() else (1, 0, piece) for piece in version.split("."))


def _notice(code: str, subject: str | None) -> dict[str, Any]:
    return {"code": code, "subject": subject}


def _session(
    unit: SessionUnit, group: SessionGroup, now: dt.datetime, retention, include_titles: bool
) -> dict[str, Any]:
    parent = group.parent_of(unit.session_id)
    return {
        "session_id": unit.session_id,
        "relation": "root" if unit.session_id == group.group_id else "continuation",
        "parent_session_id": parent,
        "title": unit.title if include_titles else None,
        "title_source": unit.title_source,
        "project_path": unit.project_path,
        "project_identity": "unknown" if unit.identity == "missing" else unit.identity,
        "created_at": iso(unit.created_at),
        "last_activity": iso(unit.last_activity),
        "last_activity_source": unit.last_activity_source,
        "bytes": unit.unit_bytes,
        "issues": [_notice(item.code, item.path) for item in unit.issues],
        "extensions": {
            CLAUDE: {
                "storage_dir": unit.project_dir_name,
                "transcript_modified_at": iso(unit.transcript_mtime),
                "transcript_bytes": unit.transcript_bytes,
                "bytes_by_kind": unit.bytes_by_kind(),
                "artifact_count": sum(1 for item in unit.artifacts if not item.is_dir),
                "subagent_count": unit.subagent_count,
                "entrypoints": list(unit.entrypoints),
                "claude_code_versions": list(unit.claude_versions),
                "links": [{"kind": "continued-in", "session_id": target} for target in unit.continued_in]
                + [{"kind": "shared-session-data", "session_id": target} for target in unit.shared_data_links],
                "unknown_associated_artifacts": list(unit.unknown_artifacts),
                "warnings": list(unit.warnings),
                "native_retention_risk": native.transcript_risk(unit.transcript_mtime, now, retention),
            }
        },
    }


def _group(
    decision: GroupDecision, now: dt.datetime, retention, include_titles: bool, config, requested_action: str | None
) -> dict[str, Any]:
    group = decision.group
    sessions = [_session(unit, group, now, retention, include_titles) for unit in group.members]
    risks = [s["extensions"][CLAUDE]["native_retention_risk"] for s in sessions]
    left_in_place = sorted(path for unit in group.members for path in unit_targets(unit)[1])
    return {
        "group_id": group.group_id,
        "status": decision.status,
        "reasons": list(decision.reasons),
        "rank": decision.rank,
        "proposed_action": requested_action if decision.status == "selected" else None,
        "last_activity": iso(group.last_activity),
        "last_activity_session_id": group.last_activity_session,
        "bytes": group.bytes,
        "session_count": len(group.members),
        "sessions": sessions,
        "mutation_blockers": group_blockers(group, config),
        "extensions": {
            CLAUDE: {
                "native_retention_risk": native.worst_risk(risks),
                "missing_links": list(group.missing_links),
                "left_in_place_on_delete": left_in_place,
            }
        },
    }


def _totals(groups: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        key: {"examined": 0, **{status: 0 for status in STATUSES}} for key in ("groups", "sessions", "bytes")
    }
    reasons: dict[str, dict[str, int]] = {status: {} for status in STATUSES}
    for group in groups:
        status = group["status"]
        for key, amount in (("groups", 1), ("sessions", group["session_count"]), ("bytes", group["bytes"])):
            totals[key]["examined"] += amount
            totals[key][status] += amount
        for reason in group["reasons"]:
            reasons[status][reason] = reasons[status].get(reason, 0) + 1
    totals["reasons"] = {status: dict(sorted(counts.items())) for status, counts in reasons.items()}
    return totals


def _native_section(retention: native.RetentionConfig, groups: list[dict[str, Any]], verbose_sources: bool) -> dict[str, Any]:
    by_risk = {level: 0 for level in native.RISK_LEVELS}
    kept_at_risk = selected_at_risk = 0
    for group in groups:
        if group["status"] == "excluded":
            continue
        risk = group["extensions"][CLAUDE]["native_retention_risk"]
        by_risk[risk] += 1
        if risk != "within-period":
            if group["status"] == "selected":
                selected_at_risk += 1
            else:
                kept_at_risk += 1
    sources = [
        {"kind": s.kind, "path": s.path, "status": s.status, "value_days": s.value}
        for s in retention.sources
        if verbose_sources or s.status != "missing"
    ]
    return {
        "disclaimer": native.DISCLAIMER,
        "observed": {
            "last_cleanup_marker": iso(retention.last_cleanup_marker),
            "last_cleanup_marker_status": retention.last_cleanup_marker_status,
            "basis": "transcript file modification time",
        },
        "configured": {
            "default_days": native.DEFAULT_PERIOD_DAYS,
            "managed_days": retention.managed_days,
            "user_days": retention.user_days,
            "applicable_days": list(retention.applicable_days),
            "lowest_applicable_days": retention.lowest_applicable_days,
            "sweep_may_pause": retention.sweep_may_pause,
            "sources": sources,
            "complete": False,
            "limitations": list(native.LIMITATIONS),
        },
        "estimates": {
            "approaching_window_days": native.APPROACHING_WINDOW_DAYS,
            "groups_by_risk": by_risk,
            "kept_groups_at_risk": kept_at_risk,
            "selected_groups_at_risk": selected_at_risk,
        },
    }


def _activity_section(signals: ActivitySignals, min_idle_seconds: int) -> dict[str, Any]:
    liveness: dict[str, int] = {}
    for entry in signals.registry:
        liveness[entry.liveness] = liveness.get(entry.liveness, 0) + 1
    return {
        "complete": False,
        "invoking_session_id": signals.invoking_session_id,
        "sources": [
            {"name": "session-registry", "entries": len(signals.registry),
             "liveness": dict(sorted(liveness.items())), "unreadable": list(signals.registry_unreadable)},
            {"name": "background-session-records", "records": signals.job_records_read,
             "session_ids": len(signals.background_session_ids), "unreadable": list(signals.job_records_unreadable)},
            {"name": "recent-modification-guard", "min_idle_seconds": min_idle_seconds},
            {"name": "claude-code-process-scan", "available": signals.process_scan.available,
             "processes": len(signals.relevant_processes()),
             "uninspectable_processes": signals.process_scan.uninspectable},
        ],
        "not_used": ["claude agents --json"],
        "limitations": list(activity_module.LIMITATIONS),
    }


def build_report(
    *,
    plan: Plan,
    discovery: Discovery,
    signals: ActivitySignals,
    retention: native.RetentionConfig,
    include_titles: bool,
    verbose_sources: bool = False,
    requested_action: str | None = None,
) -> dict[str, Any]:
    now = plan.now
    policy = plan.policy
    groups = [
        _group(decision, now, retention, include_titles, discovery.config, requested_action)
        for decision in plan.decisions
    ]
    versions = sorted({v for unit in discovery.units for v in unit.claude_versions}, key=_version_key)
    warnings = [_notice(item.code, item.path) for item in discovery.warnings]
    if versions and versions[-1] not in TESTED_CLAUDE_CODE_VERSIONS:
        warnings.append(_notice("claude-code-version-untested", versions[-1]))
    if signals.invoking_session_env_present and signals.invoking_session_id is None:
        warnings.append(_notice("invoking-session-id-unavailable", "CLAUDE_CODE_SESSION_ID"))
    if retention.sweep_may_pause:
        warnings.append(_notice("native-sweep-may-be-paused", None))
    min_idle_seconds = int(policy.min_idle.total_seconds())
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "agent": "claude-code",
        "agent_version": None,
        "tool": {"name": "claude-session-cleanup", "version": __version__},
        "mode": "preview",
        "changes_made": False,
        "evaluated_at": iso(now),
        "scope": {
            "mode": policy.project_mode,
            "path": policy.project_path if policy.project_mode == "current" else None,
        },
        "policy": {
            "older_than_seconds": int(policy.older_than.total_seconds()) if policy.older_than else None,
            "cutoff": iso(plan.cutoff),
            "keep_latest": policy.keep_latest,
            "min_idle_seconds": min_idle_seconds,
            "combination": "intersection",
            "ranking_unit": "root-group",
            "requested_action": requested_action,
        },
        "activity_timestamp": dict(ACTIVITY_TIMESTAMP),
        "capabilities": capabilities(signals),
        "totals": _totals(groups),
        "groups": groups,
        "warnings": sorted(warnings, key=lambda item: (item["code"], item["subject"] or "")),
        "extensions": {
            CLAUDE: {
                "config_dir": discovery.config.path,
                "config_dir_source": discovery.config.source,
                "invoking_directory": policy.project_path,
                "claude_code_versions_observed": versions,
                "tested_claude_code_versions": list(TESTED_CLAUDE_CODE_VERSIONS),
                "mutation_blockers": list(signals.mutation_blockers),
                "native_retention": _native_section(retention, groups, verbose_sources),
                "activity_detection": _activity_section(signals, min_idle_seconds),
            }
        },
    }
