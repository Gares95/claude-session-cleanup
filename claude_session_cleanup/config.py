"""Configuration-directory resolution and project identity rules.

Claude Code stores each session under ``projects/<name>/`` where ``<name>`` is
the session's starting directory with every non-alphanumeric character
replaced by ``-``. Names longer than 200 characters are truncated and given a
hash suffix whose algorithm is not stable across Claude Code builds. The
encoding is lossy (``/a-b`` and ``/a/b`` collide), so a storage directory name
alone never proves which project a session belongs to.
"""

from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
from typing import Mapping

from .errors import CleanupError

SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")
MAX_SANITIZED_LENGTH = 200
_HASH_SUFFIX_RE = re.compile(r"[0-9a-z]+\Z")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
UUID_SEARCH_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
PROJECT_DIR_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")

CASE_INSENSITIVE_PATHS = os.name == "nt"


def is_session_id(value: object) -> bool:
    """Return whether ``value`` is a canonical lowercase UUID string."""
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_recorded_path(value: object) -> str | None:
    """Normalize a path recorded in a transcript, or return None if unusable.

    Recorded paths are historical: they may no longer exist, so they are
    normalized lexically and never resolved against the filesystem.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    if not os.path.isabs(value):
        return None
    return nfc(os.path.normpath(value))


def paths_equal(left: str, right: str) -> bool:
    if CASE_INSENSITIVE_PATHS:
        return os.path.normcase(left) == os.path.normcase(right)
    return left == right


def canonical_project_path(path: str) -> str:
    """Resolve the invoking directory the way Claude Code does (realpath + NFC)."""
    if not os.path.isabs(path):
        raise CleanupError("the project directory must be an absolute path")
    try:
        resolved = os.path.realpath(path, strict=True)
    except OSError as exc:
        raise CleanupError(f"cannot resolve the project directory: {exc.strerror or exc}") from exc
    if not os.path.isdir(resolved):
        raise CleanupError("the project path is not a directory")
    return nfc(resolved)


def sanitize_path(path: str) -> str:
    """Return the storage-directory name for a short path, or the prefix of a long one."""
    sanitized = SANITIZE_RE.sub("-", path)
    return sanitized[:MAX_SANITIZED_LENGTH]


def project_dir_matches(dir_name: str, project_path: str) -> bool:
    """Return whether ``dir_name`` is a storage directory Claude Code could use for ``project_path``."""
    sanitized = SANITIZE_RE.sub("-", project_path)
    if CASE_INSENSITIVE_PATHS:
        sanitized = sanitized.lower()
        dir_name = dir_name.lower()
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return dir_name == sanitized
    prefix = sanitized[:MAX_SANITIZED_LENGTH] + "-"
    return dir_name.startswith(prefix) and _HASH_SUFFIX_RE.fullmatch(dir_name[len(prefix):]) is not None


@dataclasses.dataclass(frozen=True)
class ConfigDir:
    path: str
    source: str  # "--config-dir", "CLAUDE_CONFIG_DIR", or "default"

    @property
    def projects(self) -> str:
        return os.path.join(self.path, "projects")


def resolve_config_dir(
    environment: Mapping[str, str],
    explicit: str | None = None,
    *,
    home: str | None = None,
) -> ConfigDir:
    """Locate the Claude Code configuration directory without creating anything."""
    if explicit:
        raw, source = explicit, "--config-dir"
    elif environment.get("CLAUDE_CONFIG_DIR"):
        raw, source = environment["CLAUDE_CONFIG_DIR"], "CLAUDE_CONFIG_DIR"
    else:
        base = home if home is not None else os.path.expanduser("~")
        raw, source = os.path.join(base, ".claude"), "default"
    path = nfc(os.path.abspath(os.path.expanduser(raw)))
    if not os.path.isdir(path):
        raise CleanupError(f"no Claude Code configuration directory at {path}")
    return ConfigDir(path=path, source=source)
