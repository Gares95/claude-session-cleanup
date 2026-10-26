#!/usr/bin/env python3
"""Skill entry point: run claude-session-cleanup from an installed skill directory."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_session_cleanup.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
