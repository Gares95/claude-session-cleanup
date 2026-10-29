"""Shared test support: isolation guard and synthetic Claude Code config directories.

Every test case derives from ``IsolatedTestCase``. It fails the test if code
touches the real ``~/.claude`` or ``~/.claude.json``, launches a process, or
sends a signal, and it points ``HOME`` at a temporary directory.
"""

from __future__ import annotations

import builtins
import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

from claude_session_cleanup import cli

NOW = dt.datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
NOW_ARG = "2026-10-25T12:00:00Z"

_REAL_HOME = os.path.expanduser("~")
_FORBIDDEN = tuple(
    {os.path.abspath(p) for p in (
        os.path.join(_REAL_HOME, ".claude"),
        os.path.join(_REAL_HOME, ".claude.json"),
    )}
    | {os.path.realpath(p) for p in (
        os.path.join(_REAL_HOME, ".claude"),
        os.path.join(_REAL_HOME, ".claude.json"),
    )}
)


_REAL_PROCESS_FUNCTIONS = {
    (module, name): getattr(module, name)
    for module, name in [(subprocess, "Popen"), (os, "system"), (os, "kill")]
    + [(os, n) for n in ("posix_spawn", "posix_spawnp", "fork", "execv", "execve") if hasattr(os, n)]
}


def allow_processes():
    """Temporarily restore process functions (for tests that run the script itself)."""
    import contextlib

    stack = contextlib.ExitStack()
    for (module, name), function in _REAL_PROCESS_FUNCTIONS.items():
        stack.enter_context(mock.patch.object(module, name, function))
    return stack


class GuardViolation(AssertionError):
    pass


def _check(path) -> None:
    if isinstance(path, int):
        return
    try:
        text = os.path.abspath(os.fsdecode(path))
    except TypeError:
        return
    for forbidden in _FORBIDDEN:
        if text == forbidden or text.startswith(forbidden + os.sep):
            raise GuardViolation(f"test touched the real Claude Code data: {text}")


def _wrap(function):
    def guarded(path, *args, **kwargs):
        _check(path)
        return function(path, *args, **kwargs)

    return guarded


def _forbid(name):
    def refuse(*args, **kwargs):
        raise GuardViolation(f"test attempted {name}")

    return refuse


class IsolatedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="csc-test-")
        self.tmp = os.path.realpath(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        patches = [
            mock.patch.dict(os.environ, {"HOME": home}, clear=False),
            mock.patch.object(os, "lstat", _wrap(os.lstat)),
            mock.patch.object(os, "stat", _wrap(os.stat)),
            mock.patch.object(os, "scandir", _wrap(os.scandir)),
            mock.patch.object(os, "listdir", _wrap(os.listdir)),
            mock.patch.object(os, "open", _wrap(os.open)),
            mock.patch.object(builtins, "open", _wrap(builtins.open)),
            mock.patch.object(subprocess, "Popen", _forbid("subprocess.Popen")),
            mock.patch.object(os, "system", _forbid("os.system")),
            mock.patch.object(os, "kill", _forbid("os.kill")),
        ]
        for name in ("posix_spawn", "posix_spawnp", "fork", "execv", "execve"):
            if hasattr(os, name):
                patches.append(mock.patch.object(os, name, _forbid(f"os.{name}")))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE"):
            if key in os.environ:
                original = os.environ.pop(key)
                self.addCleanup(os.environ.__setitem__, key, original)
        self.factory = ConfigFactory(os.path.join(self.tmp, "claude-config"), os.path.join(self.tmp, "work"))

    def run_cli(self, *args: str, cwd: str | None = None, env: dict | None = None,
                managed_dirs: tuple[str, ...] = (), liveness_options: dict | None = None) -> tuple[int, str, str]:
        environment = {"HOME": os.environ["HOME"], "CLAUDE_CONFIG_DIR": self.factory.config}
        environment.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        code = cli.run(
            ["--now", NOW_ARG, *args],
            environment=environment,
            cwd=cwd or self.factory.default_project,
            stdout=out,
            stderr=err,
            tz=dt.timezone.utc,
            managed_dirs=managed_dirs,
            liveness_options=liveness_options or {"platform": "test", "kill": _forbid("kill")},
        )
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *args: str, **kwargs) -> dict:
        code, out, err = self.run_cli(*args, "--json", **kwargs)
        self.assertEqual(0, code, err)
        return json.loads(out)


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.symlink(directory, os.path.join(directory, "link"))
        except (OSError, NotImplementedError, AttributeError):
            return False
    return True


requires_symlinks = unittest.skipUnless(_symlinks_supported(), "symbolic links are not available")


def session_uuid(number: int) -> str:
    return str(uuid.UUID(int=number))


def set_age(path: str, age: dt.timedelta) -> None:
    stamp = (NOW - age).timestamp()
    if os.utime in os.supports_follow_symlinks:
        os.utime(path, (stamp, stamp), follow_symlinks=False)
    else:  # Windows: utime cannot target a link itself
        os.utime(path, (stamp, stamp))


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ConfigFactory:
    """Build synthetic Claude Code configuration directories.

    Record shapes follow the keys observed in Claude Code 2.1.310 transcripts;
    no real transcript content is used.
    """

    def __init__(self, config: str, workspace: str) -> None:
        self.config = config
        self.workspace = workspace
        os.makedirs(os.path.join(config, "projects"))
        os.makedirs(workspace)
        self.default_project = self.project("repo")
        self._counter = 0

    def project(self, *parts: str) -> str:
        path = os.path.join(self.workspace, *parts)
        os.makedirs(path, exist_ok=True)
        return os.path.realpath(path)

    @staticmethod
    def storage_name(project_path: str) -> str:
        from claude_session_cleanup.config import SANITIZE_RE

        return SANITIZE_RE.sub("-", project_path)

    def next_id(self) -> str:
        self._counter += 1
        return session_uuid(self._counter)

    def storage_dir(self, project_path: str | None = None, name: str | None = None) -> str:
        directory = os.path.join(self.config, "projects", name or self.storage_name(project_path or self.default_project))
        os.makedirs(directory, exist_ok=True)
        return directory

    def records(self, session_id: str, *, cwd, last_cwd, content_time, title, ai_title, prompt,
                version, entrypoint, continued_in, extra) -> list[dict]:
        first = str(uuid.uuid4())
        records: list[dict] = [{"type": "permission-mode", "permissionMode": "default", "sessionId": session_id}]
        if prompt is not None:
            records.append({
                "type": "user", "uuid": first, "parentUuid": None, "sessionId": session_id,
                "cwd": cwd, "timestamp": iso(content_time), "isSidechain": False,
                "userType": "external", "entrypoint": entrypoint, "version": version,
                "message": {"role": "user", "content": prompt},
            })
            records.append({
                "type": "assistant", "uuid": str(uuid.uuid4()), "parentUuid": first, "sessionId": session_id,
                "cwd": last_cwd, "timestamp": iso(content_time), "isSidechain": False,
                "entrypoint": entrypoint, "version": version,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            })
        if ai_title is not None:
            records.append({"type": "ai-title", "aiTitle": ai_title, "sessionId": session_id})
        if title is not None:
            records.append({"type": "custom-title", "customTitle": title, "sessionId": session_id})
        for target in continued_in:
            records.append({"type": "continued-in", "continuedInSessionId": target,
                            "sessionId": session_id, "timestamp": iso(content_time)})
        records.extend(extra)
        return records

    def session(
        self,
        project_path: str | None = None,
        *,
        session_id: str | None = None,
        age: dt.timedelta = dt.timedelta(days=0),
        content_age: dt.timedelta | None = None,
        cwd: str | None = None,
        last_cwd: str | None = None,
        storage_name: str | None = None,
        title: str | None = None,
        ai_title: str | None = "Synthetic session",
        prompt: str | None = "synthetic prompt",
        subagents: tuple[tuple[str, dt.timedelta], ...] = (),
        tool_results: tuple[tuple[str, dt.timedelta], ...] = (),
        file_history: dt.timedelta | None = None,
        session_env: dt.timedelta | None = None,
        continued_in: tuple[str, ...] = (),
        extra: tuple[dict, ...] = (),
        raw: bytes | None = None,
        version: str = "2.1.310",
        entrypoint: str = "cli",
    ) -> str:
        project_path = project_path or self.default_project
        session_id = session_id or self.next_id()
        directory = self.storage_dir(project_path, storage_name)
        transcript = os.path.join(directory, f"{session_id}.jsonl")
        content_time = NOW - (content_age if content_age is not None else age)
        if raw is None:
            lines = self.records(
                session_id,
                cwd=cwd if cwd is not None else project_path,
                last_cwd=last_cwd if last_cwd is not None else (cwd if cwd is not None else project_path),
                content_time=content_time, title=title, ai_title=ai_title, prompt=prompt,
                version=version, entrypoint=entrypoint, continued_in=continued_in, extra=list(extra),
            )
            raw = ("\n".join(json.dumps(line) for line in lines) + "\n").encode()
        with open(transcript, "wb") as handle:
            handle.write(raw)
        session_dir = os.path.join(directory, session_id)
        touched_dirs = []
        for name, entry_age in subagents:
            path = os.path.join(session_dir, "subagents", f"agent-{name}.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(json.dumps({"type": "user", "isSidechain": True, "sessionId": session_id,
                                         "timestamp": iso(NOW - entry_age)}) + "\n")
            with open(path[:-6] + ".meta.json", "w") as handle:
                handle.write(json.dumps({"agentType": "general-purpose", "spawnDepth": 1}))
            set_age(path, entry_age)
            set_age(path[:-6] + ".meta.json", entry_age)
            touched_dirs.append(os.path.dirname(path))
        for name, entry_age in tool_results:
            path = os.path.join(session_dir, "tool-results", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("x" * 10)
            set_age(path, entry_age)
            touched_dirs.append(os.path.dirname(path))
        for top, entry_age in (("file-history", file_history), ("session-env", session_env)):
            if entry_age is None:
                continue
            path = os.path.join(self.config, top, session_id)
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "snapshot@v1"), "w") as handle:
                handle.write("y" * 5)
            set_age(os.path.join(path, "snapshot@v1"), entry_age)
            set_age(path, entry_age)
        # Directories get the age of their newest content, or the transcript's.
        for path in sorted(set(touched_dirs), reverse=True):
            newest = max(os.lstat(os.path.join(path, n)).st_mtime for n in os.listdir(path))
            os.utime(path, (newest, newest))
        if os.path.isdir(session_dir):
            newest = max(os.lstat(os.path.join(session_dir, n)).st_mtime for n in os.listdir(session_dir))
            os.utime(session_dir, (newest, newest))
        set_age(transcript, age)
        return session_id

    def transcript_path(self, session_id: str, project_path: str | None = None, storage_name: str | None = None) -> str:
        return os.path.join(self.storage_dir(project_path, storage_name), f"{session_id}.jsonl")

    def registry(self, pid: int, session_id: str, proc_start: str | None = "4242") -> str:
        directory = os.path.join(self.config, "sessions")
        os.makedirs(directory, exist_ok=True)
        record = {"pid": pid, "sessionId": session_id, "cwd": self.default_project, "kind": "interactive"}
        if proc_start is not None:
            record["procStart"] = proc_start
        path = os.path.join(directory, f"{pid}.json")
        with open(path, "w") as handle:
            json.dump(record, handle)
        return path

    def job(self, short: str, session_id: str, resume: str | None = None) -> None:
        directory = os.path.join(self.config, "jobs", short)
        os.makedirs(directory, exist_ok=True)
        record = {"state": "done", "sessionId": session_id}
        if resume:
            record["resumeSessionId"] = resume
        with open(os.path.join(directory, "state.json"), "w") as handle:
            json.dump(record, handle)

    def write_json(self, path: str, value) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump(value, handle)
        return path

    def user_settings(self, value) -> str:
        return self.write_json(os.path.join(self.config, "settings.json"), value)


def snapshot_tree(root: str) -> dict:
    """Names, types, sizes, and mtimes of everything under root (no content)."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = os.path.join(directory, name)
            st = os.lstat(path)
            result[os.path.relpath(path, root)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return result
