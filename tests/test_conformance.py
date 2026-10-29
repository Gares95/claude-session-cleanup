"""Run the shared contract fixtures (contract/fixtures) through this backend.

The adapter builds real SessionUnit objects, groups them with the real
discovery grouping code (descendants become continuation links), and runs
the real policy engine.
"""

import datetime as dt
import glob
import json
import os

from claude_session_cleanup.config import SANITIZE_RE
from claude_session_cleanup.discovery import SessionUnit, build_groups
from claude_session_cleanup.fsutil import Issue
from claude_session_cleanup.policy import Policy, build_plan
from support import IsolatedTestCase

CONTRACT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contract")
CASES = sorted(glob.glob(os.path.join(CONTRACT, "fixtures", "*.json")))
BACKEND_CAPABILITIES = {"pinning": False}


def _unit(spec: dict, now: dt.datetime, children: list[str]) -> SessionUnit:
    malformed = spec["metadata"] == "malformed"
    activity = now - dt.timedelta(hours=spec["hours_ago"])
    return SessionUnit(
        session_id=spec["id"],
        project_dir_name=SANITIZE_RE.sub("-", spec["project"]),
        identity="missing" if malformed else "verified",
        project_path=None if malformed else spec["project"],
        recorded_cwds=(), title=None, title_source=None, created_at=None,
        transcript_mtime=activity, transcript_bytes=0, content_last_at=None,
        last_activity=activity, last_activity_source="transcript-mtime",
        artifacts=(), unit_bytes=0, subagent_count=0, entrypoints=(), claude_versions=(),
        continued_in=tuple(sorted(children)), shared_data_links=(),
        issues=(Issue("unparseable-transcript", spec["id"]),) if malformed else (),
        warnings=(), unknown_artifacts=(),
    )


def evaluate(case: dict) -> dict:
    now = dt.datetime.fromisoformat(case["evaluated_at"].replace("Z", "+00:00"))
    children: dict[str, list[str]] = {}
    for spec in case["sessions"]:
        if spec["parent"] is not None:
            children.setdefault(spec["parent"], []).append(spec["id"])
    units = tuple(_unit(spec, now, children.get(spec["id"], [])) for spec in case["sessions"])
    groups, _ = build_groups(units)
    hours = case["policy"]["older_than_hours"]
    policy = Policy(
        older_than=dt.timedelta(hours=hours) if hours is not None else None,
        keep_latest=case["policy"]["keep_latest"],
        project_mode=case["scope"]["mode"],
        project_path=case["scope"]["path"] or "/",
        min_idle=dt.timedelta(seconds=1),  # Claude-only guard, not modelled by the fixtures
    )
    protections = {spec["id"]: "running" for spec in case["sessions"] if spec["state"] == "running"}
    plan = build_plan(groups, policy, now=now, session_protections=protections)
    return {d.group.group_id: (d.status, d.rank, d.reasons) for d in plan.decisions}


class ConformanceTests(IsolatedTestCase):
    def test_fixture_set_is_present(self) -> None:
        self.assertGreaterEqual(len(CASES), 15)
        for name in ("SPEC.md", "preview-v1.schema.json", "compatibility-matrix.md", "codex-handoff.md"):
            self.assertTrue(os.path.isfile(os.path.join(CONTRACT, name)), name)

    def test_contract_fixtures(self) -> None:
        for path in CASES:
            with open(path, encoding="utf-8") as handle:
                case = json.load(handle)
            with self.subTest(case=case["id"]):
                self.assertEqual(("session-cleanup/conformance-case", 1), (case["format"], case["version"]))
                missing = [cap for cap in case["requires"] if not BACKEND_CAPABILITIES.get(cap, False)]
                if missing:
                    self.skipTest(f"backend lacks {missing}")
                actual = evaluate(case)
                expected = {item["group"]: item for item in case["expected"]}
                self.assertEqual(set(expected), set(actual))
                for group_id, item in expected.items():
                    status, rank, reasons = actual[group_id]
                    self.assertEqual(item["status"], status, group_id)
                    self.assertEqual(item["rank"], rank, group_id)
                    self.assertTrue(set(item["reasons_include"]) <= set(reasons), (group_id, reasons))


class ContractConsistencyTests(IsolatedTestCase):
    def test_reason_registry_matches_spec_table(self) -> None:
        from claude_session_cleanup.policy import REASON_ORDER

        with open(os.path.join(CONTRACT, "SPEC.md"), encoding="utf-8") as handle:
            spec = handle.read()
        section = spec.split("### 3.3", 1)[1].split("### 3.4", 1)[0]
        codes = tuple(line.split("`")[1] for line in section.splitlines() if line.startswith("| `"))
        self.assertEqual(REASON_ORDER, codes)

    def test_fixtures_are_well_formed(self) -> None:
        from claude_session_cleanup.policy import REASON_ORDER

        statuses = {"selected", "retained", "protected", "excluded"}
        for path in CASES:
            with open(path, encoding="utf-8") as handle:
                case = json.load(handle)
            with self.subTest(case=os.path.basename(path)):
                ids = [s["id"] for s in case["sessions"]]
                self.assertEqual(len(ids), len(set(ids)))
                roots = {s["id"] for s in case["sessions"] if s["parent"] is None}
                for spec in case["sessions"]:
                    self.assertIn(spec["state"], {"idle", "running", "pinned"})
                    self.assertIn(spec["metadata"], {"valid", "malformed"})
                    if spec["parent"] is not None:
                        self.assertIn(spec["parent"], ids)
                for item in case["expected"]:
                    self.assertIn(item["group"], roots)
                    self.assertIn(item["status"], statuses)
                    self.assertEqual(item["status"] == "excluded", item["rank"] is None)
                    self.assertTrue(set(item["reasons_include"]) <= set(REASON_ORDER))

    def test_every_emitted_reason_is_registered(self) -> None:
        from claude_session_cleanup.policy import REASON_ORDER

        day = dt.timedelta(days=1)
        self.factory.session(age=40 * day)
        self.factory.session(age=1 * day)
        self.factory.session(prompt=None, age=50 * day)
        self.factory.session(raw=b"not json\n", age=50 * day)
        self.factory.session(cwd=self.factory.project("elsewhere"), age=50 * day)
        self.factory.job("abcdef12", self.factory.session(age=40 * day))
        report = self.run_json("--older-than", "7d", "--keep-latest", "1", "--project", "all")
        emitted = {reason for group in report["groups"] for reason in group["reasons"]}
        self.assertTrue(emitted)
        self.assertTrue(emitted <= set(REASON_ORDER), emitted - set(REASON_ORDER))
        self.assertEqual({"selected", "retained", "protected", "excluded"},
                         {group["status"] for group in report["groups"]})


class ContractIntegrityTests(IsolatedTestCase):
    def test_contract_files_match_checksums(self) -> None:
        """The contract is vendored byte-identically; edits must update SHA256SUMS deliberately."""
        import hashlib

        with open(os.path.join(CONTRACT, "SHA256SUMS"), encoding="utf-8") as handle:
            recorded = dict(reversed(line.split("  ", 1)) for line in handle.read().splitlines() if line)
        actual = {}
        for directory, _, files in os.walk(CONTRACT):
            for name in files:
                path = os.path.join(directory, name)
                rel = os.path.relpath(path, CONTRACT).replace(os.sep, "/")
                if rel != "SHA256SUMS":
                    with open(path, "rb") as handle:
                        actual[rel] = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(recorded, actual)
