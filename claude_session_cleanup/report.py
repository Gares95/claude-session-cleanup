"""Build the versioned preview report (``session-cleanup/preview`` v1).

The terminal renderer consumes this same structure, so the human and JSON
outputs cannot disagree. The envelope is agent-neutral (``agent`` names the
backend) so a future unified Codex/Claude skill can merge reports; see
docs/json-contract.md for compatibility rules.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import SCHEMA_NAME, SCHEMA_VERSION, TESTED_CLAUDE_CODE_VERSIONS, __version__
from . import activity as activity_module
from . import native_retention as native
from .activity import ActivitySignals
from .discovery import Discovery, SessionGroup, SessionUnit
from .policy import GroupDecision, Plan

GLOBAL_MUTATION_BLOCKERS = (
    "not-implemented-in-this-version",
    "no-supported-claude-code-session-mutation-interface",
    activity_module.MUTATION_BLOCKER,
)


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _version_key(version: str) -> tuple:
    parts = []
    for piece in version.split("."):
        parts.append((0, int(piece), "") if piece.isdigit() else (1, 0, piece))
    return tuple(parts)


def _session(unit: SessionUnit, role: str, now: dt.datetime, retention, include_titles: bool) -> dict[str, Any]:
    return {
        "session_id": unit.session_id,
        "role": role,
        "title": unit.title if include_titles else None,
        "title_source": unit.title_source,
        "project": {
            "storage_dir": unit.project_dir_name,
            "identity": unit.identity,
            "path": unit.project_path,
        },
        "created_at": iso(unit.created_at),
        "last_activity": iso(unit.last_activity),
        "last_activity_source": unit.last_activity_source,
        "transcript": {"modified_at": iso(unit.transcript_mtime), "bytes": unit.transcript_bytes},
        "bytes": unit.unit_bytes,
        "bytes_by_kind": unit.bytes_by_kind(),
        "artifact_count": sum(1 for item in unit.artifacts if not item.is_dir),
        "subagent_count": unit.subagent_count,
        "entrypoints": list(unit.entrypoints),
        "claude_code_versions": list(unit.claude_versions),
        "links": [{"kind": "continued-in", "session_id": target} for target in unit.continued_in]
        + [{"kind": "shared-session-data", "session_id": target} for target in unit.shared_data_links],
        "issues": [{"code": item.code, "path": item.path} for item in unit.issues],
        "warnings": list(unit.warnings),
        "unknown_associated_artifacts": list(unit.unknown_artifacts),
        "native_retention": {
            "transcript_modified_at": iso(unit.transcript_mtime),
            "risk": native.transcript_risk(unit.transcript_mtime, now, retention),
        },
    }


def _group_blockers(group: SessionGroup, status: str) -> list[str]:
    blockers = set()
    if status == "excluded":
        blockers.add("excluded-from-policy")
    if any(unit.unknown_artifacts for unit in group.members):
        blockers.add("unknown-associated-artifact")
    if group.missing_links:
        blockers.add("linked-session-not-found")
    if any(unit.shared_data_links for unit in group.members):
        blockers.add("cross-session-symlink")
    return sorted(blockers)


def _group(decision: GroupDecision, now: dt.datetime, retention, include_titles: bool) -> dict[str, Any]:
    group = decision.group
    sessions = [
        _session(unit, "root" if index == 0 else "linked", now, retention, include_titles)
        for index, unit in enumerate(group.members)
    ]
    newest = next(unit for unit in group.members if unit.session_id == group.last_activity_session)
    return {
        "group_id": group.group_id,
        "status": decision.status,
        "reasons": list(decision.reasons),
        "rank": decision.rank,
        "last_activity": iso(group.last_activity),
        "last_activity_evidence": {
            "session_id": newest.session_id,
            "source": newest.last_activity_source,
        },
        "bytes": group.bytes,
        "session_count": len(group.members),
        "missing_links": list(group.missing_links),
        "native_retention": {
            "risk": native.worst_risk([s["native_retention"]["risk"] for s in sessions]),
        },
        "mutation_blockers": _group_blockers(group, decision.status),
        "sessions": sessions,
    }


def _counts(groups: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "groups": {"examined": len(groups), "selected": 0, "protected": 0, "excluded": 0},
        "sessions": {"examined": 0, "selected": 0, "protected": 0, "excluded": 0},
        "bytes": {"examined": 0, "selected": 0, "protected": 0, "excluded": 0},
        "reasons": {"selected": {}, "protected": {}, "excluded": {}},
    }
    for group in groups:
        status = group["status"]
        totals["groups"][status] += 1
        for key, amount in (("sessions", group["session_count"]), ("bytes", group["bytes"])):
            totals[key]["examined"] += amount
            totals[key][status] += amount
        for reason in group["reasons"]:
            bucket = totals["reasons"][status]
            bucket[reason] = bucket.get(reason, 0) + 1
    for bucket in totals["reasons"].values():
        ordered = dict(sorted(bucket.items()))
        bucket.clear()
        bucket.update(ordered)
    return totals


def _native_section(retention: native.RetentionConfig, groups: list[dict[str, Any]], verbose_sources: bool) -> dict[str, Any]:
    by_risk = {level: 0 for level in native.RISK_LEVELS}
    protected_at_risk = selected_at_risk = 0
    for group in groups:
        if group["status"] == "excluded":
            continue
        risk = group["native_retention"]["risk"]
        by_risk[risk] += 1
        if risk != "within-period":
            if group["status"] == "protected":
                protected_at_risk += 1
            else:
                selected_at_risk += 1
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
            "protected_groups_at_risk": protected_at_risk,
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
            {
                "name": "session-registry",
                "entries": len(signals.registry),
                "liveness": dict(sorted(liveness.items())),
                "unreadable": list(signals.registry_unreadable),
            },
            {
                "name": "background-session-records",
                "records": signals.job_records_read,
                "session_ids": len(signals.background_session_ids),
                "unreadable": list(signals.job_records_unreadable),
            },
            {"name": "recent-modification-guard", "min_idle_seconds": min_idle_seconds},
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
) -> dict[str, Any]:
    now = plan.now
    groups = [_group(decision, now, retention, include_titles) for decision in plan.decisions]
    versions = sorted({v for unit in discovery.units for v in unit.claude_versions}, key=_version_key)
    newest_version = versions[-1] if versions else None
    warnings = [{"code": item.code, "path": item.path} for item in discovery.warnings]
    if newest_version is not None and newest_version not in TESTED_CLAUDE_CODE_VERSIONS:
        warnings.append({"code": "claude-code-version-untested", "path": newest_version})
    if signals.invoking_session_env_present and signals.invoking_session_id is None:
        warnings.append({"code": "invoking-session-id-unavailable", "path": "CLAUDE_CODE_SESSION_ID"})
    if retention.sweep_may_pause:
        warnings.append({"code": "native-sweep-may-be-paused", "path": "settings"})
    policy = plan.policy
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "agent": "claude-code",
        "tool": {"name": "claude-session-cleanup", "version": __version__},
        "mode": "preview",
        "read_only": True,
        "generated_at": iso(now),
        "environment": {
            "config_dir": discovery.config.path,
            "config_dir_source": discovery.config.source,
            "claude_code_versions_observed": versions,
            "tested_claude_code_versions": list(TESTED_CLAUDE_CODE_VERSIONS),
        },
        "policy": {
            "project": {"mode": policy.project_mode, "path": policy.project_path},
            "older_than_seconds": int(policy.older_than.total_seconds()) if policy.older_than else None,
            "keep_latest": policy.keep_latest,
            "min_idle_seconds": int(policy.min_idle.total_seconds()),
            "combination": "intersection",
            "ranking_unit": "root-session-group",
            "action": None,
        },
        "totals": _counts(groups),
        "groups": groups,
        "native_retention": _native_section(retention, groups, verbose_sources),
        "activity_detection": _activity_section(signals, int(policy.min_idle.total_seconds())),
        "mutation": {"supported": False, "blockers": list(GLOBAL_MUTATION_BLOCKERS)},
        "warnings": sorted(warnings, key=lambda item: (item["code"], item["path"])),
    }
