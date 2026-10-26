"""Pure retention policy engine. No I/O.

Retention is decided per *root session group* (see discovery.py). The rules,
in order:

1. **Scope.** ``current`` considers groups with a member whose verified
   project path equals the invoking directory exactly, plus groups stored in
   that directory's storage folder whose identity cannot be verified (shown
   as excluded so nothing is silently hidden). ``all`` considers every group.
2. **Exclusion.** A group is excluded, never selected and never ranked, if
   any member has unknown or unsafe metadata (missing or unverified project
   identity, symlinks, unreadable or empty transcripts, foreign ownership,
   duplicate ids) or, in ``current`` scope, a member belongs to another
   project.
3. **Ranking.** Remaining groups are ranked by last activity, newest first,
   ties broken by group id. Each group counts once toward ``--keep-latest``
   however many sessions it contains. Protected groups occupy ranks like any
   other; excluded groups do not, so excluded data can only cause more
   sessions to be kept, never fewer.
4. **Selection.** A ranked group is selected only if it is older than the
   cutoff (when ``--older-than`` is set) *and* ranked outside the latest N
   (when ``--keep-latest`` is set) *and* has no protection.

A group's last activity is the newest evidence across all members and all
their artifacts, so a recent subagent, continuation, tool result, or
checkpoint protects an older parent.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Iterable, Mapping

from .config import paths_equal, project_dir_matches
from .discovery import SessionGroup, SessionUnit
from .errors import CleanupError

STATUS_ORDER = {"selected": 0, "protected": 1, "excluded": 2}


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
    status: str  # selected | protected | excluded
    reasons: tuple[str, ...]
    rank: int | None


@dataclasses.dataclass(frozen=True)
class Plan:
    policy: Policy
    now: dt.datetime
    decisions: tuple[GroupDecision, ...]

    def by_status(self, status: str) -> tuple[GroupDecision, ...]:
        return tuple(item for item in self.decisions if item.status == status)


def _member_exclusions(unit: SessionUnit, policy: Policy) -> list[str]:
    reasons = list(unit.excluded_issue_codes)
    if unit.identity == "missing":
        reasons.append("missing-cwd-evidence")
    elif unit.identity == "unverified":
        reasons.append("project-identity-unverified")
    elif policy.project_mode == "current":
        assert unit.project_path is not None
        if not paths_equal(unit.project_path, policy.project_path):
            if project_dir_matches(unit.project_dir_name, policy.project_path):
                reasons.append("project-path-collision")
            else:
                reasons.append("related-session-outside-project")
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

    decisions: list[GroupDecision] = []
    rankable: list[SessionGroup] = []
    for group in groups:
        if not _in_scope(group, policy):
            continue
        reasons = sorted({reason for unit in group.members for reason in _member_exclusions(unit, policy)})
        if reasons:
            decisions.append(GroupDecision(group, "excluded", tuple(reasons), None))
        else:
            rankable.append(group)

    rankable.sort(key=lambda group: (-group.last_activity.timestamp(), group.group_id))
    cutoff = now - policy.older_than if policy.older_than is not None else None
    for index, group in enumerate(rankable, start=1):
        member_ids = {unit.session_id for unit in group.members}
        protected: list[str] = []
        if invoking_session_id is not None and invoking_session_id in member_ids:
            protected.append("invoking-session")
        protected.extend(sorted({protections[i] for i in member_ids if i in protections}))
        if now - group.last_activity < policy.min_idle:
            protected.append("recently-active")
        if policy.keep_latest is not None and index <= policy.keep_latest:
            protected.append("within-latest")
        if cutoff is not None and not group.last_activity < cutoff:
            protected.append("newer-than-cutoff")
        if protected:
            decisions.append(GroupDecision(group, "protected", tuple(protected), index))
            continue
        selected = []
        if cutoff is not None:
            selected.append("older-than")
        if policy.keep_latest is not None:
            selected.append("outside-latest")
        decisions.append(GroupDecision(group, "selected", tuple(selected), index))

    decisions.sort(
        key=lambda item: (
            STATUS_ORDER[item.status],
            -item.group.last_activity.timestamp(),
            item.group.group_id,
            item.group.members[0].project_dir_name,
        )
    )
    return Plan(policy=policy, now=now, decisions=tuple(decisions))
