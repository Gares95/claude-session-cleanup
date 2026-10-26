"""Human-readable rendering of a preview report, and JSON serialization."""

from __future__ import annotations

import datetime as dt
import json
import unicodedata
from typing import Any

REASON_LABELS = {
    "older-than": "older than cutoff",
    "outside-latest": "outside latest N",
    "invoking-session": "this session",
    "registered-running": "running (registry)",
    "registered-possibly-running": "possibly running (registry)",
    "background-session-record": "background session record",
    "recently-active": "recently active",
    "within-latest": "within latest N",
    "newer-than-cutoff": "newer than cutoff",
    "missing-cwd-evidence": "no recorded working directory",
    "project-identity-unverified": "project identity unverified",
    "project-path-collision": "different project with the same storage name",
    "related-session-outside-project": "linked session in another project",
    "symlink-in-session": "symbolic link in session data",
    "non-regular-file": "special file in session data",
    "foreign-owner": "owned by another user",
    "unreadable": "unreadable",
    "empty-transcript": "empty transcript",
    "unparseable-transcript": "unparseable transcript",
    "duplicate-session-id": "session id in several projects",
}
RISK_LABELS = {
    "past-period": "older than the lowest configured period",
    "approaching": "within 7 days of the lowest configured period",
}


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"


def safe_text(value: str, limit: int) -> str:
    """Strip control and formatting characters (terminal-injection safe) and truncate."""
    cleaned = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch
        for ch in value
        if unicodedata.category(ch) not in ("Cf", "Co", "Cs", "Cn")
    )
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _path(value: str) -> str:
    return safe_text(value, 500)


def format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "not set"
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    hours = seconds // 3600
    return f"{hours} hour{'s' if hours != 1 else ''}"


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _age(now: dt.datetime, then: dt.datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return "<1h"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _reason_summary(reasons: dict[str, int]) -> str:
    return " · ".join(f"{REASON_LABELS.get(key, key)}: {value}" for key, value in reasons.items())


def _session_id(value: str, full_ids: bool) -> str:
    return value if full_ids else value[:8]


def _title(session: dict[str, Any], show_titles: bool) -> str:
    if not show_titles:
        return ""
    title = session.get("title")
    return safe_text(title, 60) if title else "(untitled)"


def render_text(
    report: dict[str, Any],
    *,
    tz: dt.tzinfo | None = None,
    verbose: bool = False,
    full_ids: bool = False,
    show_titles: bool = True,
) -> str:
    now = _parse(report["generated_at"])
    local = (lambda value: value.astimezone(tz)) if tz is not None else (lambda value: value.astimezone())
    policy = report["policy"]
    totals = report["totals"]
    lines: list[str] = ["Claude Code session retention preview (read-only)", ""]

    project = policy["project"]
    scope = f"current — {_path(project['path'])}" if project["mode"] == "current" else "all projects"
    keep = policy["keep_latest"]
    lines += [
        f"Config:   {_path(report['environment']['config_dir'])} ({report['environment']['config_dir_source']})",
        f"Project:  {scope}",
        "Policy:   older than "
        + format_duration(policy["older_than_seconds"])
        + " · keep latest "
        + (f"{keep} session groups" if keep is not None else "not set")
        + " · min idle "
        + format_duration(policy["min_idle_seconds"]),
        f"Clock:    {local(now).strftime('%Y-%m-%d %H:%M %Z').strip()}",
        "",
    ]

    groups, sessions, sizes = totals["groups"], totals["sessions"], totals["bytes"]
    lines.append(
        f"Examined {_plural(groups['examined'], 'group')} "
        f"({_plural(sessions['examined'], 'session')}, {format_bytes(sizes['examined'])})"
    )
    lines.append(f"  {'':<13} {'GROUPS':>6} {'SESSIONS':>9} {'SIZE':>10}")
    for status, label in (("selected", "Would select"), ("protected", "Protected"), ("excluded", "Excluded")):
        lines.append(
            f"  {label:<13} {groups[status]:>6} {sessions[status]:>9} {format_bytes(sizes[status]):>10}"
        )
    lines.append("")

    def table(status: str, heading: str) -> None:
        rows = [group for group in report["groups"] if group["status"] == status]
        if not rows:
            return
        lines.append(heading)
        lines.append("  LAST ACTIVITY     AGE    SIZE      SESSION   " + ("TITLE" if show_titles else ""))
        for group in rows:
            root = group["sessions"][0]
            last = _parse(group["last_activity"])
            lines.append(
                f"  {local(last).strftime('%Y-%m-%d %H:%M')}  {_age(now, last):<5}  "
                f"{format_bytes(group['bytes']):>8}  {_session_id(group['group_id'], full_ids):<8}  "
                f"{_title(root, show_titles)}".rstrip()
            )
            if verbose:
                lines.append("      reasons: " + ", ".join(REASON_LABELS.get(r, r) for r in group["reasons"]))
                if group["native_retention"]["risk"] != "within-period":
                    lines.append("      native cleanup: " + RISK_LABELS[group["native_retention"]["risk"]])
            for session in group["sessions"][1:]:
                lines.append(
                    f"      └ linked {_session_id(session['session_id'], full_ids)}  {_title(session, show_titles)}".rstrip()
                )
            if verbose:
                for session in group["sessions"]:
                    kinds = ", ".join(f"{k} {format_bytes(v)}" for k, v in session["bytes_by_kind"].items())
                    path = _path(session["project"]["path"] or f"(storage {session['project']['storage_dir']})")
                    lines.append(
                        f"      {_session_id(session['session_id'], full_ids)}: {path} · identity "
                        f"{session['project']['identity']} · activity from {session['last_activity_source']}"
                    )
                    if kinds:
                        lines.append(f"        {kinds}")
                    for item in session["unknown_associated_artifacts"]:
                        lines.append(f"        unknown associated artifact: {_path(item)}")
        lines.append("")

    table("selected", "Selected (eligible for a future cleanup action; nothing is changed):")
    if verbose:
        table("protected", "Protected:")
        table("excluded", "Excluded (unknown or unsafe metadata; never selected):")
    for status, label in (("protected", "Protected"), ("excluded", "Excluded")):
        if totals["reasons"][status]:
            lines.append(f"{label}: {_reason_summary(totals['reasons'][status])}")
    if totals["reasons"]["protected"] or totals["reasons"]["excluded"]:
        lines.append("")

    native = report["native_retention"]
    configured = native["configured"]
    lines.append("Claude Code's own cleanup (estimate; this tool does not control it):")
    if configured["managed_days"] is not None:
        lines.append(f"  Configured: managed settings set {configured['managed_days']} days")
    else:
        user = configured["user_days"]
        lines.append(
            "  Configured: user settings "
            + (f"{user} days" if user is not None else f"not set (default {configured['default_days']} days)")
        )
        for source in configured["sources"]:
            if source["kind"].startswith("project") and source["status"] == "set":
                lines.append(f"              {_path(source['path'])}: {source['value_days']} days")
    lines.append(f"  Lowest applicable period found: {configured['lowest_applicable_days']} days")
    for source in configured["sources"]:
        if source["status"] in ("invalid", "unreadable"):
            lines.append(f"  Warning: {_path(source['path'])} is {source['status']}; Claude Code may pause its cleanup")
    marker = native["observed"]["last_cleanup_marker"]
    if marker:
        lines.append(f"  Last cleanup marker (observed): {local(_parse(marker)).strftime('%Y-%m-%d %H:%M')}")
    estimates = native["estimates"]
    risk = estimates["groups_by_risk"]
    lines.append(
        f"  Transcripts last modified longer ago than that period: {_plural(risk['past-period'], 'group')}; "
        f"within {estimates['approaching_window_days']} days of it: {_plural(risk['approaching'], 'group')}"
    )
    if estimates["protected_groups_at_risk"]:
        lines.append(
            f"  {_plural(estimates['protected_groups_at_risk'], 'group')} this policy keeps "
            f"{'is' if estimates['protected_groups_at_risk'] == 1 else 'are'} past or near that period; "
            "Claude Code may delete "
            f"{'it' if estimates['protected_groups_at_risk'] == 1 else 'them'} anyway."
        )
    lines.append("  " + native["disclaimer"])
    lines.append("")

    for warning in report["warnings"]:
        lines.append(f"Warning: {warning['code']}: {_path(warning['path'])}")
    if report["warnings"]:
        lines.append("")
    lines.append(
        "Active-session detection is incomplete (registry and file activity only); "
        "cleanup actions are not available in this version."
    )
    lines.append("No changes made.")
    return "\n".join(lines) + "\n"
