"""Read-only scan of running Claude Code processes through ``/proc`` (Linux only).

The session registry (``sessions/<pid>.json``) is written by Claude Code
itself and can be missing, stale, or written by a process in another PID
namespace. Before anything is deleted, this module looks at the processes
themselves: every process of the current user whose executable or command
line is Claude Code is reported with its working directory, start time, the
session ids named on its command line, and the session files it holds open.

Nothing here sends a signal, launches a process, or writes a file. On any
platform other than Linux with a readable ``/proc``, the scan is reported as
unavailable and deletion stays unsupported.
"""

from __future__ import annotations

import dataclasses
import os
import stat

from .config import UUID_SEARCH_RE, nfc

_CLAUDE_EXE_NAMES = frozenset({"claude", "claude.exe"})
_SCRIPT_HOSTS = frozenset({"node", "bun", "deno"})


@dataclasses.dataclass(frozen=True)
class ClaudeProcess:
    pid: int
    start_time: str | None
    cwd: str | None  # None when the working directory could not be read
    config_dir: str | None  # None when the environment could not be read
    session_ids: tuple[str, ...]  # named on the command line or held open under projects/
    ancestor: bool  # this tool's own process chain (the invoking session)


@dataclasses.dataclass(frozen=True)
class ProcessScan:
    available: bool
    processes: tuple[ClaudeProcess, ...] = ()
    uninspectable: int = 0  # current-user processes whose executable and command line were unreadable


def _read(path: str, limit: int = 1 << 20) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return None


def _stat_fields(proc_root: str, pid: int) -> list[str] | None:
    data = _read(os.path.join(proc_root, str(pid), "stat"), 4096)
    if data is None:
        return None
    text = data.decode("ascii", errors="replace")
    if ")" not in text:
        return None
    return text.rsplit(")", 1)[1].split()


def start_time(proc_root: str, pid: int) -> str | None:
    fields = _stat_fields(proc_root, pid)
    return fields[19] if fields is not None and len(fields) > 19 else None  # field 22 of stat


def _parent(proc_root: str, pid: int) -> int | None:
    fields = _stat_fields(proc_root, pid)
    if fields is None or len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


def ancestors(proc_root: str, pid: int) -> frozenset[int]:
    """Return ``pid`` and every parent up to init."""
    chain: set[int] = set()
    current: int | None = pid
    while current is not None and current > 0 and current not in chain:
        chain.add(current)
        current = _parent(proc_root, current)
    return frozenset(chain)


def _is_claude(exe: str | None, argv: list[str]) -> bool:
    candidates = [exe] if exe else []
    candidates += argv[:1]
    for value in candidates:
        if os.path.basename(value) in _CLAUDE_EXE_NAMES:
            return True
        # Native installs run the versioned binary .../claude/versions/<version>.
        parts = value.replace("\\", "/").split("/")
        if len(parts) >= 3 and parts[-3] == "claude" and parts[-2] == "versions":
            return True
    host = os.path.basename(exe or (argv[0] if argv else ""))
    if host.split(".")[0] in _SCRIPT_HOSTS:
        for arg in argv[1:3]:
            normalized = arg.replace("\\", "/")
            if "@anthropic-ai/claude-code/" in normalized or os.path.basename(normalized) == "claude":
                return True
    return False


def _config_dir(environ: bytes | None) -> str | None:
    if environ is None:
        return None
    values = {}
    for item in environ.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key in (b"CLAUDE_CONFIG_DIR", b"HOME"):
            values[key] = os.fsdecode(value)
    if values.get(b"CLAUDE_CONFIG_DIR"):
        return nfc(os.path.normpath(values[b"CLAUDE_CONFIG_DIR"]))
    if values.get(b"HOME"):
        return nfc(os.path.normpath(os.path.join(values[b"HOME"], ".claude")))
    return None


def _open_session_ids(proc_root: str, pid: int, projects: str) -> tuple[set[str], bool]:
    """Session ids of files this process holds open under ``projects/``."""
    found: set[str] = set()
    directory = os.path.join(proc_root, str(pid), "fd")
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return found, True
    except OSError:
        return found, False
    prefix = projects.rstrip(os.sep) + os.sep
    for name in names:
        try:
            target = os.readlink(os.path.join(directory, name))
        except OSError:
            continue
        if target.startswith(prefix):
            found.update(match.group(0) for match in UUID_SEARCH_RE.finditer(target[len(prefix):].lower()))
    return found, True


def scan(
    projects: str,
    *,
    platform: str,
    proc_root: str = "/proc",
    own_pid: int | None = None,
    uid: int | None = None,
) -> ProcessScan:
    """List Claude Code processes of the current user. Read-only."""
    if not platform.startswith("linux") or not os.path.isdir(proc_root):
        return ProcessScan(available=False)
    own_pid = os.getpid() if own_pid is None else own_pid
    getuid = getattr(os, "getuid", None)
    uid = (getuid() if getuid else -1) if uid is None else uid
    own_chain = ancestors(proc_root, own_pid)
    processes: list[ClaudeProcess] = []
    uninspectable = 0
    try:
        names = sorted((name for name in os.listdir(proc_root) if name.isdigit()), key=int)
    except OSError:
        return ProcessScan(available=False)
    for name in names:
        pid = int(name)
        base = os.path.join(proc_root, name)
        try:
            st = os.stat(base)
        except OSError:
            continue  # exited during the scan
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or pid == own_pid:
            continue
        try:
            exe: str | None = os.readlink(os.path.join(base, "exe"))
        except OSError:
            exe = None  # kernel thread, zombie, exited, or unreadable
        cmdline = _read(os.path.join(base, "cmdline"))
        argv = [os.fsdecode(part) for part in cmdline.split(b"\0") if part] if cmdline else []
        if exe is None and cmdline is None:
            if _stat_fields(proc_root, pid) is not None:  # still running, yet unreadable
                uninspectable += 1
            continue
        if not _is_claude(exe, argv):
            continue
        try:
            cwd: str | None = nfc(os.readlink(os.path.join(base, "cwd")))
        except OSError:
            # Unreadable while alive: it could be working anywhere. Exited or
            # zombie processes (common for short-lived children) are skipped.
            fields = _stat_fields(proc_root, pid)
            if fields is None or fields[0] in ("Z", "X", "x"):
                continue
            cwd = None
        ids = {match.group(0) for arg in argv[1:] for match in UUID_SEARCH_RE.finditer(arg.lower())}
        held, _ = _open_session_ids(proc_root, pid, projects)
        processes.append(
            ClaudeProcess(
                pid=pid,
                start_time=start_time(proc_root, pid),
                cwd=cwd,
                config_dir=_config_dir(_read(os.path.join(base, "environ"))),
                session_ids=tuple(sorted(ids | held)),
                ancestor=pid in own_chain,
            )
        )
    return ProcessScan(available=True, processes=tuple(processes), uninspectable=uninspectable)


def pid_domain(platform: str, proc_root: str = "/proc", machine_id_path: str = "/etc/machine-id") -> str | None:
    """The PID namespace identity Claude Code writes as ``pidDomain`` in its registry on Linux."""
    if not platform.startswith("linux"):
        return None
    data = _read(machine_id_path, 256)
    try:
        namespace = os.readlink(os.path.join(proc_root, "self", "ns", "pid"))
    except OSError:
        return None
    if data is None:
        return None
    return f"linux:{data.decode('ascii', errors='replace').strip()}:{namespace}"
