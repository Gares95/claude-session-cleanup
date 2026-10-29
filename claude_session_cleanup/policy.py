"""Pure retention policy engine implementing contract/SPEC.md section 2. No I/O.

Retention is decided per *root group* (see discovery.py). In order:

1. **Scope.** ``current`` considers groups with a member whose verified
   project path equals the invoking directory exactly, plus groups stored in
   that directory's storage folder whose identity cannot be verified (shown
   as excluded so nothing is silently hidden). ``all`` considers every group.
2. **Exclusion.** A group is ``excluded`` (never ranked, never selected) if
   any member has unknown or unsafe metadata, its links do not form a single
   tree, or, in ``current`` scope, a member belongs to another project.
3. **Ranking.** All other groups are ranked by last activity, newest first,
   ties broken by group id. Each group counts once toward ``--keep-latest``.
   Protected groups occupy ranks; excluded groups do not, so unknown data can
   only cause more sessions to be kept.
4. **Status.** ``protected`` if a safety rule applies, else ``retained`` if
   the policy keeps it, else ``selected``.

A group's last activity is the newest evidence across all members and all
their artifacts, so a recent subagent, continuation, tool result, or
checkpoint keeps an older parent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Iterable, Mapping

from .config import paths_equal, project_dir_matches
from .discovery import SessionGroup, SessionUnit
from .errors import CleanupError

STATUS_ORDER = {"selected": 0, "retained": 1, "protected": 2, "excluded": 3}

# Reason codes in contract registry order (SPEC.md section 3.3).
REASON_ORDER = (
    "older-than",
    "outside-latest",
    "invoking-session",
    "pinned",
    "running",
    "possibly-running",
    "background-session",
    "ephemeral",
    "unsafe-runtime-state",
    "recently-active",
    "within-latest",
    "newer-than-cutoff",
    "related-outside-scope",
    "project-identity-unknown",
    "project-identity-unverified",
    "project-path-collision",
    "metadata-malformed",
    "ancestry-ambiguous",
    "duplicate-session-id",
    "unsafe-storage",
)
PROTECTION_REASONS = frozenset(REASON_ORDER[2:10])

# Unit issue codes (discovery) mapped to contract exclusion reasons.
ISSUE_REASONS = {
    "symlink-in-session": "unsafe-storage",
    "non-regular-file": "unsafe-storage",
    "foreign-owner": "unsafe-storage",
    "unreadable": "metadata-malformed",
    "empty-transcript": "metadata-malformed",
    "unparseable-transcript": "metadata-malformed",
    "duplicate-session-id": "duplicate-session-id",
}


def ordered_reasons(reasons: Iterable[str]) -> tuple[str, ...]:
    unique = set(reasons)
    known = [code for code in REASON_ORDER if code in unique]
    return tuple(known + sorted(unique - set(REASON_ORDER)))


@dataclasses.dataclass(frozen=True)
class Policy:
    older_than: dt.timedelta | None
    keep_latest: int | None
    project_mode: str  # "current" or "all"
    project_path: str  # canonical invoking directory
    min_idle: dt.timedelta


@dataclasses.dataclass(frozen=True)
class GroupDecision:
    group: SessionGroup
    status: str  # selected | retained | protected | excluded
    reasons: tuple[str, ...]
    rank: int | None


@dataclasses.dataclass(frozen=True)
class Plan:
    policy: Policy
    now: dt.datetime
    cutoff: dt.datetime | None
    decisions: tuple[GroupDecision, ...]

    def by_status(self, status: str) -> tuple[GroupDecision, ...]:
        return tuple(item for item in self.decisions if item.status == status)


def _member_exclusions(unit: SessionUnit, policy: Policy) -> list[str]:
    reasons = [ISSUE_REASONS[code] for code in unit.excluded_issue_codes if code in ISSUE_REASONS]
    if unit.identity == "missing":
        reasons.append("project-identity-unknown")
    elif unit.identity == "unverified":
        reasons.append("project-identity-unverified")
    elif policy.project_mode == "current" and unit.project_path is not None:
        if not paths_equal(unit.project_path, policy.project_path):
            if project_dir_matches(unit.project_dir_name, policy.project_path):
                reasons.append("project-path-collision")
            else:
                reasons.append("related-outside-scope")
    return reasons


def _in_scope(group: SessionGroup, policy: Policy) -> bool:
    if policy.project_mode == "all":
        return True
    for unit in group.members:
        if unit.identity == "verified" and paths_equal(unit.project_path or "", policy.project_path):
            return True
        if project_dir_matches(unit.project_dir_name, policy.project_path):
            return True
    return False


def build_plan(
    groups: Iterable[SessionGroup],
    policy: Policy,
    *,
    now: dt.datetime,
    invoking_session_id: str | None = None,
    session_protections: Mapping[str, str] | None = None,
) -> Plan:
    if policy.older_than is None and policy.keep_latest is None:
        raise CleanupError("at least one of --older-than or --keep-latest is required")
    if policy.keep_latest is not None and policy.keep_latest < 1:
        raise CleanupError("--keep-latest must be at least 1")
    if policy.project_mode not in ("current", "all"):
        raise CleanupError("project scope must be 'current' or 'all'")
    if now.tzinfo is None:
        raise CleanupError("the planning clock must be timezone-aware")
    protections = dict(session_protections or {})
    unknown_protections = set(protections.values()) - PROTECTION_REASONS
    if unknown_protections:
        raise CleanupError(f"unknown protection reasons: {sorted(unknown_protections)}")

    decisions: list[GroupDecision] = []
    rankable: list[SessionGroup] = []
    for group in groups:
        if not _in_scope(group, policy):
            continue
        reasons = [reason for unit in group.members for reason in _member_exclusions(unit, policy)]
        if group.ancestry_ambiguous:
            reasons.append("ancestry-ambiguous")
        if reasons:
            decisions.append(GroupDecision(group, "excluded", ordered_reasons(reasons), None))
        else:
            rankable.append(group)

    rankable.sort(key=lambda group: (-group.last_activity.timestamp(), group.group_id))
    cutoff = now - policy.older_than if policy.older_than is not None else None
    for rank, group in enumerate(rankable, start=1):
        member_ids = {unit.session_id for unit in group.members}
        protected: list[str] = []
        if invoking_session_id is not None and invoking_session_id in member_ids:
            protected.append("invoking-session")
        protected.extend(protections[i] for i in member_ids if i in protections)
        if now - group.last_activity < policy.min_idle:
            protected.append("recently-active")
        retained: list[str] = []
        if policy.keep_latest is not None and rank <= policy.keep_latest:
            retained.append("within-latest")
        if cutoff is not None and not group.last_activity < cutoff:
            retained.append("newer-than-cutoff")
        if protected:
            decisions.append(GroupDecision(group, "protected", ordered_reasons(protected + retained), rank))
        elif retained:
            decisions.append(GroupDecision(group, "retained", ordered_reasons(retained), rank))
        else:
            selected = []
            if cutoff is not None:
                selected.append("older-than")
            if policy.keep_latest is not None:
                selected.append("outside-latest")
            decisions.append(GroupDecision(group, "selected", tuple(selected), rank))

    decisions.sort(
        key=lambda item: (
            STATUS_ORDER[item.status],
            -item.group.last_activity.timestamp(),
            item.group.group_id,
            item.group.members[0].project_dir_name,
        )
    )
    return Plan(policy=policy, now=now, cutoff=cutoff, decisions=tuple(decisions))
