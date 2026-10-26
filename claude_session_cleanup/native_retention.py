"""Diagnostics for Claude Code's own retention sweep (``cleanupPeriodDays``).

This tool cannot control or reliably predict that sweep. It reports three
separate things and never computes a future deletion time:

- **observed** metadata: transcript modification times and the
  ``.last-cleanup`` marker;
- **configured** retention: every ``cleanupPeriodDays`` value it could read,
  with its source, plus what it could not inspect;
- **estimated risk**: whether a transcript's modification time is already
  older than the lowest applicable configured period, or close to it.

Facts behind the model (Claude Code 2.1.310, see docs/native-retention.md):
the sweep ages transcripts by file modification time, runs in the background
after a session starts, removes a parent's whole ``<id>/`` directory
including recent subagents, and reads ``cleanupPeriodDays`` from the merged
settings of whichever project that session starts in, so one project's
setting can shorten retention for every project. Managed settings override
everything.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sys

from .config import ConfigDir
from .fsutil import read_small_file
from .transcript import parse_timestamp

DEFAULT_PERIOD_DAYS = 30
APPROACHING_WINDOW_DAYS = 7
RISK_LEVELS = ("past-period", "approaching", "within-period")

LIMITATIONS = (
    "MDM profiles, Windows registry policies, and server-managed settings not cached on disk are not inspected.",
    "Settings passed with --settings, --setting-sources, or by an embedding host are not visible.",
    "Project settings are read only for projects this preview knows about; any other directory you start Claude Code in can apply a lower value.",
    "Claude Desktop and Cowork exemptions (desktopSessionCleanupPeriodDays) are not modelled.",
    "Sweep behaviour differs between Claude Code versions; this model was checked against 2.1.310.",
)
DISCLAIMER = (
    "Estimates only. Claude Code deletes transcripts on its own schedule; "
    "this tool cannot predict exact deletion times or prevent the deletion."
)


@dataclasses.dataclass(frozen=True)
class SettingSource:
    kind: str  # managed-file, managed-drop-in, server-managed-cache, user, project-shared, project-local
    path: str
    status: str  # set | unset | missing | invalid | unreadable
    value: int | None


@dataclasses.dataclass(frozen=True)
class RetentionConfig:
    sources: tuple[SettingSource, ...]
    managed_days: int | None
    user_days: int | None
    applicable_days: tuple[int, ...]
    lowest_applicable_days: int
    sweep_may_pause: bool
    last_cleanup_marker: dt.datetime | None
    last_cleanup_marker_status: str  # present | missing | invalid


def managed_settings_dirs(platform: str = sys.platform) -> tuple[str, ...]:
    if platform == "darwin":
        return ("/Library/Application Support/ClaudeCode",)
    if platform.startswith("win"):
        return (r"C:\Program Files\ClaudeCode",)
    return ("/etc/claude-code",)


def read_setting(path: str, kind: str) -> SettingSource:
    if not os.path.lexists(path):
        return SettingSource(kind, path, "missing", None)
    data = read_small_file(path, 4 << 20, follow_symlinks=True)
    if data is None:
        return SettingSource(kind, path, "unreadable", None)
    try:
        document = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return SettingSource(kind, path, "unreadable", None)
    if not isinstance(document, dict):
        return SettingSource(kind, path, "unreadable", None)
    if "cleanupPeriodDays" not in document:
        return SettingSource(kind, path, "unset", None)
    value = document["cleanupPeriodDays"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return SettingSource(kind, path, "invalid", None)
    return SettingSource(kind, path, "set", value)


def _repo_root(path: str) -> str | None:
    current = path
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _managed_sources(directories: tuple[str, ...]) -> list[SettingSource]:
    sources: list[SettingSource] = []
    for directory in directories:
        sources.append(read_setting(os.path.join(directory, "managed-settings.json"), "managed-file"))
        drop_ins = os.path.join(directory, "managed-settings.d")
        try:
            names = sorted(os.listdir(drop_ins))
        except OSError:
            names = []
        for name in names:
            if name.endswith(".json") and not name.startswith("."):
                sources.append(read_setting(os.path.join(drop_ins, name), "managed-drop-in"))
    return sources


def _read_marker(config: ConfigDir) -> tuple[dt.datetime | None, str]:
    path = os.path.join(config.path, ".last-cleanup")
    if not os.path.lexists(path):
        return None, "missing"
    data = read_small_file(path, 256)
    if data is None:
        return None, "invalid"
    marker = parse_timestamp(data.decode("utf-8", errors="replace").strip())
    return (marker, "present") if marker is not None else (None, "invalid")


def assess_configuration(
    config: ConfigDir,
    project_paths: list[str] | tuple[str, ...],
    *,
    managed_dirs: tuple[str, ...] | None = None,
) -> RetentionConfig:
    sources = _managed_sources(managed_dirs if managed_dirs is not None else managed_settings_dirs())
    sources.append(read_setting(os.path.join(config.path, "remote-settings.json"), "server-managed-cache"))
    user = read_setting(os.path.join(config.path, "settings.json"), "user")
    sources.append(user)
    for project in sorted(set(project_paths)):
        sources.append(read_setting(os.path.join(project, ".claude", "settings.json"), "project-shared"))
        sources.append(read_setting(os.path.join(project, ".claude", "settings.local.json"), "project-local"))
        root = _repo_root(project) if os.path.isdir(project) else None
        if root is not None and root != project:
            sources.append(read_setting(os.path.join(root, ".claude", "settings.local.json"), "project-local"))

    managed_values = [s.value for s in sources if s.kind.startswith(("managed", "server")) and s.value is not None]
    # When several managed sources set a value, the lowest is reported: the
    # precedence between them is not visible from files alone.
    managed_days = min(managed_values) if managed_values else None
    user_days = user.value
    if managed_days is not None:
        applicable = (managed_days,)
        may_pause = False
    else:
        values = {user_days if user_days is not None else DEFAULT_PERIOD_DAYS}
        values.update(s.value for s in sources if s.kind.startswith("project") and s.value is not None)
        applicable = tuple(sorted(values))
        may_pause = any(
            s.status in ("invalid", "unreadable") and not s.kind.startswith(("managed", "server"))
            for s in sources
        )
    marker, marker_status = _read_marker(config)
    return RetentionConfig(
        sources=tuple(sources),
        managed_days=managed_days,
        user_days=user_days,
        applicable_days=applicable,
        lowest_applicable_days=min(applicable),
        sweep_may_pause=may_pause,
        last_cleanup_marker=marker,
        last_cleanup_marker_status=marker_status,
    )


def transcript_risk(transcript_mtime: dt.datetime, now: dt.datetime, retention: RetentionConfig) -> str:
    """Classify a transcript against the lowest applicable period. Never returns a date."""
    age = now - transcript_mtime
    period = dt.timedelta(days=retention.lowest_applicable_days)
    if age > period:
        return "past-period"
    if age > period - dt.timedelta(days=APPROACHING_WINDOW_DAYS):
        return "approaching"
    return "within-period"


def worst_risk(levels: list[str]) -> str:
    return min(levels, key=RISK_LEVELS.index) if levels else "within-period"
