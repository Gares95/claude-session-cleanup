import datetime as dt
import json
import os
from unittest import mock

from claude_session_cleanup import discovery
from claude_session_cleanup.config import ConfigDir
from claude_session_cleanup.errors import CleanupError
from support import NOW, IsolatedTestCase, requires_symlinks, session_uuid, set_age, snapshot_tree

DAY = dt.timedelta(days=1)


class DiscoveryTests(IsolatedTestCase):
    def discover(self) -> discovery.Discovery:
        return discovery.discover(ConfigDir(self.factory.config, "CLAUDE_CONFIG_DIR"))

    def unit(self, result, session_id):
        return next(u for u in result.units if u.session_id == session_id)

    def test_unit_collects_every_session_keyed_artifact(self) -> None:
        sid = self.factory.session(
            age=40 * DAY,
            subagents=(("a1", 40 * DAY),),
            tool_results=(("out.txt", 40 * DAY),),
            file_history=40 * DAY,
            session_env=40 * DAY,
        )
        directory = self.factory.storage_dir()
        with open(os.path.join(directory, f"{sid}.jsonl.superseded-1"), "w") as handle:
            handle.write("{}\n")
        set_age(os.path.join(directory, f"{sid}.jsonl.superseded-1"), 40 * DAY)
        debug = os.path.join(self.factory.config, "debug", f"{sid}.txt")
        os.makedirs(os.path.dirname(debug))
        with open(debug, "w") as handle:
            handle.write("log")
        set_age(debug, 40 * DAY)
        unit = self.unit(self.discover(), sid)
        kinds = unit.bytes_by_kind()
        for kind in ("transcript", "subagent", "tool_result", "file_history", "session_env",
                     "transcript_variant", "debug_log"):
            self.assertIn(kind, kinds)
        self.assertEqual(1, unit.subagent_count)
        self.assertEqual(sum(kinds.values()), unit.unit_bytes)
        self.assertEqual((), unit.issues)
        self.assertEqual((), unit.unknown_artifacts)

    def test_memory_and_shared_state_are_never_part_of_a_unit(self) -> None:
        sid = self.factory.session(age=40 * DAY)
        memory = os.path.join(self.factory.storage_dir(), "memory")
        os.makedirs(memory)
        with open(os.path.join(memory, "MEMORY.md"), "w") as handle:
            handle.write("- note")
        unit = self.unit(self.discover(), sid)
        self.assertFalse(any("memory" in item.rel_path for item in unit.artifacts))

    def test_recent_subagent_sets_last_activity_of_old_parent(self) -> None:
        sid = self.factory.session(age=60 * DAY, subagents=(("recent", dt.timedelta(minutes=5)),))
        unit = self.unit(self.discover(), sid)
        self.assertEqual(NOW - dt.timedelta(minutes=5), unit.last_activity)
        self.assertTrue(unit.last_activity_source.startswith("subagent"))
        self.assertEqual(NOW - 60 * DAY, unit.transcript_mtime)

    def test_content_timestamp_newer_than_mtime_is_evidence(self) -> None:
        sid = self.factory.session(age=60 * DAY, content_age=2 * DAY)
        unit = self.unit(self.discover(), sid)
        self.assertEqual("content-timestamp", unit.last_activity_source)
        self.assertEqual(NOW - 2 * DAY, unit.last_activity)

    def test_identity_verified_from_recorded_cwd(self) -> None:
        sid = self.factory.session()
        unit = self.unit(self.discover(), sid)
        self.assertEqual(("verified", self.factory.default_project), (unit.identity, unit.project_path))

    def test_identity_after_cd_uses_last_recorded_cwd(self) -> None:
        old = self.factory.project("old")
        new = self.factory.project("new")
        sid = self.factory.session(new, cwd=old, last_cwd=new)
        unit = self.unit(self.discover(), sid)
        self.assertEqual(("verified", new), (unit.identity, unit.project_path))

    def test_identity_unverified_when_no_recorded_cwd_matches_storage(self) -> None:
        other = self.factory.project("other")
        sid = self.factory.session(self.factory.default_project, cwd=other)
        unit = self.unit(self.discover(), sid)
        self.assertEqual(("unverified", None), (unit.identity, unit.project_path))

    def test_identity_missing_without_cwd_evidence(self) -> None:
        sid = self.factory.session(prompt=None)
        self.assertEqual("missing", self.unit(self.discover(), sid).identity)

    def test_long_project_path_uses_prefix_and_hash_suffix(self) -> None:
        deep = self.factory.project(*(["d" * 60] * 4))
        name = self.factory.storage_name(deep)[:200] + "-1x9z"
        sid = self.factory.session(deep, storage_name=name)
        unit = self.unit(self.discover(), sid)
        self.assertEqual(("verified", deep), (unit.identity, unit.project_path))

    @requires_symlinks
    def test_symlinks_inside_a_unit_are_reported_not_followed(self) -> None:
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        sid = self.factory.session(age=40 * DAY, subagents=(("a", 40 * DAY),))
        os.symlink(outside, os.path.join(self.factory.storage_dir(), sid, "escape"))
        unit = self.unit(self.discover(), sid)
        self.assertIn("symlink-in-session", unit.excluded_issue_codes)
        self.assertFalse(any("outside" in item.rel_path for item in unit.artifacts))

    @requires_symlinks
    def test_symlinked_transcript_is_not_read(self) -> None:
        real = os.path.join(self.tmp, "real.jsonl")
        with open(real, "w") as handle:
            handle.write(json.dumps({"type": "user", "cwd": self.factory.default_project}) + "\n")
        sid = session_uuid(77)
        os.symlink(real, os.path.join(self.factory.storage_dir(), f"{sid}.jsonl"))
        unit = self.unit(self.discover(), sid)
        self.assertIn("symlink-in-session", unit.excluded_issue_codes)
        self.assertEqual("missing", unit.identity)

    @requires_symlinks
    def test_symlinked_project_directory_is_skipped_with_warning(self) -> None:
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(target)
        os.symlink(target, os.path.join(self.factory.config, "projects", "-linked"))
        result = self.discover()
        self.assertIn(("symlinked-project-directory", "projects/-linked"),
                      [(w.code, w.path) for w in result.warnings])

    @requires_symlinks
    def test_symlinked_projects_root_is_a_safety_stop(self) -> None:
        config = os.path.join(self.tmp, "cfg2")
        os.makedirs(config)
        os.symlink(self.factory.config + "/projects", os.path.join(config, "projects"))
        with self.assertRaises(CleanupError):
            discovery.discover(ConfigDir(config, "--config-dir"))

    def test_empty_and_unparseable_transcripts_are_flagged(self) -> None:
        empty = self.factory.session(raw=b"")
        garbage = self.factory.session(raw=b"not json\n")
        result = self.discover()
        self.assertIn("empty-transcript", self.unit(result, empty).excluded_issue_codes)
        self.assertIn("unparseable-transcript", self.unit(result, garbage).excluded_issue_codes)

    def test_foreign_owner_is_flagged(self) -> None:
        sid = self.factory.session()
        with mock.patch.object(discovery, "owned_by_current_user", return_value=False), \
                mock.patch("claude_session_cleanup.fsutil.owned_by_current_user", return_value=False):
            unit = self.unit(self.discover(), sid)
        self.assertIn("foreign-owner", unit.excluded_issue_codes)

    def test_non_session_entries_and_orphans_are_reported(self) -> None:
        directory = self.factory.storage_dir()
        os.makedirs(os.path.join(directory, session_uuid(50)))
        with open(os.path.join(directory, "notes.txt"), "w") as handle:
            handle.write("x")
        with open(os.path.join(directory, session_uuid(0xABCDEF).upper() + ".jsonl"), "w") as handle:
            handle.write("{}\n")
        result = self.discover()
        codes = {(w.code, w.path.rsplit("/", 1)[-1]) for w in result.warnings}
        self.assertIn(("session-data-without-transcript", session_uuid(50)), codes)
        self.assertIn(("unrecognized-session-entry", session_uuid(0xABCDEF).upper() + ".jsonl"), codes)
        self.assertEqual((), result.units)

    def test_unknown_session_named_artifacts_are_reported(self) -> None:
        sid = self.factory.session()
        os.makedirs(os.path.join(self.factory.config, "new-feature", sid))
        unit = self.unit(self.discover(), sid)
        self.assertEqual((f"new-feature/{sid}",), unit.unknown_artifacts)

    def test_hard_links_are_counted_once(self) -> None:
        sid = self.factory.session(file_history=DAY)
        snapshot = os.path.join(self.factory.config, "file-history", sid, "snapshot@v1")
        os.link(snapshot, os.path.join(self.factory.config, "file-history", sid, "copy@v1"))
        unit = self.unit(self.discover(), sid)
        self.assertEqual(5, unit.bytes_by_kind()["file_history"])

    def test_duplicate_session_id_across_projects_is_excluded(self) -> None:
        other = self.factory.project("other")
        sid = self.factory.session()
        self.factory.session(other, session_id=sid)
        result = self.discover()
        duplicates = [u for u in result.units if u.session_id == sid]
        self.assertEqual(2, len(duplicates))
        for unit in duplicates:
            self.assertIn("duplicate-session-id", unit.excluded_issue_codes)
        self.assertEqual(2, len(result.groups))

    def test_continuation_links_form_one_group_rooted_at_the_origin(self) -> None:
        root = session_uuid(900)
        continuation = session_uuid(100)
        self.factory.session(session_id=continuation, age=DAY)
        self.factory.session(session_id=root, age=60 * DAY, continued_in=(continuation,))
        result = self.discover()
        self.assertEqual(1, len(result.groups))
        group = result.groups[0]
        self.assertEqual(root, group.group_id)
        self.assertEqual([root, continuation], [u.session_id for u in group.members])
        self.assertEqual(NOW - DAY, group.last_activity)

    def test_missing_link_target_is_reported(self) -> None:
        sid = self.factory.session(continued_in=(session_uuid(999),))
        result = self.discover()
        self.assertEqual((session_uuid(999),), result.groups[0].missing_links)
        self.assertEqual(sid, result.groups[0].group_id)

    def test_discovery_is_deterministic_and_read_only(self) -> None:
        for number in range(5):
            self.factory.session(age=number * DAY, subagents=(("s", number * DAY),), file_history=number * DAY)
        before = snapshot_tree(self.factory.config)
        first = self.discover()
        second = self.discover()
        self.assertEqual(before, snapshot_tree(self.factory.config))
        self.assertEqual(first, second)
        self.assertEqual(sorted(u.session_id for u in first.units), [u.session_id for u in first.units])

    def test_missing_projects_directory_is_a_warning(self) -> None:
        config = os.path.join(self.tmp, "fresh")
        os.makedirs(config)
        result = discovery.discover(ConfigDir(config, "--config-dir"))
        self.assertEqual("no-projects-directory", result.warnings[0].code)


class SharedSessionDataTests(IsolatedTestCase):
    """Claude Code symlinks a predecessor's subagent transcripts into a background continuation."""

    def discover(self):
        return discovery.discover(ConfigDir(self.factory.config, "CLAUDE_CONFIG_DIR"))

    def link_subagent(self, source: str, target: str, name: str = "agent-a1.jsonl", target_path: str | None = None):
        directory = os.path.join(self.factory.storage_dir(), source, "subagents")
        os.makedirs(directory, exist_ok=True)
        link = os.path.join(directory, name)
        os.symlink(target_path or os.path.join(self.factory.storage_dir(), target, "subagents", name), link)
        set_age(link, 40 * DAY)
        set_age(directory, 40 * DAY)
        set_age(os.path.dirname(directory), 40 * DAY)
        return link

    @requires_symlinks
    def test_in_tree_link_joins_groups_without_exclusion(self) -> None:
        origin = self.factory.session(session_id=session_uuid(1), age=40 * DAY, subagents=(("a1", 40 * DAY),))
        continuation = self.factory.session(session_id=session_uuid(2), age=40 * DAY)
        self.link_subagent(continuation, origin)
        result = self.discover()
        unit = next(u for u in result.units if u.session_id == continuation)
        self.assertEqual((origin,), unit.shared_data_links)
        self.assertEqual((), unit.issues)
        self.assertEqual(1, len(result.groups))
        self.assertEqual([origin, continuation], [u.session_id for u in result.groups[0].members])

    @requires_symlinks
    def test_relative_in_tree_link_is_accepted(self) -> None:
        origin = self.factory.session(session_id=session_uuid(1), age=40 * DAY, subagents=(("a1", 40 * DAY),))
        continuation = self.factory.session(session_id=session_uuid(2), age=40 * DAY)
        self.link_subagent(continuation, origin, target_path=f"../../{origin}/subagents/agent-a1.jsonl")
        unit = next(u for u in self.discover().units if u.session_id == continuation)
        self.assertEqual((origin,), unit.shared_data_links)

    @requires_symlinks
    def test_dangling_link_to_a_missing_session_is_a_missing_link(self) -> None:
        continuation = self.factory.session(session_id=session_uuid(2), age=40 * DAY)
        self.link_subagent(continuation, session_uuid(7))
        result = self.discover()
        self.assertEqual((session_uuid(7),), result.groups[0].missing_links)

    @requires_symlinks
    def test_links_outside_session_data_stay_unsafe(self) -> None:
        sid = self.factory.session(age=40 * DAY)
        other = self.factory.session(age=40 * DAY)
        outside = os.path.join(self.tmp, "outside.jsonl")
        with open(outside, "w") as handle:
            handle.write("{}\n")
        cases = {
            "escape.jsonl": outside,
            "transcript.jsonl": self.factory.transcript_path(other),  # a transcript, not session data
            "memory.jsonl": os.path.join(self.factory.storage_dir(), "memory", "x"),
            "config.jsonl": os.path.join(self.factory.config, "settings.json"),
        }
        for name, target in cases.items():
            self.link_subagent(sid, other, name=name, target_path=target)
        unit = next(u for u in self.discover().units if u.session_id == sid)
        self.assertEqual((), unit.shared_data_links)
        self.assertEqual(4, sum(1 for item in unit.issues if item.code == "symlink-in-session"))


class RaceAndAmbiguityTests(IsolatedTestCase):
    def discover(self):
        return discovery.discover(ConfigDir(self.factory.config, "CLAUDE_CONFIG_DIR"))

    def test_transcript_removed_during_scan_is_reported_not_fatal(self) -> None:
        gone = self.factory.session(age=40 * DAY)
        kept = self.factory.session(age=40 * DAY)
        real_build = discovery._build_unit

        def vanish(config, project_dir, session_id, variants):
            if session_id == gone:
                os.remove(os.path.join(project_dir, f"{session_id}.jsonl"))
            return real_build(config, project_dir, session_id, variants)

        with mock.patch.object(discovery, "_build_unit", vanish):
            result = self.discover()
        self.assertEqual([kept], [u.session_id for u in result.units])
        self.assertIn("session-vanished-during-scan", [w.code for w in result.warnings])

    def test_link_to_a_duplicated_session_id_excludes_the_linking_session(self) -> None:
        duplicated = session_uuid(5)
        self.factory.session(session_id=duplicated, age=DAY)
        self.factory.session(self.factory.project("other"), session_id=duplicated, age=DAY)
        origin = self.factory.session(age=60 * DAY, continued_in=(duplicated,))
        unit = next(u for u in self.discover().units if u.session_id == origin)
        self.assertIn("duplicate-session-id", unit.excluded_issue_codes)
