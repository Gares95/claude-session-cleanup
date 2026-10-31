"""Command-line interface: previews, saved deletion plans, and applying a reviewed plan."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from typing import Mapping, Sequence, TextIO

from . import __version__
from . import deletion
from .activity import detect_activity
from .config import canonical_project_path, resolve_config_dir
from .discovery import discover
from .errors import CleanupError
from .native_retention import assess_configuration
from .policy import Policy, build_plan
from .render import render_apply, render_text, to_json
from .report import build_report

DURATION_RE = re.compile(r"([1-9][0-9]{0,5})([hd])\Z")
MAX_DURATION = dt.timedelta(days=36500)

DESCRIPTION = """\
Preview which local Claude Code sessions a retention policy would select,
and permanently delete them through a reviewed saved plan.

Every run is a read-only preview unless --apply-plan is given. It reports
which root groups (a session plus its subagents and linked continuations) a
policy would select, which are retained or protected and why, which are
excluded because their metadata is unknown or unsafe, and how Claude Code's
own cleanupPeriodDays sweep is configured. The tool never launches Claude
Code. There is no archive, quarantine, or restore.
"""

EPILOG = """\
policy semantics (shared contract with codex-session-cleanup, contract/SPEC.md):
  --older-than and --keep-latest combine with intersection: a group is
  selected only if it is older than the cutoff AND outside the latest N.
  A group's last activity is the newest evidence across its transcripts,
  subagents, tool results, and checkpoints, so a recent member keeps an old
  root. --keep-latest counts root groups, newest first, ties broken by id;
  protected groups occupy ranks, excluded groups do not.

statuses:
  selected   eligible for the requested action (nothing happens in a preview)
  retained   kept by the policy (newer than cutoff, within latest N)
  protected  kept by a safety rule (this session, running, recently active)
  excluded   unknown or unsafe metadata; never selected or ranked

permanent deletion (irreversible; Linux only):
  1. preview:  --older-than 30d --action delete --verbose
  2. save:     --older-than 30d --action delete --save-plan .local/plan.json
  3. review the printed preview and the plan file
  4. apply:    --apply-plan .local/plan.json   (from the same directory)
  Applying re-checks everything and deletes nothing unless the sessions are
  exactly as reviewed. A plan expires after 24 hours and works once. There
  is no --apply: deletion always needs a reviewed saved plan.

examples:
  claude-session-cleanup --older-than 7d
  claude-session-cleanup --keep-latest 5
  claude-session-cleanup --older-than 30d --keep-latest 10 --verbose
  claude-session-cleanup --older-than 90d --project all --json

The JSON report follows contract/preview-v1.schema.json (schema_version 1).
exit status: 0 success, 1 safety stop or failed deletion, 2 usage error or
unusable plan, 3 stale plan (nothing deleted), 4 plan locked by another run.
"""

POLICY_FLAGS = ("older_than", "keep_latest", "action", "save_plan", "json", "include_titles")


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
                        help="retain the N most recently active root groups")
    policy.add_argument("--project", choices=("current", "all"), default="current",
                        help="'current' (default): only the exact current directory; 'all': every project")
    policy.add_argument("--min-idle", type=parse_duration, default=dt.timedelta(hours=1), metavar="DURATION",
                        help="protect groups with any activity within DURATION (default: 1h)")
    output = parser.add_argument_group("output")
    output.add_argument("--json", action="store_true", help="print the shared JSON preview (schema_version 1) instead of text")
    output.add_argument("--verbose", action="store_true", help="list retained, protected, and excluded groups with details")
    output.add_argument("--full-ids", action="store_true", help="show full session ids in text output")
    output.add_argument("--no-titles", action="store_true", help="hide session titles in text output")
    output.add_argument("--include-titles", action="store_true",
                        help="include session titles in JSON output (omitted by default; titles may contain prompt text)")
    action = parser.add_argument_group("actions")
    action.add_argument("--action", choices=("delete", "archive", "quarantine"),
                        help="'delete' marks selected groups for permanent deletion in the preview; "
                             "archive and quarantine are not offered")
    action.add_argument("--save-plan", metavar="PATH",
                        help="with --action delete: save the reviewed deletion plan to a new file (mode 0600)")
    action.add_argument("--apply-plan", metavar="PATH",
                        help="delete exactly what a saved plan lists, after re-checking everything; "
                             "takes no policy options")
    action.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
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
    if args.apply:
        parser.error("permanent deletion requires a reviewed saved plan: run with --action delete "
                     "--save-plan PATH, review it, then --apply-plan PATH")
    if args.action in ("archive", "quarantine"):
        parser.error(f"{args.action} is not offered: Claude Code has no archive, and this tool does not "
                     "quarantine sessions; the only action is --action delete")
    if args.apply_plan is not None:
        given = [name for name in POLICY_FLAGS if getattr(args, name)]
        if given or args.project != "current" or args.min_idle != dt.timedelta(hours=1):
            parser.error("--apply-plan takes no policy or output options; the saved plan is the policy")
        return _apply(args, environment, cwd, stdout, stderr, liveness_options)
    if args.older_than is None and args.keep_latest is None:
        parser.error("at least one of --older-than or --keep-latest is required")
    if args.save_plan is not None and args.action != "delete":
        parser.error("--save-plan requires --action delete")
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
            session_protections=signals.protections(discovery.units),
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
            requested_action=args.action,
        )
        saved = None
        if args.save_plan is not None:
            if signals.mutation_blockers:
                raise CleanupError(
                    "deletion is not supported here: " + ", ".join(signals.mutation_blockers)
                    + " (it needs the Linux process scan)"
                )
            evaluation = deletion.evaluate(config, plan)
            if evaluation.deletable:
                document = deletion.build_plan_document(
                    config=config, plan=plan, evaluation=evaluation, machine=signals.pid_domain,
                    platform=_platform(liveness_options), now=now,
                )
                saved = (deletion.save_plan(args.save_plan, document, config=config), document)
            else:
                saved = (None, None)
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
    if saved is not None:
        path, document = saved
        stream = stderr if args.json else stdout
        if path is None:
            print("No plan saved: no selected group can be deleted.", file=stream)
        else:
            totals = document["totals"]
            print(
                f"Saved deletion plan: {path}\n"
                f"  {totals['groups']} groups, {totals['sessions']} sessions, {totals['targets']} files and "
                f"directories; expires {document['expires_at']}.\n"
                f"  Review it, then run from this directory: claude-session-cleanup --apply-plan {path}",
                file=stream,
            )
    return 0


def _platform(liveness_options: Mapping[str, object] | None) -> str:
    value = (liveness_options or {}).get("platform")
    return value if isinstance(value, str) else sys.platform


def _apply(args, environment, cwd, stdout, stderr, liveness_options) -> int:
    """Apply a saved plan. Exit 0 deleted, 1 safety stop/failure, 2 unusable plan, 3 stale, 4 locked."""
    options = dict(liveness_options or {})
    plan_path = os.path.abspath(args.apply_plan)
    fd = None
    try:
        config = resolve_config_dir(environment, args.config_dir)
        project_path = canonical_project_path(cwd if cwd is not None else os.getcwd())
        now = args.now or dt.datetime.now(dt.timezone.utc)
        document, fd = deletion.read_plan(plan_path)
        deletion.lock(fd)
        document = deletion.read_plan_locked(plan_path, fd)
        signals = detect_activity(config, environment, **options)
        if signals.mutation_blockers:
            raise CleanupError("deletion is not supported here: " + ", ".join(signals.mutation_blockers))
        deletion.check_plan_context(
            document, config=config, cwd=project_path, now=now, machine=signals.pid_domain,
            platform=_platform(liveness_options),
        )
        saved_policy = document["policy"]
        policy = Policy(
            older_than=dt.timedelta(seconds=saved_policy["older_than_seconds"])
            if saved_policy["older_than_seconds"] else None,
            keep_latest=saved_policy["keep_latest"],
            project_mode=saved_policy["project_mode"],
            project_path=saved_policy["project_path"],
            min_idle=dt.timedelta(seconds=saved_policy["min_idle_seconds"]),
        )
        cutoff = deletion.parse_time(saved_policy["cutoff"], "cutoff") if saved_policy["cutoff"] else None
        discovery = discover(config)
        plan = build_plan(
            discovery.groups, policy, now=now, cutoff=cutoff,
            invoking_session_id=signals.invoking_session_id,
            session_protections=signals.protections(discovery.units),
        )
        problems = deletion.compare(document, deletion.evaluate(config, plan))
        if problems:
            raise deletion.StalePlan("the sessions changed since the plan was reviewed:\n  " + "\n  ".join(problems))
        targets = deletion.deletion_order(document)
        problems = deletion.validate_paths(document, config, environment) + deletion.preflight(config, targets)
        if problems:
            raise deletion.StalePlan("a target failed its safety check:\n  " + "\n  ".join(problems))
        document["state"] = {"status": "applying", "updated_at": deletion.iso(now), "deleted": []}
        deletion.write_state(plan_path, document)
        deleted: list[str] = []
        try:
            deletion.delete_targets(config, targets, deleted.append)
        except deletion.DeletionFailed as failure:
            document["state"] = {"status": "failed", "updated_at": deletion.iso(now), "deleted": failure.deleted,
                                 "error": str(failure)}
            _try_write_state(plan_path, document, stderr)
            stdout.write(render_apply(document, deleted=failure.deleted, remaining=failure.remaining,
                                      error=str(failure), full_ids=args.full_ids))
            return 1
        document["state"] = {"status": "applied", "updated_at": deletion.iso(now), "deleted": deleted}
        _try_write_state(plan_path, document, stderr)
        leftovers = [t["path"] for t in targets if os.path.lexists(os.path.join(config.path, *t["path"].split("/")))]
        stdout.write(render_apply(document, deleted=deleted, remaining=leftovers, error=None, full_ids=args.full_ids))
        return 1 if leftovers else 0
    except deletion.InvalidPlan as exc:
        print(f"Unusable plan: {exc}\nNothing was deleted.", file=stderr)
        return 2
    except deletion.StalePlan as exc:
        print(f"Stale plan: {exc}\nNothing was deleted. Preview again and save a new plan.", file=stderr)
        return 3
    except deletion.PlanLocked as exc:
        print(f"Plan locked: {exc}\nNothing was deleted.", file=stderr)
        return 4
    except CleanupError as exc:
        print(f"Safety stop: {exc}", file=stderr)
        print("Nothing was deleted.", file=stderr)
        return 1
    finally:
        if fd is not None:
            os.close(fd)


def _try_write_state(path: str, document, stderr) -> None:
    try:
        deletion.write_state(path, document)
    except OSError as exc:
        print(f"Warning: could not record the plan state ({exc.strerror or exc}); "
              "do not apply this plan again.", file=stderr)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except KeyboardInterrupt:
        print("Interrupted. No changes made.", file=sys.stderr)
        return 130
