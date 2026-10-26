"""Read-only signals that a session may be in use.

These are *protective* heuristics, not an authoritative list of running
sessions. Previews never launch Claude Code (``claude agents --json`` could
start a session-side cleanup sweep and writes configuration files), so the
signals come from files only:

- the invoking session id (``CLAUDE_CODE_SESSION_ID``);
- the live-session registry ``sessions/<pid>.json``, with process liveness
  checked against ``/proc/<pid>/stat`` start time on Linux;
- background-session records under ``jobs/*/state.json``.

The policy engine adds a recent-modification guard on top of these.
Sessions run under another configuration directory, on another machine
sharing this directory, or by Claude Code versions that do not write the
registry cannot be detected. Mutation support must stay blocked until a
more complete source of truth is available.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import sys
from typing import Callable, Mapping

from .config import ConfigDir, is_session_id
from .fsutil import read_small_file

_REGISTRY_NAME_RE = re.compile(r"([1-9][0-9]{0,9})\.json\Z")
_JOB_NAME_RE = re.compile(r"[0-9a-f]{8}\Z")

LIMITATIONS = (
    "Sessions using a different CLAUDE_CONFIG_DIR or running on another machine are not visible.",
    "Claude Code versions or surfaces that do not write sessions/<pid>.json are not visible.",
    "Process liveness is verified against the recorded start time on Linux only; elsewhere a matching PID is treated as possibly running.",
    "claude agents --json is deliberately not invoked: previews never launch Claude Code.",
)
MUTATION_BLOCKER = "active-session-detection-incomplete"


@dataclasses.dataclass(frozen=True)
class RegistryEntry:
    file: str
    pid: int
    session_id: str
    liveness: str  # running | possibly-running | not-running | stale-pid


@dataclasses.dataclass(frozen=True)
class ActivitySignals:
    invoking_session_id: str | None
    invoking_session_env_present: bool
    registry: tuple[RegistryEntry, ...]
    registry_unreadable: tuple[str, ...]
    background_session_ids: frozenset[str]
    job_records_read: int
    job_records_unreadable: tuple[str, ...]

    def protections(self) -> dict[str, str]:
        """Map session id to the strongest registry/background protection reason."""
        reasons: dict[str, str] = {}
        for entry in self.registry:
            if entry.liveness == "running":
                reasons[entry.session_id] = "registered-running"
            elif entry.liveness == "possibly-running":
                reasons.setdefault(entry.session_id, "registered-possibly-running")
        for session_id in self.background_session_ids:
            reasons.setdefault(session_id, "background-session-record")
        return reasons


def _linux_liveness(pid: int, proc_start: object, proc_root: str) -> str:
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), "rb") as handle:
            data = handle.read(4096).decode("ascii", errors="replace")
    except FileNotFoundError:
        return "not-running"
    except OSError:
        return "possibly-running"
    try:
        fields = data.rsplit(")", 1)[1].split()
        start_time = fields[19]  # field 22 of /proc/<pid>/stat
    except IndexError:
        return "possibly-running"
    if isinstance(proc_start, (str, int)) and not isinstance(proc_start, bool):
        return "running" if str(proc_start) == start_time else "stale-pid"
    return "possibly-running"


def _posix_liveness(pid: int, kill: Callable[[int, int], None]) -> str:
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return "not-running"
    except OSError:
        return "possibly-running"
    return "possibly-running"


def process_liveness(
    pid: int,
    proc_start: object,
    *,
    platform: str = sys.platform,
    proc_root: str = "/proc",
    kill: Callable[[int, int], None] | None = None,
) -> str:
    if platform.startswith("linux") and os.path.isdir(proc_root):
        return _linux_liveness(pid, proc_start, proc_root)
    if platform.startswith("win"):
        # os.kill() on Windows terminates processes; never call it here.
        return "possibly-running"
    return _posix_liveness(pid, kill or os.kill)


def _list_dir(path: str) -> list[str]:
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return []
        with os.scandir(path) as entries:
            return sorted(entry.name for entry in entries)
    except OSError:
        return []


def read_registry(config: ConfigDir, **liveness_options) -> tuple[list[RegistryEntry], list[str]]:
    entries: list[RegistryEntry] = []
    unreadable: list[str] = []
    directory = os.path.join(config.path, "sessions")
    for name in _list_dir(directory):
        match = _REGISTRY_NAME_RE.fullmatch(name)
        if match is None:
            continue
        rel = f"sessions/{name}"
        data = read_small_file(os.path.join(directory, name), 64 << 10)
        try:
            record = json.loads(data) if data is not None else None
        except (ValueError, UnicodeDecodeError):
            record = None
        session_id = record.get("sessionId") if isinstance(record, dict) else None
        pid = record.get("pid") if isinstance(record, dict) else None
        if not is_session_id(session_id) or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            unreadable.append(rel)
            continue
        liveness = process_liveness(pid, record.get("procStart"), **liveness_options)
        entries.append(RegistryEntry(rel, pid, session_id, liveness))
    return entries, unreadable


def read_job_records(config: ConfigDir) -> tuple[set[str], int, list[str]]:
    session_ids: set[str] = set()
    read = 0
    unreadable: list[str] = []
    directory = os.path.join(config.path, "jobs")
    for name in _list_dir(directory):
        if not _JOB_NAME_RE.fullmatch(name):
            continue
        rel = f"jobs/{name}/state.json"
        path = os.path.join(directory, name, "state.json")
        try:
            if not stat.S_ISDIR(os.lstat(os.path.join(directory, name)).st_mode):
                continue
        except OSError:
            continue
        data = read_small_file(path, 4 << 20)
        try:
            record = json.loads(data) if data is not None else None
        except (ValueError, UnicodeDecodeError):
            record = None
        if not isinstance(record, dict):
            if os.path.lexists(path):
                unreadable.append(rel)
            continue
        read += 1
        for key in ("sessionId", "resumeSessionId"):
            if is_session_id(record.get(key)):
                session_ids.add(record[key])
    return session_ids, read, unreadable


def detect_activity(
    config: ConfigDir, environment: Mapping[str, str], **liveness_options
) -> ActivitySignals:
    raw_invoking = environment.get("CLAUDE_CODE_SESSION_ID")
    registry, registry_unreadable = read_registry(config, **liveness_options)
    background, jobs_read, jobs_unreadable = read_job_records(config)
    return ActivitySignals(
        invoking_session_id=raw_invoking if is_session_id(raw_invoking) else None,
        invoking_session_env_present=bool(raw_invoking) or environment.get("CLAUDECODE") == "1",
        registry=tuple(registry),
        registry_unreadable=tuple(registry_unreadable),
        background_session_ids=frozenset(background),
        job_records_read=jobs_read,
        job_records_unreadable=tuple(jobs_unreadable),
    )
