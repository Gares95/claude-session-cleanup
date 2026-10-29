import datetime as dt
import os

from claude_session_cleanup.errors import CleanupError
from claude_session_cleanup.policy import Policy, build_plan
from support import NOW, IsolatedTestCase, requires_symlinks, session_uuid, set_age

DAY = dt.timedelta(days=1)


def ids(report, status):
    return [g["group_id"] for g in report["groups"] if g["status"] == status]


def reasons(report, group_id):
    return next(g["reasons"] for g in report["groups"] if g["group_id"] == group_id)


class RetentionTests(IsolatedTestCase):
    def make(self, *ages_days, project=None):
        return [self.factory.session(project, age=age * DAY) for age in ages_days]

    def test_age_only(self) -> None:
        old, new = self.make(10, 2)
        report = self.run_json("--older-than", "7d")
        self.assertEqual([old], ids(report, "selected"))
        self.assertEqual(["newer-than-cutoff"], reasons(report, new))
        self.assertEqual(["older-than"], reasons(report, old))

    def test_keep_latest_only(self) -> None:
        a, b, c = self.make(1, 2, 3)
        report = self.run_json("--keep-latest", "2")
        self.assertEqual([c], ids(report, "selected"))
        self.assertEqual(["outside-latest"], reasons(report, c))
        self.assertEqual({a, b}, set(ids(report, "retained")))

    def test_combined_policies_use_intersection(self) -> None:
        recent_old_enough, latest_but_old, old = self.make(20, 40, 50)
        newest = self.factory.session(age=DAY)
        report = self.run_json("--older-than", "30d", "--keep-latest", "2")
        # Latest two: newest and recent_old_enough. Older than 30d: latest_but_old, old.
        self.assertEqual([latest_but_old, old], ids(report, "selected"))
        self.assertEqual(["within-latest", "newer-than-cutoff"], reasons(report, recent_old_enough))
        self.assertIn(newest, ids(report, "retained"))

    def test_cutoff_boundary_is_strict(self) -> None:
        exact = self.factory.session(age=7 * DAY)
        report = self.run_json("--older-than", "7d")
        self.assertEqual([exact], ids(report, "retained"))

    def test_ties_are_ranked_by_session_id(self) -> None:
        later_id = self.factory.session(session_id=session_uuid(9), age=5 * DAY)
        earlier_id = self.factory.session(session_id=session_uuid(3), age=5 * DAY)
        report = self.run_json("--keep-latest", "1")
        self.assertEqual([earlier_id], ids(report, "retained"))
        self.assertEqual([later_id], ids(report, "selected"))
        ranks = {g["group_id"]: g["rank"] for g in report["groups"]}
        self.assertEqual({earlier_id: 1, later_id: 2}, ranks)

    def test_zero_matches(self) -> None:
        self.make(1)
        report = self.run_json("--older-than", "30d")
        self.assertEqual([], ids(report, "selected"))
        self.assertEqual(0, report["totals"]["groups"]["selected"])


class ProjectScopeTests(IsolatedTestCase):
    def test_current_scope_is_exact_and_never_widens(self) -> None:
        here = self.factory.default_project
        parent = os.path.dirname(here)
        child = self.factory.project("repo", "packages", "app")
        sibling_worktree = self.factory.project("repo-feature")
        mine = self.factory.session(here, age=40 * DAY)
        for path in (parent, child, sibling_worktree):
            self.factory.session(path, age=40 * DAY)
        report = self.run_json("--older-than", "7d")
        self.assertEqual([mine], [g["group_id"] for g in report["groups"]])

    def test_all_scope_includes_every_verified_project(self) -> None:
        first = self.factory.session(age=40 * DAY)
        second = self.factory.session(self.factory.project("other"), age=40 * DAY)
        report = self.run_json("--older-than", "7d", "--project", "all")
        self.assertEqual({first, second}, set(ids(report, "selected")))

    @requires_symlinks
    def test_invoking_through_a_symlink_matches_the_real_directory(self) -> None:
        mine = self.factory.session(age=40 * DAY)
        link = os.path.join(self.tmp, "link-to-repo")
        os.symlink(self.factory.default_project, link)
        report = self.run_json("--older-than", "7d", cwd=link)
        self.assertEqual([mine], ids(report, "selected"))

    def test_storage_name_collision_is_excluded_not_selected(self) -> None:
        dashed = self.factory.project("a-b")
        nested = self.factory.project("a", "b")
        self.assertEqual(self.factory.storage_name(dashed), self.factory.storage_name(nested))
        theirs = self.factory.session(nested, age=40 * DAY)
        mine = self.factory.session(dashed, age=40 * DAY)
        report = self.run_json("--older-than", "7d", cwd=dashed)
        self.assertEqual([mine], ids(report, "selected"))
        self.assertEqual([theirs], ids(report, "excluded"))
        self.assertEqual(["project-path-collision"], reasons(report, theirs))

    def test_unverified_sessions_in_this_storage_are_shown_as_excluded(self) -> None:
        unverified = self.factory.session(cwd=self.factory.project("elsewhere"), age=40 * DAY)
        missing = self.factory.session(prompt=None, age=40 * DAY)
        report = self.run_json("--older-than", "7d")
        self.assertEqual({unverified, missing}, set(ids(report, "excluded")))
        self.assertEqual(["project-identity-unverified"], reasons(report, unverified))
        self.assertEqual(["project-identity-unknown"], reasons(report, missing))

    def test_moved_repository_sessions_do_not_match_new_location(self) -> None:
        old_location = self.factory.project("old-location")
        self.factory.session(old_location, age=40 * DAY)
        new_location = self.factory.project("new-location")
        report = self.run_json("--older-than", "7d", cwd=new_location)
        self.assertEqual([], report["groups"])

    def test_project_dir_name_override_sessions_are_excluded_in_all_scope(self) -> None:
        custom = self.factory.session(storage_name="work", age=40 * DAY)
        report = self.run_json("--older-than", "7d", "--project", "all")
        self.assertEqual([custom], ids(report, "excluded"))


class GroupSemanticsTests(IsolatedTestCase):
    def test_recent_subagent_protects_old_parent(self) -> None:
        parent = self.factory.session(age=60 * DAY, subagents=(("child", dt.timedelta(hours=3)),))
        report = self.run_json("--older-than", "30d")
        self.assertEqual([parent], ids(report, "retained"))
        self.assertEqual(["newer-than-cutoff"], reasons(report, parent))
        group = report["groups"][0]
        self.assertEqual(parent, group["last_activity_session_id"])
        self.assertTrue(group["sessions"][0]["last_activity_source"].startswith("subagent"))

    def test_recent_tool_result_or_checkpoint_protects_old_parent(self) -> None:
        a = self.factory.session(age=60 * DAY, tool_results=(("r.txt", DAY),))
        b = self.factory.session(age=60 * DAY, file_history=DAY)
        report = self.run_json("--older-than", "30d")
        self.assertEqual({a, b}, set(ids(report, "retained")))

    def test_recent_continuation_protects_the_whole_group(self) -> None:
        continuation = self.factory.session(session_id=session_uuid(2), age=DAY)
        root = self.factory.session(session_id=session_uuid(1), age=90 * DAY, continued_in=(continuation,))
        report = self.run_json("--older-than", "30d")
        self.assertEqual([root], ids(report, "retained"))
        group = report["groups"][0]
        self.assertEqual(2, group["session_count"])
        self.assertEqual(continuation, group["last_activity_session_id"])
        self.assertEqual([("root", None), ("continuation", root)],
                         [(s["relation"], s["parent_session_id"]) for s in group["sessions"]])

    def test_keep_latest_counts_groups_not_sessions(self) -> None:
        continuation = self.factory.session(session_id=session_uuid(2), age=DAY)
        self.factory.session(session_id=session_uuid(1), age=2 * DAY, continued_in=(continuation,))
        lone = self.factory.session(session_id=session_uuid(3), age=3 * DAY)
        report = self.run_json("--keep-latest", "1")
        self.assertEqual([lone], ids(report, "selected"))
        self.assertEqual(3, report["totals"]["sessions"]["examined"])

    def test_linked_session_in_another_project_excludes_group(self) -> None:
        other = self.factory.project("other")
        continuation = self.factory.session(other, session_id=session_uuid(2), age=40 * DAY)
        root = self.factory.session(session_id=session_uuid(1), age=40 * DAY, continued_in=(continuation,))
        report = self.run_json("--older-than", "7d")
        self.assertEqual([root], ids(report, "excluded"))
        self.assertEqual(["related-outside-scope"], reasons(report, root))

    def test_excluded_groups_do_not_consume_latest_slots(self) -> None:
        # The excluded session is the newest; it must not push an older one out of the latest N.
        self.factory.session(prompt=None, age=DAY)
        kept = self.factory.session(age=2 * DAY)
        next_oldest = self.factory.session(age=3 * DAY)
        report = self.run_json("--keep-latest", "1")
        self.assertEqual([kept], ids(report, "retained"))
        self.assertEqual([next_oldest], ids(report, "selected"))

    def test_protected_groups_occupy_latest_slots(self) -> None:
        running = self.factory.session(age=DAY)
        second = self.factory.session(age=2 * DAY)
        report = self.run_json("--keep-latest", "1", env={"CLAUDE_CODE_SESSION_ID": running})
        self.assertEqual(["invoking-session", "within-latest"], reasons(report, running))
        self.assertEqual([second], ids(report, "selected"))

    @requires_symlinks
    def test_unsafe_metadata_in_any_member_excludes_group(self) -> None:
        continuation = self.factory.session(session_id=session_uuid(2), age=40 * DAY,
                                            subagents=(("a", 40 * DAY),))
        os.symlink("/etc", os.path.join(self.factory.storage_dir(), continuation, "subagents", "link"))
        root = self.factory.session(session_id=session_uuid(1), age=40 * DAY, continued_in=(continuation,))
        report = self.run_json("--older-than", "7d")
        self.assertEqual([root], ids(report, "excluded"))
        self.assertEqual(["unsafe-storage"], reasons(report, root))


class ProtectionTests(IsolatedTestCase):
    def test_invoking_session_is_protected(self) -> None:
        sid = self.factory.session(age=40 * DAY)
        report = self.run_json("--older-than", "7d", env={"CLAUDE_CODE_SESSION_ID": sid})
        self.assertEqual(["invoking-session"], reasons(report, sid))

    def test_recently_active_guard(self) -> None:
        sid = self.factory.session(age=dt.timedelta(minutes=20))
        report = self.run_json("--keep-latest", "1", "--min-idle", "1h")
        self.assertIn("recently-active", reasons(report, sid))
        other = self.factory.session(age=dt.timedelta(minutes=30))
        report = self.run_json("--keep-latest", "1")
        self.assertEqual(["recently-active"], reasons(report, other))

    def test_background_job_record_protects(self) -> None:
        sid = self.factory.session(age=40 * DAY)
        resumed = self.factory.session(age=40 * DAY)
        self.factory.job("abcdef01", sid, resume=resumed)
        report = self.run_json("--older-than", "7d")
        self.assertEqual(["background-session"], reasons(report, sid))
        self.assertEqual(["background-session"], reasons(report, resumed))


class EngineValidationTests(IsolatedTestCase):
    def policy(self, **overrides):
        values = dict(older_than=None, keep_latest=None, project_mode="current", project_path="/p",
                      min_idle=dt.timedelta(hours=1))
        values.update(overrides)
        return Policy(**values)

    def test_unknown_protection_reason_is_refused(self) -> None:
        with self.assertRaises(CleanupError):
            build_plan([], self.policy(keep_latest=1), now=NOW, session_protections={"x": "made-up"})

    def test_requires_a_selector_and_valid_inputs(self) -> None:
        with self.assertRaises(CleanupError):
            build_plan([], self.policy(), now=NOW)
        with self.assertRaises(CleanupError):
            build_plan([], self.policy(keep_latest=0), now=NOW)
        with self.assertRaises(CleanupError):
            build_plan([], self.policy(keep_latest=1, project_mode="parent"), now=NOW)
        with self.assertRaises(CleanupError):
            build_plan([], self.policy(keep_latest=1), now=NOW.replace(tzinfo=None))


class SharedDataPolicyTests(IsolatedTestCase):
    @requires_symlinks
    def test_recent_continuation_sharing_subagent_data_protects_origin(self) -> None:
        origin = self.factory.session(session_id=session_uuid(1), age=60 * DAY, subagents=(("a1", 60 * DAY),))
        continuation = self.factory.session(session_id=session_uuid(2), age=DAY)
        directory = os.path.join(self.factory.storage_dir(), continuation, "subagents")
        os.makedirs(directory)
        link = os.path.join(directory, "agent-a1.jsonl")
        os.symlink(os.path.join(self.factory.storage_dir(), origin, "subagents", "agent-a1.jsonl"), link)
        for path in (link, directory, os.path.dirname(directory)):
            set_age(path, DAY)
        report = self.run_json("--older-than", "30d")
        self.assertEqual([origin], ids(report, "retained"))
        group = report["groups"][0]
        self.assertIn("cross-session-symlink", group["mutation_blockers"])
        self.assertIn({"kind": "shared-session-data", "session_id": origin},
                      group["sessions"][1]["extensions"]["claude_code"]["links"])


class AncestryTests(IsolatedTestCase):
    def test_two_origins_continuing_into_one_session_are_ambiguous(self) -> None:
        shared = self.factory.session(session_id=session_uuid(3), age=40 * DAY)
        first = self.factory.session(session_id=session_uuid(1), age=40 * DAY, continued_in=(shared,))
        self.factory.session(session_id=session_uuid(2), age=40 * DAY, continued_in=(shared,))
        report = self.run_json("--older-than", "7d")
        self.assertEqual([first], ids(report, "excluded"))
        self.assertEqual(["ancestry-ambiguous"], reasons(report, first))
        self.assertEqual(3, report["groups"][0]["session_count"])
