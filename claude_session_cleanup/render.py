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
    "pinned": "pinned",
    "running": "running",
    "possibly-running": "possibly running",
    "background-session": "background session record",
    "ephemeral": "ephemeral",
    "unsafe-runtime-state": "unsafe runtime state",
    "recently-active": "recently active",
    "within-latest": "within latest N",
    "newer-than-cutoff": "newer than cutoff",
    "related-outside-scope": "linked session in another project",
    "project-identity-unknown": "no recorded working directory",
    "project-identity-unverified": "project identity unverified",
    "project-path-collision": "different project with the same storage name",
    "metadata-malformed": "unreadable or malformed transcript",
    "ancestry-ambiguous": "ambiguous session links",
    "duplicate-session-id": "session id in several projects",
    "unsafe-storage": "unsafe session storage (link, special file, or other owner)",
}
STATUS_ROWS = (
    ("selected", "Would select"),
    ("retained", "Retained"),
    ("protected", "Protected"),
    ("excluded", "Excluded"),
)
BLOCKER_LABELS = {
    "unknown-associated-artifact": "unrecognized file named after the session elsewhere in the config directory",
    "linked-session-not-found": "a linked session is missing",
    "cross-session-symlink": "shares data with another session through links",
    "unrecognized-session-data": "unrecognized entry in the session's data",
    "storage-inside-project": "the configuration directory is inside the project",
    "active-session-detection-incomplete": "no process scan on this platform",
    "process-scan-incomplete": "some of your processes could not be inspected",
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
    now = _parse(report["evaluated_at"])
    claude = report["extensions"]["claude_code"]
    local = (lambda value: value.astimezone(tz)) if tz is not None else (lambda value: value.astimezone())
    policy = report["policy"]
    totals = report["totals"]
    action = policy["requested_action"]
    heading = "Claude Code session deletion preview (nothing is deleted)" if action == "delete" \
        else "Claude Code session retention preview (read-only)"
    lines: list[str] = [heading, ""]

    scope_info = report["scope"]
    scope = f"current — {_path(scope_info['path'])}" if scope_info["mode"] == "current" else "all projects"
    keep = policy["keep_latest"]
    lines += [
        f"Config:   {_path(claude['config_dir'])} ({claude['config_dir_source']})",
        f"Project:  {scope}",
        "Policy:   older than "
        + format_duration(policy["older_than_seconds"])
        + " · keep latest "
        + (f"{keep} root groups" if keep is not None else "not set")
        + " · min idle "
        + format_duration(policy["min_idle_seconds"])
        + (f" · action {action}" if action else ""),
        f"Clock:    {local(now).strftime('%Y-%m-%d %H:%M %Z').strip()}",
        "",
    ]

    groups, sessions, sizes = totals["groups"], totals["sessions"], totals["bytes"]
    lines.append(
        f"Examined {_plural(groups['examined'], 'group')} "
        f"({_plural(sessions['examined'], 'session')}, {format_bytes(sizes['examined'])})"
    )
    lines.append(f"  {'':<13} {'GROUPS':>6} {'SESSIONS':>9} {'SIZE':>10}")
    for status, label in STATUS_ROWS:
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
            if action and status == "selected" and group["mutation_blockers"]:
                lines.append("      cannot be deleted: "
                             + "; ".join(BLOCKER_LABELS.get(b, b) for b in group["mutation_blockers"]))
            if verbose:
                lines.append("      reasons: " + ", ".join(REASON_LABELS.get(r, r) for r in group["reasons"]))
                risk = group["extensions"]["claude_code"]["native_retention_risk"]
                if risk != "within-period":
                    lines.append("      native cleanup: " + RISK_LABELS[risk])
            for session in group["sessions"][1:]:
                lines.append(
                    f"      └ {session['relation']} {_session_id(session['session_id'], full_ids)}  "
                    f"{_title(session, show_titles)}".rstrip()
                )
            if verbose:
                for session in group["sessions"]:
                    extra = session["extensions"]["claude_code"]
                    kinds = ", ".join(f"{k} {format_bytes(v)}" for k, v in extra["bytes_by_kind"].items())
                    path = _path(session["project_path"] or f"(storage {extra['storage_dir']})")
                    lines.append(
                        f"      {_session_id(session['session_id'], full_ids)}: {path} · identity "
                        f"{session['project_identity']} · activity from {session['last_activity_source']}"
                    )
                    if kinds:
                        lines.append(f"        {kinds}")
                    for item in extra["unknown_associated_artifacts"]:
                        lines.append(f"        unknown associated artifact: {_path(item)}")
        lines.append("")

    table("selected", "Selected for permanent deletion (nothing is deleted until a reviewed plan is applied):"
          if action == "delete" else "Selected (eligible for cleanup; nothing is changed):")
    if verbose:
        table("retained", "Retained by the policy:")
        table("protected", "Protected by a safety rule:")
        table("excluded", "Excluded (unknown or unsafe metadata; never selected or ranked):")
    summaries = [(label, totals["reasons"][status]) for status, label in STATUS_ROWS[1:] if totals["reasons"][status]]
    for label, reasons in summaries:
        lines.append(f"{label}: {_reason_summary(reasons)}")
    if summaries:
        lines.append("")

    native = claude["native_retention"]
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
    kept_at_risk = estimates["kept_groups_at_risk"]
    if kept_at_risk:
        lines.append(
            f"  {_plural(kept_at_risk, 'group')} this policy keeps "
            f"{'is' if kept_at_risk == 1 else 'are'} past or near that period; "
            f"Claude Code may delete {'it' if kept_at_risk == 1 else 'them'} anyway."
        )
    lines.append("  " + native["disclaimer"])
    lines.append("")

    for warning in report["warnings"]:
        subject = f": {_path(warning['subject'])}" if warning["subject"] else ""
        lines.append(f"Warning: {warning['code']}{subject}")
    if report["warnings"]:
        lines.append("")
    delete = report["capabilities"]["actions"]["delete"]
    if delete["supported"]:
        lines.append("Deletion: available here, only through a reviewed saved plan "
                     "(--action delete --save-plan PATH, then --apply-plan PATH).")
    else:
        blockers = claude["mutation_blockers"]
        lines.append("Deletion: unavailable here ("
                     + "; ".join(BLOCKER_LABELS.get(b, b) for b in blockers) + "); previews only.")
    lines.append("Active-session detection is partial; see docs/safety-model.md.")
    lines.append("No changes made.")
    return "\n".join(lines) + "\n"


def render_apply(
    document: dict[str, Any], *, deleted: list[str], remaining: list[str], error: str | None, full_ids: bool = False
) -> str:
    """Summary of an applied (or stopped) deletion plan."""
    policy = document["policy"]
    scope = f"current — {_path(policy['project_path'])}" if policy["project_mode"] == "current" else "all projects"
    done = set(deleted)
    lines = [
        "Claude Code session deletion (permanent)",
        "",
        f"Plan:     {document['plan_id'][:8]} saved {document['created_at'][:19].replace('T', ' ')} UTC",
        f"Project:  {scope}",
        "",
    ]
    for group in document["groups"]:
        paths = [t["path"] for t in group["targets"]]
        removed = sum(1 for path in paths if path in done)
        state = "deleted" if removed == len(paths) else ("partly deleted" if removed else "not deleted")
        lines.append(
            f"  {_session_id(group['group_id'], full_ids):<8}  {state:<14} "
            f"{_plural(len(group['members']), 'session')}, {removed}/{len(paths)} files and directories"
        )
    left = sorted({path for group in document["groups"] for path in group["left_in_place"]})
    if left:
        lines += ["", "Left in place (not attributable to a single session):"]
        lines += [f"  {_path(path)}" for path in left]
    lines.append("")
    if error is None and not remaining:
        lines.append(f"Deleted {len(deleted)} files and directories in {_plural(len(document['groups']), 'group')}.")
    else:
        lines.append(f"STOPPED: {_path(error or 'some targets still exist')}")
        lines.append(f"Deleted {len(deleted)}; not deleted {len(remaining)}:")
        lines += [f"  {_path(path)}" for path in remaining[:50]]
        if len(remaining) > 50:
            lines.append(f"  … and {len(remaining) - 50} more")
        lines.append("Preview again before deciding what to do with the remaining data.")
    return "\n".join(lines) + "\n"
