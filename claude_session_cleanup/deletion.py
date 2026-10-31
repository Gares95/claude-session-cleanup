"""Permanent deletion of selected session groups through a reviewed saved plan.

Claude Code offers no command that deletes one conversation (``claude purge``
removes a whole project's state, ``claude rm`` only background-session
records), so this module removes the exact files that discovery attributes
to each selected session, and nothing else:

- ``projects/<dir>/<id>.jsonl`` and set-aside copies ``<id>.<suffix>``;
- ``projects/<dir>/<id>/`` when it holds only recognized entries
  (``subagents/agent-*.jsonl|.meta.json``, ``tool-results/*``,
  ``mcp-tasks/*``, ``custom-title.json``);
- ``file-history/<id>/``, ``session-env/<id>/``, and ``debug/<id>.txt``.

``tasks/<id>``, ``image-cache/<id>``, ``uploads/<id>``, and ``dev-mods/<id>``
are left in place: their ownership by a single conversation is not
documented. Project memory, prompt history, ``~/.claude.json``, settings,
and every other file are never touched.

Deletion happens only through ``--apply-plan``. The plan records every
target's type, size, timestamps, and inode. Applying it re-runs discovery,
the activity checks, and the policy with the plan's absolute cutoff, and
refuses with zero changes unless the deletable groups, their members, and
every target are exactly as reviewed. Files are then removed one by one
through directory descriptors opened without following symbolic links, each
re-checked immediately before removal; directories are removed with
``rmdir`` so anything that appears later stops the run.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import errno
import hashlib
import json
import os
import re
import stat
import uuid
from typing import Any, Iterable, Mapping

from . import __version__
from .config import ConfigDir, is_session_id, paths_equal
from .discovery import SessionGroup, SessionUnit
from .errors import CleanupError
from .policy import Plan

try:
    import fcntl
except ImportError:  # pragma: no cover - not Linux
    fcntl = None  # type: ignore[assignment]

PLAN_FORMAT = "claude-session-cleanup-plan"
PLAN_VERSION = 1
PLAN_MAX_AGE = dt.timedelta(hours=24)
PLAN_MAX_BYTES = 64 << 20

DELETED_KEYED_DIRS = ("file-history", "session-env")
LEFT_IN_PLACE_DIRS = ("tasks", "image-cache", "uploads", "dev-mods")
_LEAF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,254}\Z")
_SUBAGENT_RE = re.compile(r"agent-[A-Za-z0-9_-]{1,200}\.(?:jsonl|meta\.json)\Z")
_SESSION_SUBDIRS = {"subagents": _SUBAGENT_RE, "tool-results": _LEAF_RE, "mcp-tasks": _LEAF_RE}


class InvalidPlan(CleanupError):
    """The plan file cannot be used (malformed, edited, expired, replayed, or foreign)."""


class StalePlan(CleanupError):
    """The sessions changed since the plan was reviewed."""


class PlanLocked(CleanupError):
    """Another process is applying the same plan."""


class DeletionFailed(CleanupError):
    """Deletion stopped part-way; some targets were removed."""

    def __init__(self, message: str, deleted: list[str], remaining: list[str]) -> None:
        super().__init__(message)
        self.deleted = deleted
        self.remaining = remaining


# ---------------------------------------------------------------- targets


def _classify(rel: str, is_dir: bool, kind: str, unit: SessionUnit) -> str:
    """Return ``target``, ``left``, or ``unrecognized`` for one artifact of ``unit``."""
    if kind == "symlink":
        return "unrecognized"
    parts = rel.split("/")
    sid = unit.session_id
    if parts[0] == "projects" and len(parts) >= 3 and parts[1] == unit.project_dir_name:
        name = parts[2]
        if len(parts) == 3:
            if name == f"{sid}.jsonl" or (name.startswith(sid + ".") and _LEAF_RE.fullmatch(name)):
                return "unrecognized" if is_dir else "target"
            if name == sid:
                return "target" if is_dir else "unrecognized"
            return "unrecognized"
        if name != sid:
            return "unrecognized"
        sub = parts[3:]
        if sub == ["custom-title.json"]:
            return "unrecognized" if is_dir else "target"
        pattern = _SESSION_SUBDIRS.get(sub[0])
        if pattern is None or len(sub) > 2:
            return "unrecognized"
        if len(sub) == 1:
            return "target" if is_dir else "unrecognized"
        return "target" if not is_dir and pattern.fullmatch(sub[1]) else "unrecognized"
    if parts[0] in DELETED_KEYED_DIRS and len(parts) >= 2 and parts[1] == sid:
        if len(parts) == 2:
            return "target" if is_dir else "unrecognized"
        return "target" if len(parts) == 3 and not is_dir and _LEAF_RE.fullmatch(parts[2]) else "unrecognized"
    if parts[0] == "debug" and parts[1:] == [f"{sid}.txt"]:
        return "unrecognized" if is_dir else "target"
    if parts[0] in LEFT_IN_PLACE_DIRS and len(parts) >= 2 and parts[1] == sid:
        return "left"
    return "unrecognized"


def unit_targets(unit: SessionUnit) -> tuple[list[str], list[str], list[str]]:
    """Split a unit's artifacts into (targets, left in place, unrecognized) relative paths."""
    targets, left, unrecognized = [], [], []
    for item in unit.artifacts:
        bucket = _classify(item.rel_path, item.is_dir, item.kind, unit)
        {"target": targets, "left": left, "unrecognized": unrecognized}[bucket].append(item.rel_path)
    left_roots = sorted({"/".join(path.split("/")[:2]) for path in left})
    return sorted(targets), left_roots, sorted(unrecognized)


def group_blockers(group: SessionGroup, config: ConfigDir | None = None) -> list[str]:
    """Reasons this group cannot be deleted even when selected."""
    blockers = set()
    if any(unit.unknown_artifacts for unit in group.members):
        blockers.add("unknown-associated-artifact")
    if group.missing_links:
        blockers.add("linked-session-not-found")
    if any(unit.shared_data_links for unit in group.members):
        blockers.add("cross-session-symlink")
    for unit in group.members:
        if unit_targets(unit)[2]:
            blockers.add("unrecognized-session-data")
        if config is not None and unit.project_path is not None:
            root = os.path.realpath(config.path)
            if paths_equal(root, unit.project_path) or root.startswith(unit.project_path.rstrip(os.sep) + os.sep):
                blockers.add("storage-inside-project")
    return sorted(blockers)


# ------------------------------------------------------------ plan content


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: object, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise InvalidPlan(f"plan field {field} is missing or not a timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidPlan(f"plan field {field} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise InvalidPlan(f"plan field {field} has no timezone")
    return parsed


def _file_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _record(config: ConfigDir, rel: str) -> dict[str, Any]:
    st = os.lstat(os.path.join(config.path, *rel.split("/")))
    return {
        "path": rel,
        "type": _file_type(st.st_mode),
        "size": st.st_size if stat.S_ISREG(st.st_mode) else 0,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
        "dev": st.st_dev,
        "ino": st.st_ino,
    }


def _check_same(expected: Mapping[str, Any], st: os.stat_result, *, directory_contents: bool = True) -> str | None:
    """Return a description of the first difference, or None."""
    actual_type = _file_type(st.st_mode)
    if actual_type != expected["type"]:
        return f"is now a {actual_type}, was a {expected['type']}"
    if (st.st_dev, st.st_ino) != (expected["dev"], expected["ino"]):
        return "was replaced"
    if actual_type == "file":
        if st.st_size != expected["size"] or st.st_mtime_ns != expected["mtime_ns"]:
            return "was modified"
    elif directory_contents and st.st_mtime_ns != expected["mtime_ns"]:
        return "gained or lost entries"
    return None


def _group_entry(config: ConfigDir, group: SessionGroup, decision_reasons, rank) -> dict[str, Any]:
    members, targets, left = [], [], []
    for unit in group.members:
        unit_target_paths, unit_left, _ = unit_targets(unit)
        members.append({
            "session_id": unit.session_id,
            "parent_session_id": group.parent_of(unit.session_id),
            "project_dir": unit.project_dir_name,
            "project_path": unit.project_path,
            "last_activity": iso(unit.last_activity),
        })
        targets += [dict(_record(config, rel), session_id=unit.session_id) for rel in unit_target_paths]
        left += unit_left
    return {
        "group_id": group.group_id,
        "rank": rank,
        "reasons": list(decision_reasons),
        "last_activity": iso(group.last_activity),
        "members": members,
        "targets": targets,
        "left_in_place": sorted(left),
    }


@dataclasses.dataclass(frozen=True)
class Evaluation:
    """The deletable and blocked selected groups of a policy evaluation."""

    deletable: tuple[dict[str, Any], ...]
    blocked: tuple[dict[str, Any], ...]


def evaluate(config: ConfigDir, plan: Plan) -> Evaluation:
    deletable, blocked = [], []
    for decision in plan.by_status("selected"):
        blockers = group_blockers(decision.group, config)
        if blockers:
            blocked.append({"group_id": decision.group.group_id, "blockers": blockers})
            continue
        deletable.append(_group_entry(config, decision.group, decision.reasons, decision.rank))
    deletable.sort(key=lambda entry: entry["group_id"])
    blocked.sort(key=lambda entry: entry["group_id"])
    return Evaluation(tuple(deletable), tuple(blocked))


def _digest(document: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in document.items() if key not in ("digest", "state")}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _config_identity(config: ConfigDir) -> dict[str, Any]:
    st = os.stat(config.path)
    return {"path": config.path, "dev": st.st_dev, "ino": st.st_ino}


def build_plan_document(
    *,
    config: ConfigDir,
    plan: Plan,
    evaluation: Evaluation,
    machine: str | None,
    platform: str,
    now: dt.datetime,
) -> dict[str, Any]:
    policy = plan.policy
    document = {
        "format": PLAN_FORMAT,
        "version": PLAN_VERSION,
        "plan_id": str(uuid.uuid4()),
        "action": "delete",
        "created_at": iso(now),
        "expires_at": iso(now + PLAN_MAX_AGE),
        "tool_version": __version__,
        "platform": platform,
        "machine": machine,
        "config_dir": _config_identity(config),
        "policy": {
            "older_than_seconds": int(policy.older_than.total_seconds()) if policy.older_than else None,
            "cutoff": iso(plan.cutoff) if plan.cutoff is not None else None,
            "keep_latest": policy.keep_latest,
            "project_mode": policy.project_mode,
            "project_path": policy.project_path,
            "min_idle_seconds": int(policy.min_idle.total_seconds()),
        },
        "groups": list(evaluation.deletable),
        "blocked_groups": list(evaluation.blocked),
        "totals": {
            "groups": len(evaluation.deletable),
            "sessions": sum(len(group["members"]) for group in evaluation.deletable),
            "targets": sum(len(group["targets"]) for group in evaluation.deletable),
            "bytes": sum(t["size"] for group in evaluation.deletable for t in group["targets"]),
        },
    }
    document["digest"] = _digest(document)
    document["state"] = {"status": "saved", "updated_at": iso(now), "deleted": []}
    return document


# ----------------------------------------------------------- plan files


def _refuse_inside(path: str, roots: Iterable[str], what: str) -> None:
    real = os.path.realpath(path)
    for root in roots:
        root_real = os.path.realpath(root)
        if paths_equal(real, root_real) or real.startswith(root_real.rstrip(os.sep) + os.sep):
            raise CleanupError(f"refusing to write the plan inside {what}")


def save_plan(path: str, document: Mapping[str, Any], *, config: ConfigDir) -> str:
    """Create the plan file (mode 0600). Never overwrites an existing file."""
    if not path:
        raise CleanupError("--save-plan needs a file path")
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise CleanupError("the plan's directory does not exist")
    _refuse_inside(path, [config.path], "the Claude Code configuration directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CleanupError("the plan file already exists; choose a new path") from exc
    except OSError as exc:
        raise CleanupError(f"cannot create the plan file: {exc.strerror or exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def write_state(path: str, document: dict[str, Any]) -> None:
    """Replace the plan file atomically with an updated state."""
    directory = os.path.dirname(path)
    temp = os.path.join(directory, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidPlan(message)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_target(target: object) -> None:
    _require(isinstance(target, dict), "plan target is not an object")
    assert isinstance(target, dict)
    _require(set(target) == {"path", "type", "size", "mtime_ns", "ctime_ns", "dev", "ino", "session_id"},
             "plan target has unexpected fields")
    rel = target["path"]
    _require(isinstance(rel, str) and rel and not rel.startswith("/") and "\\" not in rel and "\0" not in rel,
             "plan target path is malformed")
    _require(all(part not in ("", ".", "..") for part in rel.split("/")), "plan target path is not normalized")
    _require(target["type"] in ("file", "dir"), "plan target type is malformed")
    _require(all(_is_int(target[key]) and target[key] >= 0 for key in ("size", "mtime_ns", "ctime_ns", "dev", "ino")),
             "plan target metadata is malformed")
    _require(is_session_id(target["session_id"]), "plan target session id is malformed")


def validate_document(document: object) -> dict[str, Any]:
    """Structural validation of a plan document; raises InvalidPlan."""
    _require(isinstance(document, dict), "the plan is not a JSON object")
    assert isinstance(document, dict)
    _require(document.get("format") == PLAN_FORMAT, "not a claude-session-cleanup plan")
    _require(document.get("version") == PLAN_VERSION, "unsupported plan version")
    _require(document.get("action") == "delete", "the plan's action is not delete")
    _require(isinstance(document.get("digest"), str) and document["digest"] == _digest(document),
             "the plan was edited after it was saved (digest mismatch)")
    _require(is_session_id(document.get("plan_id")), "plan id is malformed")
    policy = document.get("policy")
    _require(isinstance(policy, dict), "plan policy is malformed")
    assert isinstance(policy, dict)
    _require(policy.get("project_mode") in ("current", "all"), "plan scope is malformed")
    _require(isinstance(policy.get("project_path"), str) and os.path.isabs(policy["project_path"]),
             "plan project path is malformed")
    older = policy.get("older_than_seconds")
    keep = policy.get("keep_latest")
    _require(older is None or (_is_int(older) and older > 0), "plan age policy is malformed")
    _require(keep is None or (_is_int(keep) and keep > 0), "plan keep-latest policy is malformed")
    _require(older is not None or keep is not None, "plan has no selector")
    _require((older is None) == (policy.get("cutoff") is None), "plan cutoff does not match its age policy")
    _require(_is_int(policy.get("min_idle_seconds")) and policy["min_idle_seconds"] > 0, "plan minimum idle is malformed")
    config = document.get("config_dir")
    _require(isinstance(config, dict) and isinstance(config.get("path"), str)
             and _is_int(config.get("dev")) and _is_int(config.get("ino")), "plan configuration directory is malformed")
    for key in ("groups", "blocked_groups"):
        _require(isinstance(document.get(key), list), f"plan {key} is malformed")
    for group in document["groups"]:
        _require(isinstance(group, dict) and is_session_id(group.get("group_id")), "plan group is malformed")
        _require(isinstance(group.get("members"), list) and group["members"], "plan group members are malformed")
        for member in group["members"]:
            _require(isinstance(member, dict) and is_session_id(member.get("session_id")), "plan member is malformed")
        _require(isinstance(group.get("targets"), list) and group["targets"], "plan group targets are malformed")
        for target in group["targets"]:
            _validate_target(target)
    paths = [target["path"] for group in document["groups"] for target in group["targets"]]
    _require(len(paths) == len(set(paths)), "plan lists a target more than once")
    group_ids = [group["group_id"] for group in document["groups"]]
    _require(len(group_ids) == len(set(group_ids)), "plan lists a group more than once")
    state = document.get("state")
    _require(isinstance(state, dict) and isinstance(state.get("status"), str), "plan state is malformed")
    return document


def read_plan(path: str) -> tuple[dict[str, Any], int]:
    """Open the plan without following a symlink; return (document, open fd)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise InvalidPlan(f"cannot open the plan: {exc.strerror or exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > PLAN_MAX_BYTES:
            raise InvalidPlan("the plan is not a regular file of acceptable size")
        data = os.read(fd, PLAN_MAX_BYTES + 1)
        try:
            document = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidPlan("the plan is not valid JSON") from exc
        return validate_document(document), fd
    except BaseException:
        os.close(fd)
        raise


def read_plan_locked(path: str, fd: int) -> dict[str, Any]:
    """Re-read the plan through its locked descriptor and make sure the path still names it."""
    try:
        at_path = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise InvalidPlan(f"the plan file disappeared: {exc.strerror or exc}") from exc
    held = os.fstat(fd)
    if (at_path.st_dev, at_path.st_ino) != (held.st_dev, held.st_ino):
        raise PlanLocked("the plan file was replaced while it was being opened; another run may be using it")
    os.lseek(fd, 0, os.SEEK_SET)
    data = os.read(fd, PLAN_MAX_BYTES + 1)
    try:
        return validate_document(json.loads(data.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidPlan("the plan is not valid JSON") from exc


def check_plan_context(
    document: Mapping[str, Any],
    *,
    config: ConfigDir,
    cwd: str,
    now: dt.datetime,
    machine: str | None,
    platform: str,
) -> None:
    """Refuse plans that are replayed, expired, or from another tool, machine, directory, or configuration."""
    status = document["state"]["status"]
    if status != "saved":
        raise InvalidPlan(f"the plan was already used (state: {status}); save and review a new plan")
    if document.get("tool_version") != __version__:
        raise InvalidPlan("the plan was saved by another version of this tool; save a new plan")
    created = parse_time(document.get("created_at"), "created_at")
    expires = parse_time(document.get("expires_at"), "expires_at")
    if now < created:
        raise InvalidPlan("the plan was created in the future; check the system clock")
    if now >= expires or expires - created > PLAN_MAX_AGE:
        raise InvalidPlan("the plan has expired; save and review a new plan")
    if document.get("platform") != platform or document.get("machine") != machine:
        raise InvalidPlan("the plan was saved on another machine or PID namespace")
    saved = document["config_dir"]
    current = _config_identity(config)
    if not paths_equal(saved["path"], current["path"]) or (saved["dev"], saved["ino"]) != (current["dev"], current["ino"]):
        raise InvalidPlan("the plan was saved for another Claude Code configuration directory")
    policy = document["policy"]
    if policy["project_mode"] == "current" and not paths_equal(policy["project_path"], cwd):
        raise InvalidPlan("run --apply-plan from the project directory the plan was saved in")


def lock(fd: int) -> None:
    if fcntl is None:  # pragma: no cover - deletion is Linux-only
        raise PlanLocked("plan locking is unavailable on this platform")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            raise PlanLocked("another process is applying this plan") from exc
        raise


# -------------------------------------------------------------- comparison


def compare(document: Mapping[str, Any], fresh: Evaluation) -> list[str]:
    """Differences between the reviewed plan and a fresh evaluation (empty when identical)."""
    problems: list[str] = []
    planned = {group["group_id"]: group for group in document["groups"]}
    current = {group["group_id"]: group for group in fresh.deletable}
    for group_id in sorted(set(planned) - set(current)):
        problems.append(f"group {group_id} is no longer deletable under the plan's policy")
    for group_id in sorted(set(current) - set(planned)):
        problems.append(f"group {group_id} would now also be deleted")
    blocked_then = {item["group_id"]: item.get("blockers") for item in document["blocked_groups"]}
    blocked_now = {item["group_id"]: item["blockers"] for item in fresh.blocked}
    if blocked_then != blocked_now:
        problems.append("the set of selected groups that cannot be deleted changed")
    for group_id in sorted(set(planned) & set(current)):
        then, now = planned[group_id], current[group_id]
        for key, label in (("members", "members"), ("last_activity", "last activity"), ("left_in_place", "associated data")):
            if then.get(key) != now[key]:
                problems.append(f"group {group_id}: {label} changed")
        then_targets = {t["path"]: t for t in then["targets"]}
        now_targets = {t["path"]: t for t in now["targets"]}
        if set(then_targets) != set(now_targets):
            problems.append(f"group {group_id}: the files to delete changed")
            continue
        for rel in sorted(then_targets):
            if then_targets[rel] != now_targets[rel]:
                problems.append(f"group {group_id}: {rel} changed")
    return problems


# ---------------------------------------------------------------- deletion


def _excluded_roots(environment: Mapping[str, str]) -> list[str]:
    """Codex state directories that no target may ever be inside."""
    roots = []
    if environment.get("CODEX_HOME"):
        roots.append(environment["CODEX_HOME"])
    home = environment.get("HOME") or os.path.expanduser("~")
    roots.append(os.path.join(home, ".codex"))
    return [os.path.realpath(root) for root in roots]


def validate_paths(
    document: Mapping[str, Any], config: ConfigDir, environment: Mapping[str, str]
) -> list[str]:
    """Static checks on every target path; returns problems (empty when safe)."""
    problems: list[str] = []
    root = os.path.realpath(config.path)
    forbidden = _excluded_roots(environment)
    for excluded in forbidden:
        if paths_equal(root, excluded) or root.startswith(excluded + os.sep) or excluded.startswith(root + os.sep):
            problems.append("the Claude Code configuration directory overlaps Codex state")
    for group in document["groups"]:
        members = {m["session_id"]: m for m in group["members"]}
        for member in members.values():
            project = member.get("project_path")
            if project and (paths_equal(root, project) or root.startswith(project.rstrip(os.sep) + os.sep)):
                problems.append(f"group {group['group_id']}: the configuration directory is inside the project")
        for target in group["targets"]:
            rel = target["path"]
            parts = rel.split("/")
            member = members.get(target["session_id"])
            if member is None:
                problems.append(f"{rel}: belongs to no member of its group")
                continue
            sid = member["session_id"]
            ok = (
                (parts[0] == "projects" and len(parts) >= 3 and parts[1] == member["project_dir"]
                 and parts[2].startswith(sid))
                or (parts[0] in DELETED_KEYED_DIRS and len(parts) >= 2 and parts[1] == sid)
                or parts == ["debug", f"{sid}.txt"]
            )
            if not ok or any(part.lower() == ".codex" for part in parts):
                problems.append(f"{rel}: outside the session's own storage")
                continue
            absolute = os.path.normpath(os.path.join(root, *parts))
            if not absolute.startswith(root + os.sep):
                problems.append(f"{rel}: escapes the configuration directory")
            for excluded in forbidden:
                if absolute == excluded or absolute.startswith(excluded + os.sep):
                    problems.append(f"{rel}: inside Codex state")
    return problems


def _open_dir(root_fd: int, parts: list[str]) -> int:
    """Open a directory below ``root_fd`` refusing symlinks at every component."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def deletion_order(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Children before parents; within a session its transcript last; descendants before roots."""
    ordered: list[dict[str, Any]] = []
    for group in document["groups"]:
        member_order = [m["session_id"] for m in group["members"]]
        by_session: dict[str, list[dict[str, Any]]] = {}
        for target in group["targets"]:
            by_session.setdefault(target["session_id"], []).append(target)
        for sid in reversed(member_order):
            targets = by_session.get(sid, [])
            transcript = [t for t in targets if t["path"].endswith(f"/{sid}.jsonl") and t["path"].count("/") == 2]
            rest = [t for t in targets if t not in transcript]
            rest.sort(key=lambda t: (-t["path"].count("/"), t["path"]))
            ordered += rest + transcript
    return ordered


def preflight(config: ConfigDir, targets: list[dict[str, Any]]) -> list[str]:
    """Re-check every target through no-follow directory descriptors before deleting anything."""
    problems: list[str] = []
    root_fd = os.open(config.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for target in targets:
            parts = target["path"].split("/")
            try:
                parent = _open_dir(root_fd, parts[:-1])
            except OSError as exc:
                problems.append(f"{target['path']}: parent directory unusable ({exc.strerror or exc})")
                continue
            try:
                st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                problems.append(f"{target['path']}: {exc.strerror or exc}")
                continue
            finally:
                os.close(parent)
            difference = _check_same(target, st)
            if difference is None and target["type"] == "file" and st.st_ctime_ns != target["ctime_ns"]:
                difference = "changed metadata"
            if difference:
                problems.append(f"{target['path']}: {difference}")
    finally:
        os.close(root_fd)
    return problems


def delete_targets(config: ConfigDir, targets: list[dict[str, Any]], on_deleted) -> None:
    """Remove targets in order; stop at the first surprise. Raises DeletionFailed."""
    deleted: list[str] = []
    root_fd = os.open(config.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for index, target in enumerate(targets):
            parts = target["path"].split("/")
            try:
                parent = _open_dir(root_fd, parts[:-1])
                try:
                    st = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    difference = _check_same(target, st, directory_contents=False)
                    if difference:
                        raise OSError(errno.ESTALE, f"{difference} since the preflight check")
                    if target["type"] == "dir":
                        os.rmdir(parts[-1], dir_fd=parent)
                    else:
                        os.unlink(parts[-1], dir_fd=parent)
                finally:
                    os.close(parent)
            except OSError as exc:
                remaining = [t["path"] for t in targets[index:]]
                raise DeletionFailed(
                    f"stopped at {target['path']}: {exc.strerror or exc}", deleted, remaining
                ) from exc
            deleted.append(target["path"])
            on_deleted(target["path"])
    finally:
        os.close(root_fd)
