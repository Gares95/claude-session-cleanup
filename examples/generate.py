"""Regenerate the example previews from synthetic data.

Usage (from the repository root):
    PYTHONPATH=.:tests python3 examples/generate.py
"""

import datetime as dt
import io
import os
import tempfile

from support import ConfigFactory, NOW_ARG, session_uuid
from claude_session_cleanup import cli
_temp = tempfile.TemporaryDirectory(prefix="csc-example-")
root = os.path.realpath(_temp.name)
out_dir = os.path.dirname(os.path.abspath(__file__))
f = ConfigFactory(os.path.join(root, ".claude"), os.path.join(root, "src"))
DAY = dt.timedelta(days=1)
f.user_settings({"cleanupPeriodDays": 30})
f.session(session_id=session_uuid(0xaaaa0001000040008000000000000001), age=45*DAY, title="Refactor upload pipeline", subagents=(("a1", 45*DAY),), file_history=45*DAY)
f.session(session_id=session_uuid(0xaaaa0002000040008000000000000002), age=34*DAY, ai_title="Add Stripe webhooks", tool_results=(("r1.txt", 34*DAY),))
f.session(session_id=session_uuid(0xaaaa0003000040008000000000000003), age=40*DAY, ai_title="Spike: caching layer", subagents=(("b2", dt.timedelta(hours=5)),))
f.session(session_id=session_uuid(0xaaaa0004000040008000000000000004), age=3*DAY, ai_title="Fix login redirect loop")
f.session(session_id=session_uuid(0xaaaa0005000040008000000000000005), age=26*DAY, ai_title="Upgrade build tooling")
f.session(session_id=session_uuid(0xaaaa0006000040008000000000000006), prompt=None, age=50*DAY)
os.makedirs(os.path.join(f.default_project, ".claude"), exist_ok=True)
for name, args in (("preview.txt", ["--older-than", "30d", "--keep-latest", "2"]), ("preview-verbose.txt", ["--older-than", "30d", "--keep-latest", "2", "--verbose"]), ("preview.json", ["--older-than", "30d", "--keep-latest", "2", "--json", "--include-titles"])):
    out = io.StringIO()
    cli.run(["--now", NOW_ARG, *args], environment={"CLAUDE_CONFIG_DIR": f.config, "HOME": root}, cwd=f.default_project, stdout=out, tz=dt.timezone.utc, managed_dirs=(), liveness_options={"platform": "linux", "proc_root": os.path.join(root, "noproc")})
    text = out.getvalue().replace(root, "/home/user").replace("-" + root.replace("/", "-").lstrip("-"), "-home-user")
    text = text.replace("(--config-dir)", "(CLAUDE_CONFIG_DIR)")
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as handle:
        handle.write(text)
_temp.cleanup()
