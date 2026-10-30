"""Read-only discovery of Claude Code session units and root groups.

A *session unit* is one top-level transcript plus everything Claude Code keys
by that session id: the ``projects/<dir>/<id>/`` directory (subagent
transcripts, tool results, MCP task state, title sidecar), set-aside
transcript copies, and per-session directories at the top of the
configuration directory such as ``file-history/<id>/``.

A *root group* joins units that Claude Code links explicitly
(``continued-in`` records, written when a conversation continues in a
background session). Retention is decided per group.

Nothing here writes, renames, or launches a process.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import stat

from .config import (
    PROJECT_DIR_NAME_RE,
    UUID_SEARCH_RE,
    ConfigDir,
    is_session_id,
    project_dir_matches,
)
from .errors import CleanupError
from .fsutil import (
    Artifact,
    Issue,
    collect,
    mtime_datetime,
    owned_by_current_user,
    read_small_file,
    relative,
    total_bytes,
)
from .transcript import TranscriptSummary, summarize_transcript

# Per-session paths at the top of the configuration directory, keyed by id.
SESSION_KEYED_DIRS = (
    ("file-history", "file_history"),
    ("session-env", "session_env"),
    ("tasks", "task_list"),
    ("image-cache", "image_cache"),
    ("uploads", "uploads"),
    ("dev-mods", "dev_mods"),
)
SESSION_KEYED_FILES = (("debug", ".txt", "debug_log"),)
SHARED_PROJECT_ENTRIES = frozenset({"memory"})

_SESSION_DIR_KINDS = {
    "subagents": "subagent",
    "tool-results": "tool_result",
    "mcp-tasks": "mcp_task",
    "custom-title.json": "title_sidecar",
}

EXCLUDING_ISSUES = frozenset(
    {
        "symlink-in-session",
        "non-regular-file",
        "foreign-owner",
        "unreadable",
        "empty-transcript",
        "unparseable-transcript",
        "duplicate-session-id",
    }
)


@dataclasses.dataclass(frozen=True)
class SessionUnit:
    session_id: str
    project_dir_name: str
    identity: str  # "verified", "unverified", or "missing"
    project_path: str | None  # set only when identity is verified
    recorded_cwds: tuple[str, ...]
    title: str | None
    title_source: str | None
    created_at: dt.datetime | None
    transcript_mtime: dt.datetime
    transcript_bytes: int
    content_last_at: dt.datetime | None
    last_activity: dt.datetime
    last_activity_source: str
    artifacts: tuple[Artifact, ...]
    unit_bytes: int
    subagent_count: int
    entrypoints: tuple[str, ...]
    claude_versions: tuple[str, ...]
    continued_in: tuple[str, ...]
    shared_data_links: tuple[str, ...]  # sessions whose data this unit symlinks to
    issues: tuple[Issue, ...]
    warnings: tuple[str, ...]
    unknown_artifacts: tuple[str, ...]

    @property
    def excluded_issue_codes(self) -> tuple[str, ...]:
        return tuple(sorted({item.code for item in self.issues if item.code in EXCLUDING_ISSUES}))

    def bytes_by_kind(self) -> dict[str, int]:
        totals: dict[str, list[Artifact]] = {}
        for item in self.artifacts:
            if not item.is_dir and item.kind != "symlink":
                totals.setdefault(item.kind, []).append(item)
        return {kind: total_bytes(items) for kind, items in sorted(totals.items())}


@dataclasses.dataclass(frozen=True)
class SessionGroup:
    group_id: str  # the root session id
    members: tuple[SessionUnit, ...]  # root first, then by depth, activity (newest first), id
    last_activity: dt.datetime
    last_activity_session: str
    missing_links: tuple[str, ...]
    parents: tuple[tuple[str, str], ...] = ()  # (member, parent member)
    ancestry_ambiguous: bool = False

    def parent_of(self, session_id: str) -> str | None:
        return dict(self.parents).get(session_id)

    @property
    def bytes(self) -> int:
        return sum(unit.unit_bytes for unit in self.members)


@dataclasses.dataclass(frozen=True)
class DiscoveryWarning:
    code: str
    path: str


@dataclasses.dataclass(frozen=True)
class Discovery:
    config: ConfigDir
    units: tuple[SessionUnit, ...]
    groups: tuple[SessionGroup, ...]
    warnings: tuple[DiscoveryWarning, ...]


def _session_dir_kind(parts: tuple[str, ...]) -> str:
    return _SESSION_DIR_KINDS.get(parts[0], "session_data_other")


def _identity(dir_name: str, summary: TranscriptSummary | None) -> tuple[str, str | None, tuple[str, ...]]:
    """Verify project identity from recorded working directories.

    A unit is *verified* when a recorded cwd encodes to exactly the storage
    directory that holds it. The last recorded cwd is preferred because
    ``/cd`` moves a session into the new directory's storage.
    """
    if summary is None:
        return "missing", None, ()
    recorded = tuple(dict.fromkeys(p for p in (summary.first_cwd, summary.last_cwd) if p))
    if not recorded:
        return "missing", None, ()
    for candidate in (summary.last_cwd, summary.first_cwd):
        if candidate and project_dir_matches(dir_name, candidate):
            return "verified", candidate, recorded
    return "unverified", None, recorded


def _comparison_path(path: str) -> str:
    """Normalize equivalent Windows extended paths for lexical containment."""
    normalized = os.path.normpath(path)
    if os.name != "nt":
        return normalized
    if normalized.startswith("\\\\?\\UNC\\"):
        return "\\\\" + normalized[8:]
    if normalized.startswith("\\\\?\\"):
        return normalized[4:]
    return normalized


def _session_link_target(config: ConfigDir, link: str) -> str | None:
    """Return the session id whose ``projects/<dir>/<id>/`` data a symlink points into.

    Claude Code links a predecessor's subagent transcripts into a background
    continuation's ``subagents/`` directory. Such links are read with
    ``readlink`` only and accepted solely when the target stays inside another
    session's data directory under ``projects/``; any other target is unsafe.
    """
    try:
        target = os.readlink(link)
    except OSError:
        return None
    absolute = _comparison_path(os.path.join(os.path.dirname(link), target))
    projects = _comparison_path(config.projects)
    try:
        rel = os.path.relpath(absolute, projects)
    except ValueError:
        return None
    parts = rel.split(os.sep)
    if rel.startswith(os.pardir) or os.path.isabs(rel) or len(parts) < 3:
        return None
    if not PROJECT_DIR_NAME_RE.fullmatch(parts[0]) or not is_session_id(parts[1]):
        return None
    for depth in (1, 2):
        try:
            if stat.S_ISLNK(os.lstat(os.path.join(config.projects, *parts[:depth])).st_mode):
                return None
        except FileNotFoundError:
            break
        except OSError:
            return None
    return parts[1]


def _clean_title(value: str) -> str:
    return " ".join(value.split())


def _title(summary: TranscriptSummary | None, sidecar: str | None) -> tuple[str | None, str | None]:
    if summary is not None and summary.custom_title and summary.custom_title.strip():
        return _clean_title(summary.custom_title), "custom-title"
    if sidecar and sidecar.strip():
        return _clean_title(sidecar), "custom-title-sidecar"
    if summary is not None:
        for value, source in (
            (summary.ai_title, "ai-title"),
            (summary.first_prompt, "first-prompt"),
            (summary.last_prompt, "last-prompt"),
        ):
            if value and value.strip():
                return _clean_title(value), source
    return None, None


def _read_sidecar_title(path: str) -> str | None:
    data = read_small_file(path, 64 << 10)
    if data is None:
        return None
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    title = value.get("customTitle") if isinstance(value, dict) else None
    return title[:500] if isinstance(title, str) else None


def _scan_unknown_artifacts(config: ConfigDir, session_ids: set[str], known: set[str]) -> dict[str, list[str]]:
    """Find names containing a session id one level below each top-level directory.

    Any such entry that is not a known per-session path means this release
    cannot account for everything Claude Code keeps for that session.
    """
    found: dict[str, list[str]] = {}
    try:
        with os.scandir(config.path) as entries:
            top_names = sorted(entry.name for entry in entries)
    except OSError:
        return found
    for top in top_names:
        if top == "projects":
            continue
        top_path = os.path.join(config.path, top)
        candidates = [top]
        try:
            if stat.S_ISDIR(os.lstat(top_path).st_mode):
                with os.scandir(top_path) as entries:
                    candidates = [f"{top}/{entry.name}" for entry in sorted(entries, key=lambda e: e.name)]
        except OSError:
            continue
        for rel in candidates:
            for match in UUID_SEARCH_RE.finditer(rel.lower()):
                session_id = match.group(0)
                if session_id in session_ids and rel not in known:
                    found.setdefault(session_id, []).append(rel)
    return found


def _build_unit(
    config: ConfigDir,
    project_dir: str,
    session_id: str,
    variants: list[str],
) -> tuple[SessionUnit, set[str]]:
    dir_name = os.path.basename(project_dir)
    transcript_path = os.path.join(project_dir, f"{session_id}.jsonl")
    known: set[str] = set()
    artifacts: list[Artifact] = []
    issues: list[Issue] = []
    warnings: list[str] = []

    def add(path: str, kind: str, **kwargs) -> None:
        known.add(relative(config.path, path))
        found, problems = collect(config.path, path, kind, **kwargs)
        artifacts.extend(found)
        issues.extend(problems)

    add(transcript_path, "transcript")
    add(os.path.join(project_dir, session_id), "session_dir", kind_for_child=_session_dir_kind)
    for name in variants:
        add(os.path.join(project_dir, name), "transcript_variant")
    for top, kind in SESSION_KEYED_DIRS:
        add(os.path.join(config.path, top, session_id), kind)
    for top, suffix, kind in SESSION_KEYED_FILES:
        add(os.path.join(config.path, top, session_id + suffix), kind)

    session_prefix = f"projects/{dir_name}/{session_id}/"
    shared_links: set[str] = set()
    kept_issues: list[Issue] = []
    for item in issues:
        if item.code == "symlink-in-session" and item.path.startswith(session_prefix):
            target = _session_link_target(config, os.path.join(config.path, *item.path.split("/")))
            if target is not None:
                if target != session_id:
                    shared_links.add(target)
                continue
        kept_issues.append(item)
    issues = kept_issues

    transcript_st = os.lstat(transcript_path)
    summary: TranscriptSummary | None = None
    if stat.S_ISREG(transcript_st.st_mode) and owned_by_current_user(transcript_st):
        if transcript_st.st_size == 0:
            issues.append(Issue("empty-transcript", relative(config.path, transcript_path)))
        else:
            summary = summarize_transcript(transcript_path, session_id)
            if summary is None:
                issues.append(Issue("unreadable", relative(config.path, transcript_path)))
            elif summary.parsed_records == 0:
                issues.append(Issue("unparseable-transcript", relative(config.path, transcript_path)))
                summary = None

    if summary is not None:
        if summary.parse_errors:
            warnings.append("transcript-parse-errors")
        if summary.oversized_lines:
            warnings.append("transcript-oversized-lines")
        if summary.incomplete_last_line:
            warnings.append("transcript-incomplete-last-line")
        if summary.foreign_records:
            warnings.append("transcript-foreign-session-records")
        if summary.sidechain_first:
            warnings.append("transcript-starts-as-sidechain")

    identity, project_path, recorded = _identity(dir_name, summary)
    sidecar = None
    if not any(item.code == "symlink-in-session" for item in issues):
        sidecar = _read_sidecar_title(os.path.join(project_dir, session_id, "custom-title.json"))
    title, title_source = _title(summary, sidecar)

    # Last activity is the newest evidence of any kind, so that a recently
    # written subagent, tool result, or checkpoint protects an older parent.
    transcript_mtime = mtime_datetime(transcript_st.st_mtime_ns)
    evidence: list[tuple[dt.datetime, str]] = [(transcript_mtime, "transcript-mtime")]
    for item in artifacts:
        if item.kind != "transcript":
            evidence.append((mtime_datetime(item.mtime_ns), f"{item.kind}-mtime"))
    if summary is not None and summary.content_last_at is not None:
        evidence.append((summary.content_last_at, "content-timestamp"))
    last_activity, source = max(evidence, key=lambda pair: (pair[0], pair[1]))

    unit = SessionUnit(
        session_id=session_id,
        project_dir_name=dir_name,
        identity=identity,
        project_path=project_path,
        recorded_cwds=recorded,
        title=title,
        title_source=title_source,
        created_at=summary.created_at if summary else None,
        transcript_mtime=transcript_mtime,
        transcript_bytes=transcript_st.st_size if stat.S_ISREG(transcript_st.st_mode) else 0,
        content_last_at=summary.content_last_at if summary else None,
        last_activity=last_activity,
        last_activity_source=source,
        artifacts=tuple(artifacts),
        unit_bytes=total_bytes(artifacts),
        subagent_count=sum(
            1 for item in artifacts
            if item.kind == "subagent" and not item.is_dir and item.rel_path.endswith(".jsonl")
        ),
        entrypoints=summary.entrypoints if summary else (),
        claude_versions=summary.versions if summary else (),
        continued_in=summary.continued_in if summary else (),
        shared_data_links=tuple(sorted(shared_links)),
        issues=tuple(sorted(set(issues), key=lambda item: (item.path, item.code))),
        warnings=tuple(sorted(set(warnings))),
        unknown_artifacts=(),
    )
    return unit, known


def build_groups(units: tuple[SessionUnit, ...]) -> tuple[tuple[SessionGroup, ...], tuple[DiscoveryWarning, ...]]:
    """Join units linked by Claude Code into root groups.

    ``continued-in`` points from an origin to its continuation; a shared-data
    symlink points from a continuation back to its origin. Either way the
    continuation's parent is the origin. A group whose links do not form a
    single tree (several origins, conflicting parents, unreachable members)
    is marked ancestry-ambiguous.
    """
    by_id = {unit.session_id: unit for unit in units}
    union = {session_id: session_id for session_id in by_id}

    def find(item: str) -> str:
        while union[item] != item:
            union[item] = union[union[item]]
            item = union[item]
        return item

    warnings: list[DiscoveryWarning] = []
    missing: dict[str, list[str]] = {}
    parents: dict[str, set[str]] = {}
    for unit in units:
        edges = [(target, unit.session_id, target) for target in unit.continued_in]
        edges += [(target, target, unit.session_id) for target in unit.shared_data_links]
        for target, parent, child in edges:
            if target not in by_id:
                missing.setdefault(unit.session_id, []).append(target)
                continue
            parents.setdefault(child, set()).add(parent)
            left, right = find(unit.session_id), find(target)
            if left != right:
                union[max(left, right)] = min(left, right)

    components: dict[str, list[SessionUnit]] = {}
    for session_id in sorted(by_id):
        components.setdefault(find(session_id), []).append(by_id[session_id])

    groups: list[SessionGroup] = []
    for members in components.values():
        ids = {unit.session_id for unit in members}
        roots = sorted(session_id for session_id in ids if session_id not in parents)
        root_id = roots[0] if roots else min(ids)
        ambiguous = len(roots) != 1 or any(len(parents[i]) > 1 for i in ids if i in parents)
        parent_of = {child: min(found) for child, found in parents.items() if child in ids}
        depth = {root_id: 0}
        frontier = [root_id]
        while frontier:
            current = frontier.pop()
            for child, parent in parent_of.items():
                if parent == current and child not in depth:
                    depth[child] = depth[current] + 1
                    frontier.append(child)
        if len(depth) != len(ids):
            ambiguous = True
        ordered = sorted(
            members,
            key=lambda unit: (
                depth.get(unit.session_id, len(ids)),
                -unit.last_activity.timestamp(),
                unit.session_id,
            ),
        )
        newest = max(ordered, key=lambda unit: (unit.last_activity, unit.session_id))
        group_missing = sorted({target for unit in ordered for target in missing.get(unit.session_id, ())})
        for target in group_missing:
            warnings.append(DiscoveryWarning("linked-session-not-found", target))
        groups.append(
            SessionGroup(
                group_id=root_id,
                members=tuple(ordered),
                last_activity=newest.last_activity,
                last_activity_session=newest.session_id,
                missing_links=tuple(group_missing),
                parents=tuple(sorted(parent_of.items())),
                ancestry_ambiguous=ambiguous,
            )
        )
    groups.sort(key=lambda group: group.group_id)
    return tuple(groups), tuple(warnings)


def discover(config: ConfigDir) -> Discovery:
    """Scan ``projects/`` and build session units and groups without modifying anything."""
    warnings: list[DiscoveryWarning] = []
    projects = config.projects
    try:
        projects_st = os.lstat(projects)
    except FileNotFoundError:
        return Discovery(config, (), (), (DiscoveryWarning("no-projects-directory", "projects"),))
    if stat.S_ISLNK(projects_st.st_mode) or not stat.S_ISDIR(projects_st.st_mode):
        raise CleanupError("projects/ is a symbolic link or not a directory; refusing to inspect it")

    try:
        with os.scandir(projects) as entries:
            project_names = sorted(entry.name for entry in entries)
    except OSError as exc:
        raise CleanupError(f"cannot list projects/: {exc.strerror or exc}") from exc

    units: list[SessionUnit] = []
    known: set[str] = set()
    for name in project_names:
        project_dir = os.path.join(projects, name)
        rel_dir = f"projects/{name}"
        try:
            st = os.lstat(project_dir)
        except OSError:
            warnings.append(DiscoveryWarning("unreadable-project-directory", rel_dir))
            continue
        if stat.S_ISLNK(st.st_mode):
            warnings.append(DiscoveryWarning("symlinked-project-directory", rel_dir))
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        try:
            with os.scandir(project_dir) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            warnings.append(DiscoveryWarning("unreadable-project-directory", rel_dir))
            continue

        transcripts: list[str] = []
        session_dirs: set[str] = set()
        variants: dict[str, list[str]] = {}
        for entry_name in names:
            if entry_name in SHARED_PROJECT_ENTRIES:
                continue
            stem = entry_name[:36]
            if not is_session_id(stem):
                if UUID_SEARCH_RE.search(entry_name.lower()):
                    warnings.append(DiscoveryWarning("unrecognized-session-entry", f"{rel_dir}/{entry_name}"))
                continue
            rest = entry_name[36:]
            if rest == ".jsonl":
                transcripts.append(stem)
            elif rest == "":
                session_dirs.add(stem)
            elif rest.startswith("."):
                variants.setdefault(stem, []).append(entry_name)
            else:
                warnings.append(DiscoveryWarning("unrecognized-session-entry", f"{rel_dir}/{entry_name}"))

        transcript_ids = set(transcripts)
        for session_id in sorted(session_dirs - transcript_ids):
            warnings.append(DiscoveryWarning("session-data-without-transcript", f"{rel_dir}/{session_id}"))
        for session_id in sorted(set(variants) - transcript_ids):
            warnings.append(DiscoveryWarning("transcript-variant-without-transcript", f"{rel_dir}/{session_id}"))
        for session_id in transcripts:
            try:
                unit, unit_known = _build_unit(config, project_dir, session_id, variants.get(session_id, []))
            except FileNotFoundError:
                # Removed while scanning, for example by Claude Code's own cleanup.
                warnings.append(DiscoveryWarning("session-vanished-during-scan", f"{rel_dir}/{session_id}.jsonl"))
                continue
            units.append(unit)
            known |= unit_known

    ids = [unit.session_id for unit in units]
    duplicates = sorted({session_id for session_id in ids if ids.count(session_id) > 1})
    for session_id in duplicates:
        warnings.append(DiscoveryWarning("session-id-in-several-projects", session_id))

    unknown = _scan_unknown_artifacts(config, set(ids), known)
    final_units = []
    for unit in units:
        extra = list(unit.unknown_artifacts) + unknown.get(unit.session_id, [])
        issues = unit.issues
        # A duplicated id, or a link to one, cannot be attributed to a single session.
        ambiguous = sorted(
            {unit.session_id} & set(duplicates)
            | {target for target in unit.continued_in + unit.shared_data_links if target in duplicates}
        )
        if ambiguous:
            extra_issues = tuple(Issue("duplicate-session-id", session_id) for session_id in ambiguous)
            issues = tuple(sorted(issues + extra_issues, key=lambda i: (i.path, i.code)))
        final_units.append(dataclasses.replace(unit, unknown_artifacts=tuple(sorted(extra)), issues=issues))
    units_tuple = tuple(sorted(final_units, key=lambda unit: (unit.session_id, unit.project_dir_name)))

    groups, group_warnings = build_groups(tuple(u for u in units_tuple if u.session_id not in duplicates))
    # Units sharing an id with another project cannot be grouped safely; each becomes its own group.
    duplicate_groups = tuple(
        SessionGroup(unit.session_id, (unit,), unit.last_activity, unit.session_id, ())
        for unit in units_tuple
        if unit.session_id in duplicates
    )
    all_groups = tuple(sorted(groups + duplicate_groups, key=lambda g: (g.group_id, g.members[0].project_dir_name)))
    warnings.extend(group_warnings)
    warnings.sort(key=lambda item: (item.code, item.path))
    return Discovery(config, units_tuple, all_groups, tuple(warnings))
