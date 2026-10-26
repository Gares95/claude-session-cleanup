"""Command-line interface. Every invocation is a read-only preview."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from typing import Mapping, Sequence, TextIO

from . import __version__
from .activity import detect_activity
from .config import canonical_project_path, resolve_config_dir
from .discovery import discover
from .errors import CleanupError
from .native_retention import assess_configuration
from .policy import Policy, build_plan
from .render import render_text, to_json
from .report import build_report

DURATION_RE = re.compile(r"([1-9][0-9]{0,5})([hd])\Z")
MAX_DURATION = dt.timedelta(days=36500)

DESCRIPTION = """\
Preview which local Claude Code sessions a retention policy would select.

This release is read-only: it never moves, renames, edits, or deletes
anything, and it never launches Claude Code. It reports which root session
groups (a session plus its subagents and linked continuations) a policy would
select, which are protected and why, which are excluded because their
metadata is unknown or unsafe, and how Claude Code's own cleanupPeriodDays
sweep is configured.
"""

EPILOG = """\
policy semantics:
  --older-than and --keep-latest combine with intersection: a group is
  selected only if it is older than the cutoff AND outside the latest N.
  Last activity is the newest evidence across a group's transcripts,
  subagents, tool results, and checkpoints. --keep-latest counts session
  groups, newest first, ties broken by session id.

examples:
  claude-session-cleanup --older-than 7d
  claude-session-cleanup --keep-latest 5
  claude-session-cleanup --older-than 30d --keep-latest 10 --verbose
  claude-session-cleanup --older-than 90d --project all --json

exit status: 0 preview printed, 1 safety stop, 2 usage error.
"""


def parse_duration(value: str) -> dt.timedelta:
    match = DURATION_RE.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError("duration must be a positive whole number followed by h or d, e.g. 48h or 30d")
    amount = int(match.group(1))
    result = dt.timedelta(hours=amount) if match.group(2) == "h" else dt.timedelta(days=amount)
    if result > MAX_DURATION:
        raise argparse.ArgumentTypeError("duration must not exceed 36500d")
    return result


def positive_int(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,9}", value):
        raise argparse.ArgumentTypeError("must be a positive whole number")
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_now(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO 8601 timestamp with a timezone") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-session-cleanup",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    policy = parser.add_argument_group("retention policy (at least one selector is required)")
    policy.add_argument("--older-than", type=parse_duration, metavar="DURATION",
                        help="select groups whose last activity is older than DURATION (e.g. 48h, 30d)")
    policy.add_argument("--keep-latest", type=positive_int, metavar="N",
                        help="always keep the N most recently active session groups")
    policy.add_argument("--project", choices=("current", "all"), default="current",
                        help="'current' (default): only the exact current directory; 'all': every project")
    policy.add_argument("--min-idle", type=parse_duration, default=dt.timedelta(hours=1), metavar="DURATION",
                        help="protect groups with any activity within DURATION (default: 1h)")
    output = parser.add_argument_group("output")
    output.add_argument("--json", action="store_true", help="print the versioned JSON report instead of text")
    output.add_argument("--verbose", action="store_true", help="list protected and excluded groups with details")
    output.add_argument("--full-ids", action="store_true", help="show full session ids in text output")
    output.add_argument("--no-titles", action="store_true", help="hide session titles in text output")
    output.add_argument("--include-titles", action="store_true",
                        help="include session titles in JSON output (omitted by default; titles may contain prompt text)")
    parser.add_argument("--config-dir", metavar="PATH",
                        help="Claude Code configuration directory (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--now", type=parse_now, help=argparse.SUPPRESS)  # deterministic tests
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    tz: dt.tzinfo | None = None,
    managed_dirs: tuple[str, ...] | None = None,
    liveness_options: Mapping[str, object] | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    environment = os.environ if environment is None else environment
    parser = argument_parser()
    args = parser.parse_args(argv)
    if args.older_than is None and args.keep_latest is None:
        parser.error("at least one of --older-than or --keep-latest is required")
    try:
        config = resolve_config_dir(environment, args.config_dir)
        project_path = canonical_project_path(cwd if cwd is not None else os.getcwd())
        now = args.now or dt.datetime.now(dt.timezone.utc)
        policy = Policy(
            older_than=args.older_than,
            keep_latest=args.keep_latest,
            project_mode=args.project,
            project_path=project_path,
            min_idle=args.min_idle,
        )
        discovery = discover(config)
        signals = detect_activity(config, environment, **dict(liveness_options or {}))
        plan = build_plan(
            discovery.groups,
            policy,
            now=now,
            invoking_session_id=signals.invoking_session_id,
            session_protections=signals.protections(),
        )
        known_projects = {project_path}
        for decision in plan.decisions:
            known_projects.update(u.project_path for u in decision.group.members if u.project_path)
        retention = assess_configuration(config, sorted(known_projects), managed_dirs=managed_dirs)
        report = build_report(
            plan=plan,
            discovery=discovery,
            signals=signals,
            retention=retention,
            include_titles=args.include_titles if args.json else not args.no_titles,
            verbose_sources=args.verbose,
        )
    except CleanupError as exc:
        print(f"Safety stop: {exc}", file=stderr)
        print("No changes made.", file=stderr)
        return 1
    if args.json:
        stdout.write(to_json(report))
    else:
        stdout.write(
            render_text(report, tz=tz, verbose=args.verbose, full_ids=args.full_ids, show_titles=not args.no_titles)
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("Interrupted. No changes made.", file=sys.stderr)
        return 130
