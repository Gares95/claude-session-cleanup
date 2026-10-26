"""Tolerant, read-only extraction of session metadata from a JSONL transcript.

The transcript format is internal to Claude Code and changes between
versions. This module therefore extracts only a few fields, never retains
message content beyond a display title, and reports what it could not read
instead of guessing.

One streaming pass parses the first records and every short line that carries
a metadata marker (titles, continuation links). A bounded read of the end of
the file supplies the latest working directory and timestamps.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import stat
from typing import Any

from .config import is_session_id, normalize_recorded_path

CHUNK_SIZE = 1 << 20
MAX_LINE = 1 << 20  # longer lines are content, never metadata
MAX_METADATA_LINE = 64 << 10
HEAD_RECORDS = 100
TAIL_BYTES = 256 << 10
MAX_LABELS = 16

_MARKERS = (b'"custom-title"', b'"ai-title"', b'"last-prompt"', b'"continued-in"')
_SKIP_PROMPT_RE = re.compile(
    r"^\s*(?:<local-command-|<command-name>|<command-message>|<command-args>|"
    r"<session-start-hook>|<system-reminder>|<ide_opened_file>|<ide_selection>|"
    r"<tick>|<goal>|\[Request interrupted by user)"
)


@dataclasses.dataclass(frozen=True)
class TranscriptSummary:
    first_cwd: str | None
    last_cwd: str | None
    created_at: dt.datetime | None
    content_last_at: dt.datetime | None
    custom_title: str | None
    ai_title: str | None
    last_prompt: str | None
    first_prompt: str | None
    entrypoints: tuple[str, ...]
    versions: tuple[str, ...]
    continued_in: tuple[str, ...]
    sidechain_first: bool
    parsed_records: int
    parse_errors: int
    oversized_lines: int
    foreign_records: int
    incomplete_last_line: bool


class _State:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.first_cwd: str | None = None
        self.last_cwd: str | None = None
        self.created_at: dt.datetime | None = None
        self.content_last_at: dt.datetime | None = None
        self.custom_title: str | None = None
        self.ai_title: str | None = None
        self.last_prompt: str | None = None
        self.first_prompt: str | None = None
        self.entrypoints: set[str] = set()
        self.versions: set[str] = set()
        self.continued_in: list[str] = []
        self.sidechain_first: bool | None = None
        self.parsed = 0
        self.parse_errors = 0
        self.oversized = 0
        self.foreign = 0
        self.incomplete_last_line = False


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _label(value: Any) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 64 and value.isprintable():
        return value
    return None


def _text_of(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                return block["text"]
    return None


def _decode(line: bytes, state: _State) -> dict | None:
    try:
        record = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        state.parse_errors += 1
        return None
    if not isinstance(record, dict):
        state.parse_errors += 1
        return None
    state.parsed += 1
    return record


def _is_foreign(record: dict, state: _State) -> bool:
    session_id = record.get("sessionId")
    if isinstance(session_id, str) and session_id != state.session_id:
        state.foreign += 1
        return True
    return False


def _absorb_common(record: dict, state: _State, *, from_tail: bool) -> None:
    cwd = normalize_recorded_path(record.get("cwd"))
    if cwd is not None:
        if state.first_cwd is None and not from_tail:
            state.first_cwd = cwd
        if from_tail:
            state.last_cwd = cwd
    timestamp = parse_timestamp(record.get("timestamp"))
    if timestamp is not None:
        if not from_tail and state.created_at is None:
            state.created_at = timestamp
        if state.content_last_at is None or timestamp > state.content_last_at:
            state.content_last_at = timestamp
    for key, bucket in (("entrypoint", state.entrypoints), ("version", state.versions)):
        label = _label(record.get(key))
        if label is not None and len(bucket) < MAX_LABELS:
            bucket.add(label)


def _absorb_head(record: dict, state: _State) -> None:
    if state.sidechain_first is None:
        state.sidechain_first = record.get("isSidechain") is True
    _absorb_common(record, state, from_tail=False)
    if (
        state.first_prompt is None
        and record.get("type") == "user"
        and record.get("isMeta") is not True
        and record.get("isSidechain") is not True
        and not _is_foreign(record, state)
    ):
        text = _text_of(record.get("message"))
        if text and text.strip() and not _SKIP_PROMPT_RE.match(text):
            state.first_prompt = text.strip()[:500]


def _absorb_marker(record: dict, state: _State) -> None:
    kind = record.get("type")
    if kind not in ("custom-title", "ai-title", "last-prompt", "continued-in"):
        return
    if _is_foreign(record, state):
        return
    if kind == "custom-title" and isinstance(record.get("customTitle"), str):
        state.custom_title = record["customTitle"][:500]
    elif kind == "ai-title" and isinstance(record.get("aiTitle"), str):
        state.ai_title = record["aiTitle"][:500]
    elif kind == "last-prompt" and isinstance(record.get("lastPrompt"), str):
        state.last_prompt = record["lastPrompt"][:500]
    elif kind == "continued-in":
        target = record.get("continuedInSessionId")
        if is_session_id(target) and target != state.session_id and target not in state.continued_in:
            state.continued_in.append(target)


def _stream(handle, state: _State, chunk_size: int) -> None:
    head_lines = 0
    pending = b""
    skipping = False

    def handle_line(line: bytes) -> None:
        nonlocal head_lines
        if not line.strip():
            return
        if len(line) > MAX_LINE:
            state.oversized += 1
            return
        in_head = head_lines < HEAD_RECORDS
        has_marker = len(line) <= MAX_METADATA_LINE and any(m in line for m in _MARKERS)
        if not in_head and not has_marker:
            return
        record = _decode(line, state)
        if record is None:
            return
        if in_head:
            head_lines += 1
            _absorb_head(record, state)
        if has_marker:
            _absorb_marker(record, state)

    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            break
        if skipping:
            newline = chunk.find(b"\n")
            if newline == -1:
                continue
            chunk = chunk[newline + 1:]
            skipping = False
        data = pending + chunk
        lines = data.split(b"\n")
        pending = lines.pop()
        for line in lines:
            handle_line(line)
        if len(pending) > MAX_LINE:
            state.oversized += 1
            pending = b""
            skipping = True
    if pending and not skipping:
        if _probe_complete(pending):
            handle_line(pending)
        else:
            state.incomplete_last_line = True


def _probe_complete(line: bytes) -> bool:
    try:
        json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return True


def _tail(handle, size: int, state: _State) -> None:
    offset = max(0, size - TAIL_BYTES)
    handle.seek(offset)
    data = handle.read(TAIL_BYTES)
    lines = data.split(b"\n")
    if offset > 0 and lines:
        lines = lines[1:]  # the first line is a fragment
    if lines and not data.endswith(b"\n"):
        last = lines.pop()
        if last.strip() and _probe_complete(last):
            lines.append(last)
    tail_state = _State(state.session_id)
    for line in lines:
        if not line.strip() or len(line) > MAX_LINE:
            continue
        record = _decode(line, tail_state)
        if record is not None:
            _absorb_common(record, state, from_tail=True)


def summarize_transcript(
    path: str, session_id: str, *, chunk_size: int = CHUNK_SIZE
) -> TranscriptSummary | None:
    """Summarize a transcript; None if it cannot be opened as a regular file."""
    # O_NONBLOCK keeps a FIFO swapped in after the lstat() check from blocking.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    state = _State(session_id)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            _stream(handle, state, chunk_size)
            _tail(handle, st.st_size, state)
    except OSError:
        return None
    finally:
        os.close(fd)
    return TranscriptSummary(
        first_cwd=state.first_cwd,
        last_cwd=state.last_cwd,
        created_at=state.created_at,
        content_last_at=state.content_last_at,
        custom_title=state.custom_title,
        ai_title=state.ai_title,
        last_prompt=state.last_prompt,
        first_prompt=state.first_prompt,
        entrypoints=tuple(sorted(state.entrypoints)),
        versions=tuple(sorted(state.versions)),
        continued_in=tuple(sorted(state.continued_in)),
        sidechain_first=bool(state.sidechain_first),
        parsed_records=state.parsed,
        parse_errors=state.parse_errors,
        oversized_lines=state.oversized,
        foreign_records=state.foreign,
        incomplete_last_line=state.incomplete_last_line,
    )
