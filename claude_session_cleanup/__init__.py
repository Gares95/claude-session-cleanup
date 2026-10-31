"""Retention previews and reviewed permanent deletion for local Claude Code sessions."""

__version__ = "1.0.0"

# Claude Code versions whose on-disk layout this release was checked against.
# Previews still run on other versions, with a warning.
TESTED_CLAUDE_CODE_VERSIONS = ("2.1.310", "2.1.314", "2.1.316")

SCHEMA_NAME = "session-cleanup/preview"
SCHEMA_VERSION = 1
