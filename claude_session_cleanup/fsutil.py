"""Read-only filesystem helpers that never follow symbolic links."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import stat


@dataclasses.dataclass(frozen=True)
class Artifact:
    rel_path: str  # relative to the config directory, "/"-separated
    kind: str
    size: int
    mtime_ns: int
    dev: int
    ino: int
    nlink: int
    is_dir: bool


@dataclasses.dataclass(frozen=True)
class Issue:
    code: str
    path: str  # relative to the config directory; never file contents


def relative(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def mtime_datetime(mtime_ns: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=dt.timezone.utc)


def owned_by_current_user(st: os.stat_result) -> bool:
    getuid = getattr(os, "getuid", None)
    return True if getuid is None else st.st_uid == getuid()


def _artifact(root: str, path: str, kind: str, st: os.stat_result) -> Artifact:
    return Artifact(
        rel_path=relative(root, path),
        kind=kind,
        size=0 if stat.S_ISDIR(st.st_mode) else st.st_size,
        mtime_ns=st.st_mtime_ns,
        dev=st.st_dev,
        ino=st.st_ino,
        nlink=st.st_nlink,
        is_dir=stat.S_ISDIR(st.st_mode),
    )


def collect(
    root: str, path: str, kind: str, *, kind_for_child=None
) -> tuple[list[Artifact], list[Issue]]:
    """Describe ``path`` (and, for a directory, everything under it) without following links.

    Missing paths yield nothing. Symbolic links, special files, foreign
    ownership, and unreadable entries are reported as issues and never
    traversed. ``kind_for_child(relative_parts)`` may refine the kind of
    entries below a directory.
    """
    artifacts: list[Artifact] = []
    issues: list[Issue] = []
    try:
        top = os.lstat(path)
    except FileNotFoundError:
        return artifacts, issues
    except OSError:
        return artifacts, [Issue("unreadable", relative(root, path))]

    stack: list[tuple[str, os.stat_result, tuple[str, ...]]] = [(path, top, ())]
    while stack:
        current, st, parts = stack.pop()
        rel = relative(root, current)
        if stat.S_ISLNK(st.st_mode):
            # Recorded (size 0, never followed) so callers can judge the link target.
            artifacts.append(dataclasses.replace(_artifact(root, current, "symlink", st), size=0))
            issues.append(Issue("symlink-in-session", rel))
            continue
        if not owned_by_current_user(st):
            issues.append(Issue("foreign-owner", rel))
        entry_kind = kind if not parts or kind_for_child is None else kind_for_child(parts)
        if stat.S_ISREG(st.st_mode):
            artifacts.append(_artifact(root, current, entry_kind, st))
            continue
        if not stat.S_ISDIR(st.st_mode):
            issues.append(Issue("non-regular-file", rel))
            continue
        artifacts.append(_artifact(root, current, entry_kind, st))
        try:
            with os.scandir(current) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            issues.append(Issue("unreadable", rel))
            continue
        children = []
        for name in names:
            child = os.path.join(current, name)
            try:
                child_st = os.lstat(child)
            except OSError:
                issues.append(Issue("unreadable", relative(root, child)))
                continue
            children.append((child, child_st, parts + (name,)))
        stack.extend(reversed(children))
    artifacts.sort(key=lambda item: item.rel_path)
    issues.sort(key=lambda item: (item.path, item.code))
    return artifacts, issues


def total_bytes(artifacts: list[Artifact] | tuple[Artifact, ...]) -> int:
    """Sum file sizes, counting hard-linked files once."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for item in artifacts:
        if item.is_dir:
            continue
        if item.nlink > 1:
            key = (item.dev, item.ino)
            if key in seen:
                continue
            seen.add(key)
        total += item.size
    return total


def read_small_file(path: str, limit: int, *, follow_symlinks: bool = False) -> bytes | None:
    """Read a small regular file; None if missing, too large, special, or unreadable.

    Session data is read without following a final symlink. Settings files,
    which users commonly symlink from dotfile repositories, may opt in.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if not follow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(limit + 1)[:limit]
    except OSError:
        return None
    finally:
        os.close(fd)
