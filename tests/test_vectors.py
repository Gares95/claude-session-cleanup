"""Agent-neutral retention vectors (tests/vectors/retention/*.json).

The vectors describe groups only by member activity and protection/exclusion
flags, so another backend (such as codex-session-cleanup) can run the same
cases against its own engine.
"""

import datetime as dt
import glob
import json
import os

from claude_session_cleanup.discovery import SessionGroup, SessionUnit
from claude_session_cleanup.policy import Policy, build_plan
from support import IsolatedTestCase

VECTORS = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "vectors", "retention", "*.json")))


def unit(session_id: str, last_activity: dt.datetime, excluded: bool) -> SessionUnit:
    return SessionUnit(
        session_id=session_id, project_dir_name="-p", identity="missing" if excluded else "verified",
        project_path=None if excluded else "/p", recorded_cwds=(), title=None, title_source=None,
        created_at=None, transcript_mtime=last_activity, transcript_bytes=0, content_last_at=None,
        last_activity=last_activity, last_activity_source="transcript-mtime", artifacts=(), unit_bytes=0,
        subagent_count=0, entrypoints=(), claude_versions=(), continued_in=(), shared_data_links=(), issues=(), warnings=(),
        unknown_artifacts=(),
    )


class VectorTests(IsolatedTestCase):
    def test_vectors_exist(self) -> None:
        self.assertGreaterEqual(len(VECTORS), 8)

    def test_all_vectors(self) -> None:
        for path in VECTORS:
            with open(path, encoding="utf-8") as handle:
                vector = json.load(handle)
            with self.subTest(vector=os.path.basename(path)):
                self.assertEqual(("session-cleanup/retention-vector", 1), (vector["format"], vector["version"]))
                now = dt.datetime.fromisoformat(vector["now"].replace("Z", "+00:00"))
                groups, protections = [], {}
                for spec in vector["groups"]:
                    members = tuple(
                        unit(spec["id"] if index == 0 else f"{spec['id']}-{index}",
                             now - dt.timedelta(hours=hours), spec.get("excluded", False))
                        for index, hours in enumerate(spec["members_hours_ago"])
                    )
                    newest = max(members, key=lambda m: m.last_activity)
                    groups.append(SessionGroup(spec["id"], members, newest.last_activity, newest.session_id, ()))
                    if spec.get("protected"):
                        protections[spec["id"]] = "registered-running"
                hours = vector["policy"]["older_than_hours"]
                policy = Policy(
                    older_than=dt.timedelta(hours=hours) if hours is not None else None,
                    keep_latest=vector["policy"]["keep_latest"],
                    project_mode="all", project_path="/p", min_idle=dt.timedelta(minutes=1),
                )
                plan = build_plan(groups, policy, now=now, session_protections=protections)
                actual = {status: sorted(d.group.group_id for d in plan.by_status(status))
                          for status in ("selected", "protected", "excluded")}
                expected = {status: sorted(ids) for status, ids in vector["expected"].items()}
                self.assertEqual(expected, actual)
