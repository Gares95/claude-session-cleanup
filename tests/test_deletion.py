"""Permanent deletion through reviewed saved plans, on synthetic storage only.

Every test builds its own temporary configuration directory, fake ``/proc``,
and Codex-like tree. The isolation guard in support.py fails any test that
touches the real ``~/.claude``; removals are guarded too.
"""

import datetime as dt
import hashlib
import json
import os
import stat
import unittest
from unittest import mock

from claude_session_cleanup import deletion, processes
from schema_check import validate
from support import NOW, ConfigFactory, IsolatedTestCase, fake_process, requires_symlinks, set_age, session_uuid
from test_cli import SCHEMA

DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)
DOMAIN = "linux:test-machine:pid:[1]"

# Deletion uses POSIX directory-descriptor operations and flock (Linux in
# production). Elsewhere only the "delete is unsupported" behavior is tested.
requires_posix_deletion = unittest.skipUnless(
    os.name == "posix" and {os.unlink, os.rmdir, os.stat, os.open} <= os.supports_dir_fd,
    "deletion needs POSIX directory-descriptor operations",
)


def tree(root: str) -> dict:
    """Type, size, and content digest of everything under root (never follows links)."""
    result = {}
    if not os.path.lexists(root):
        return result
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = os.path.join(directory, name)
            st = os.lstat(path)
            if stat.S_ISREG(st.st_mode):
                with open(path, "rb") as handle:
                    result[os.path.relpath(path, root)] = ("file", hashlib.sha256(handle.read()).hexdigest())
            elif stat.S_ISLNK(st.st_mode):
                result[os.path.relpath(path, root)] = ("link", os.readlink(path))
            else:
                result[os.path.relpath(path, root)] = ("dir", None)
    return result


class DeletionCase(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        self.plans = os.path.join(self.tmp, "plans")
        os.makedirs(self.plans)
        self.home = os.environ["HOME"]
        self.codex = os.path.join(self.home, ".codex")
        self.platform = "linux"
        self.domain = DOMAIN
        self._plan_counter = 0

    def opts(self) -> dict:
        return {"platform": self.platform, "proc_root": self.proc, "own_pid": 999,
                "uid": os.getuid() if hasattr(os, "getuid") else 0, "pid_domain": self.domain}

    def cli(self, *args, **kwargs):
        kwargs.setdefault("liveness_options", self.opts())
        return self.run_cli(*args, **kwargs)

    def preview(self, *args, **kwargs) -> dict:
        kwargs.setdefault("liveness_options", self.opts())
        return self.run_json(*args, **kwargs)

    def save(self, *args, expect: int = 0, **kwargs) -> str:
        self._plan_counter += 1
        path = os.path.join(self.plans, f"plan-{self._plan_counter}.json")
        code, out, err = self.cli(*args, "--action", "delete", "--save-plan", path, **kwargs)
        self.assertEqual(expect, code, out + err)
        return path

    def apply(self, path: str, *extra, **kwargs):
        return self.cli("--apply-plan", path, *extra, **kwargs)

    def load(self, path: str) -> dict:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def rewrite(self, path: str, document: dict, *, fix_digest: bool = True) -> None:
        if fix_digest:
            document["digest"] = deletion._digest(document)
        os.chmod(path, 0o600)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def everything(self) -> dict:
        """Everything under the test root except plans and the fake /proc."""
        return {k: v for k, v in tree(self.tmp).items() if k.split(os.sep)[0] not in ("plans", "proc")}

    def session_paths(self, session_id: str, project=None) -> list[str]:
        f = self.factory
        directory = f.storage_dir(project)
        return [p for p in (
            os.path.join(directory, f"{session_id}.jsonl"),
            os.path.join(directory, session_id),
            os.path.join(f.config, "file-history", session_id),
            os.path.join(f.config, "session-env", session_id),
            os.path.join(f.config, "debug", f"{session_id}.txt"),
        ) if os.path.lexists(p)]

    def write(self, path: str, data: str = "x", age: dt.timedelta | None = None) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(data)
        if age is not None:
            set_age(path, age)
            set_age(os.path.dirname(path), age)  # adding an entry touched the directory
        return path

    def full_session(self, age: dt.timedelta, project=None, **kwargs) -> str:
        """A session with every kind of per-session data Claude Code keeps."""
        f = self.factory
        sid = f.session(project, age=age, title="Private title", prompt="private prompt text",
                        subagents=(("a1", age),), tool_results=(("r1.txt", age),),
                        file_history=age, session_env=age, **kwargs)
        session_dir = os.path.join(f.storage_dir(project), sid)
        self.write(os.path.join(session_dir, "mcp-tasks", "task-1.json"), "{}", age)
        self.write(os.path.join(session_dir, "custom-title.json"), json.dumps({"customTitle": "Private title"}), age)
        self.write(os.path.join(f.config, "debug", f"{sid}.txt"), "log", age)
        for directory in (os.path.join(session_dir, "mcp-tasks"), session_dir):
            set_age(directory, age)
        return sid


class PreviewTests(DeletionCase):
    def test_delete_preview_is_read_only_and_reports_the_capability(self) -> None:
        old = self.full_session(40 * DAY)
        self.factory.session(age=2 * DAY)
        before = self.everything()
        for extra in ((), ("--verbose",)):
            code, out, err = self.cli("--older-than", "7d", "--action", "delete", *extra)
            self.assertEqual(0, code, err)
            self.assertIn("nothing is deleted", out)
        report = self.preview("--older-than", "7d", "--action", "delete")
        self.assertEqual(before, self.everything())
        validate(report, SCHEMA)
        self.assertEqual("delete", report["policy"]["requested_action"])
        selected = [g for g in report["groups"] if g["status"] == "selected"]
        self.assertEqual([old], [g["group_id"] for g in selected])
        self.assertEqual("delete", selected[0]["proposed_action"])
        self.assertTrue(all(g["proposed_action"] is None for g in report["groups"] if g["status"] != "selected"))
        delete = report["capabilities"]["actions"]["delete"]
        self.assertEqual((True, True, "irreversible", None),
                         (delete["supported"], delete["requires_saved_plan"], delete["reversibility"],
                          delete["unsupported_reason"]))
        self.assertFalse(report["capabilities"]["actions"]["archive"]["supported"])
        self.assertFalse(report["capabilities"]["actions"]["quarantine"]["supported"])
        self.assertFalse(report["capabilities"]["pinning"])

    def test_other_platforms_do_not_offer_deletion(self) -> None:
        self.factory.session(age=40 * DAY)
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform):
                self.platform = platform
                report = self.preview("--older-than", "7d", "--action", "delete")
                delete = report["capabilities"]["actions"]["delete"]
                self.assertFalse(delete["supported"])
                self.assertEqual("blocked-by-safety-requirements", delete["unsupported_reason"])
                self.assertIn("active-session-detection-incomplete",
                              report["extensions"]["claude_code"]["mutation_blockers"])
                before = self.everything()
                path = self.save("--older-than", "7d", expect=1)
                self.assertFalse(os.path.exists(path))
                self.assertEqual(before, self.everything())


@requires_posix_deletion
class SavedPlanTests(DeletionCase):
    def test_plan_is_private_and_holds_no_content(self) -> None:
        sid = self.full_session(40 * DAY)
        path = self.save("--older-than", "7d")
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("Private title", text)
        self.assertNotIn("private prompt text", text)
        document = json.loads(text)
        self.assertEqual(("claude-session-cleanup-plan", 1, "saved"),
                         (document["format"], document["version"], document["state"]["status"]))
        self.assertEqual([sid], [g["group_id"] for g in document["groups"]])
        kinds = {t["path"].split("/")[0] for t in document["groups"][0]["targets"]}
        self.assertEqual({"projects", "file-history", "session-env", "debug"}, kinds)
        for target in document["groups"][0]["targets"]:
            self.assertEqual({"path", "type", "size", "mtime_ns", "ctime_ns", "dev", "ino", "session_id"},
                             set(target))

    def test_save_plan_refuses_existing_files_and_the_config_directory(self) -> None:
        self.factory.session(age=40 * DAY)
        existing = self.write(os.path.join(self.plans, "existing.json"), "keep")
        code, _, err = self.cli("--older-than", "7d", "--action", "delete", "--save-plan", existing)
        self.assertEqual(1, code)
        self.assertIn("already exists", err)
        with open(existing) as handle:
            self.assertEqual("keep", handle.read())
        inside = os.path.join(self.factory.config, "plan.json")
        code, _, err = self.cli("--older-than", "7d", "--action", "delete", "--save-plan", inside)
        self.assertEqual(1, code)
        self.assertFalse(os.path.exists(inside))

    def test_nothing_deletable_saves_no_plan(self) -> None:
        self.factory.session(age=2 * DAY)
        path = os.path.join(self.plans, "none.json")
        code, out, _ = self.cli("--older-than", "7d", "--action", "delete", "--save-plan", path)
        self.assertEqual(0, code)
        self.assertIn("No plan saved", out)
        self.assertFalse(os.path.exists(path))


@requires_posix_deletion
class ApplyTests(DeletionCase):
    def populate_shared_state(self) -> dict:
        """Claude Code state that is never a deletion target, plus Codex-like state."""
        f = self.factory
        storage = f.storage_dir()
        paths = {
            "memory": self.write(os.path.join(storage, "memory", "MEMORY.md"), "notes"),
            "history": self.write(os.path.join(f.config, "history.jsonl"), "{}\n"),
            "settings": f.user_settings({"cleanupPeriodDays": 3650}),
            "claude_json": self.write(os.path.join(self.home, ".claude.json"), "{}"),
            "paste": self.write(os.path.join(f.config, "paste-cache", "a.txt"), "p"),
            "codex_rollout": self.write(os.path.join(
                self.codex, "sessions", "2026", "09", "01",
                f"rollout-2026-09-01T00-00-00-{session_uuid(0xC0DE)}.jsonl"), '{"type":"session_meta"}\n'),
            "codex_config": self.write(os.path.join(self.codex, "config.toml"), "model = 'x'\n"),
            "project_file": self.write(os.path.join(f.default_project, "README.md"), "project"),
        }
        return paths

    def test_reviewed_plan_deletes_exactly_the_selected_groups(self) -> None:
        f = self.factory
        shared = self.populate_shared_state()
        child = f.session(age=35 * DAY, subagents=(("c1", 35 * DAY),))
        old = self.full_session(40 * DAY, continued_in=(child,))
        left = os.path.join(f.config, "tasks", old)
        self.write(os.path.join(left, "1.json"), "{}", 40 * DAY)
        keep = f.session(age=2 * DAY, tool_results=(("k.txt", 2 * DAY),))
        other = f.project("other")
        other_old = f.session(other, age=40 * DAY)
        doomed = self.session_paths(old) + self.session_paths(child)
        before = self.everything()
        path = self.save("--older-than", "7d")
        self.assertEqual(before, self.everything())
        document = self.load(path)
        self.assertEqual([old], [g["group_id"] for g in document["groups"]])
        self.assertEqual({old, child}, {m["session_id"] for m in document["groups"][0]["members"]})
        self.assertEqual([f"tasks/{old}"], document["groups"][0]["left_in_place"])

        code, out, err = self.apply(path)
        self.assertEqual(0, code, out + err)
        self.assertIn("Deleted", out)
        self.assertIn(f"tasks/{old}", out)
        for item in doomed:
            self.assertFalse(os.path.lexists(item), item)
        after = self.everything()
        removed = {k for k in before if k not in after}
        config_rel = os.path.relpath(f.config, self.tmp)
        for key in removed:
            parts = key.split(os.sep)
            self.assertEqual(config_rel, parts[0], key)
            self.assertTrue(old in key or child in key, key)
        for key, value in before.items():
            if key not in removed:
                self.assertEqual(value, after[key], key)
        for item in shared.values():
            self.assertTrue(os.path.exists(item), item)
        self.assertTrue(os.path.isdir(left))
        for sid, project in ((keep, None), (other_old, other)):
            self.assertTrue(os.path.exists(f.transcript_path(sid, project)))
        self.assertEqual("applied", self.load(path)["state"]["status"])
        report = self.preview("--older-than", "7d")
        self.assertNotIn(old, {g["group_id"] for g in report["groups"]})

    def test_replayed_plan_is_refused(self) -> None:
        self.factory.session(age=40 * DAY)
        path = self.save("--older-than", "7d")
        self.assertEqual(0, self.apply(path)[0])
        code, _, err = self.apply(path)
        self.assertEqual(2, code)
        self.assertIn("already used", err)

    def test_unusable_plans_are_refused_without_changes(self) -> None:
        self.factory.session(age=40 * DAY)
        path = self.save("--older-than", "7d")
        good = self.load(path)
        before = self.everything()
        cases = {
            "missing": (os.path.join(self.plans, "missing.json"), None),
            "not json": (self.write(os.path.join(self.plans, "broken.json"), "{oops"), None),
        }
        edited = dict(good, policy=dict(good["policy"], keep_latest=1))
        cases["edited"] = (self.write(os.path.join(self.plans, "edited.json"), json.dumps(edited)), None)
        for name, change in (
            ("format", {"format": "codex-session-cleanup-plan"}),
            ("traversal", {"groups": [dict(good["groups"][0], targets=[
                dict(good["groups"][0]["targets"][0], path="projects/../../outside")])]}),
            ("absolute", {"groups": [dict(good["groups"][0], targets=[
                dict(good["groups"][0]["targets"][0], path="/etc/passwd")])]}),
            ("type", {"groups": [dict(good["groups"][0], targets=[
                dict(good["groups"][0]["targets"][0], type="symlink")])]}),
            ("duplicate", {"groups": [dict(good["groups"][0], targets=good["groups"][0]["targets"] * 2)]}),
        ):
            forged = json.loads(json.dumps(good))
            forged.update(change)
            target = os.path.join(self.plans, f"{name}.json")
            self.write(target, "")
            self.rewrite(target, forged)
            cases[name] = (target, None)
        for name, (target, _) in cases.items():
            with self.subTest(case=name):
                code, out, err = self.apply(target)
                self.assertEqual(2, code, out + err)
                self.assertIn("Nothing was deleted", err)
        self.assertEqual(before, self.everything())

    def test_forged_targets_outside_the_session_are_refused(self) -> None:
        f = self.factory
        sid = f.session(age=40 * DAY)
        settings = f.user_settings({"cleanupPeriodDays": 30})
        path = self.save("--older-than", "7d")
        document = self.load(path)
        extra = deletion._record(deletion.ConfigDir(f.config, "x"), "settings.json")
        document["groups"][0]["targets"].append(dict(extra, session_id=sid))
        self.rewrite(path, document)
        code, out, err = self.apply(path)
        self.assertEqual(3, code, out + err)
        self.assertTrue(os.path.exists(settings))
        self.assertTrue(os.path.exists(f.transcript_path(sid)))

    def test_plan_from_another_context_is_refused(self) -> None:
        f = self.factory
        f.session(age=40 * DAY)
        cases = []
        path = self.save("--older-than", "7d")
        cases.append(("other machine", path, {"opts": dict(self.opts(), pid_domain="linux:other:pid:[2]")}))
        path = self.save("--older-than", "7d")
        cases.append(("other directory", path, {"cwd": f.project("elsewhere")}))
        path = self.save("--older-than", "7d")
        cases.append(("expired", path, {"extra": ("--now", "2026-10-26T12:00:01Z")}))
        path = self.save("--older-than", "7d")
        cases.append(("clock went back", path, {"extra": ("--now", "2026-10-25T11:00:00Z")}))
        path = self.save("--older-than", "7d")
        cases.append(("other config", path, {"env": {"CLAUDE_CONFIG_DIR": ConfigFactory(
            os.path.join(self.tmp, "config-2"), os.path.join(self.tmp, "work-2")).config}}))
        path = self.save("--older-than", "7d")
        cases.append(("other tool version", path, {"version": "9.9.9"}))
        for name, plan, change in cases:
            with self.subTest(case=name):
                kwargs = {"liveness_options": change.get("opts", self.opts())}
                if "cwd" in change:
                    kwargs["cwd"] = change["cwd"]
                if "env" in change:
                    kwargs["env"] = change["env"]
                with mock.patch.object(deletion, "__version__", change.get("version", deletion.__version__)):
                    code, out, err = self.cli("--apply-plan", plan, *change.get("extra", ()), **kwargs)
                self.assertEqual(2, code, out + err)
                self.assertEqual(1, len([n for n in os.listdir(f.storage_dir()) if n.endswith(".jsonl")]))

    def test_locked_plan_is_refused(self) -> None:
        self.factory.session(age=40 * DAY)
        path = self.save("--older-than", "7d")
        with mock.patch.object(deletion, "lock", side_effect=deletion.PlanLocked("busy")):
            code, _, err = self.apply(path)
        self.assertEqual(4, code)
        self.assertIn("Nothing was deleted", err)
        self.assertEqual("saved", self.load(path)["state"]["status"])

    def test_partial_failure_is_reported_and_the_plan_cannot_be_reused(self) -> None:
        sid = self.full_session(40 * DAY)
        path = self.save("--older-than", "7d")
        real_unlink = os.unlink
        calls = []

        def failing_unlink(name, *args, **kwargs):
            calls.append(name)
            if len(calls) == 3:
                raise PermissionError(13, "Permission denied")
            return real_unlink(name, *args, **kwargs)

        with mock.patch.object(os, "unlink", failing_unlink):
            code, out, err = self.apply(path)
        self.assertEqual(1, code)
        self.assertIn("STOPPED", out)
        self.assertNotIn("Deleted 0 files", out)
        state = self.load(path)["state"]
        self.assertEqual(("failed", 2), (state["status"], len(state["deleted"])))
        self.assertTrue(os.path.exists(self.factory.transcript_path(sid)), "transcript is removed last")
        self.assertEqual(2, self.apply(path)[0])


@requires_posix_deletion
class StalePlanTests(DeletionCase):
    """Anything that changes between review and apply refuses the whole plan, with zero deletions."""

    def assert_stale(self, path: str, message: str = "") -> None:
        before = self.everything()
        code, out, err = self.apply(path)
        self.assertEqual(3, code, out + err)
        self.assertIn("Nothing was deleted", err)
        if message:
            self.assertIn(message, err)
        self.assertEqual(before, self.everything())
        self.assertEqual("saved", self.load(path)["state"]["status"])

    def planned(self, **kwargs) -> tuple[str, str]:
        sid = self.full_session(40 * DAY, **kwargs)
        return sid, self.save("--older-than", "7d")

    def test_new_activity_blocks(self) -> None:
        sid, path = self.planned()
        with open(self.factory.transcript_path(sid), "a") as handle:
            handle.write("{}\n")
        self.assert_stale(path)

    def test_recent_activity_now_protects(self) -> None:
        sid, path = self.planned()
        set_age(self.factory.transcript_path(sid), dt.timedelta(minutes=5))
        self.assert_stale(path, "no longer deletable")

    def test_running_root_blocks(self) -> None:
        sid, path = self.planned()
        fake_process(self.proc, 4000, "77", argv=("claude",), cwd=self.tmp)
        self.factory.registry(4000, sid, "77")
        self.assert_stale(path, "no longer deletable")

    def test_running_descendant_blocks(self) -> None:
        child = self.factory.session(age=35 * DAY)
        sid, path = self.planned(continued_in=(child,))
        fake_process(self.proc, 4001, "78", argv=("claude", "--resume", child), cwd=self.tmp)
        self.assert_stale(path, "no longer deletable")

    def test_claude_process_in_the_project_blocks(self) -> None:
        _, path = self.planned()
        fake_process(self.proc, 4002, "79", exe="/opt/claude/versions/2.1.316", argv=("claude",),
                     cwd=self.factory.default_project)
        self.assert_stale(path, "no longer deletable")

    def test_new_subagent_blocks(self) -> None:
        sid, path = self.planned()
        self.write(os.path.join(self.factory.storage_dir(), sid, "subagents", "agent-new.jsonl"), "{}\n", 40 * DAY)
        self.assert_stale(path)

    def test_new_continuation_blocks(self) -> None:
        f = self.factory
        sid, path = self.planned()
        new_child = f.session(age=39 * DAY)
        transcript = f.transcript_path(sid)
        with open(transcript, "a") as handle:
            handle.write(json.dumps({"type": "continued-in", "continuedInSessionId": new_child, "sessionId": sid}) + "\n")
        set_age(transcript, 40 * DAY)
        self.assert_stale(path)

    def test_changed_content_with_restored_timestamp_blocks(self) -> None:
        sid, path = self.planned()
        result = os.path.join(self.factory.storage_dir(), sid, "tool-results", "r1.txt")
        st = os.stat(result)
        with open(result, "w") as handle:
            handle.write("y" * st.st_size)
        os.utime(result, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assert_stale(path)

    def test_replaced_file_blocks(self) -> None:
        sid, path = self.planned()
        result = os.path.join(self.factory.storage_dir(), sid, "tool-results", "r1.txt")
        st = os.stat(result)
        with open(result, "rb") as handle:
            data = handle.read()
        os.unlink(result)
        with open(result, "wb") as handle:
            handle.write(data)
        os.utime(result, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assert_stale(path)

    def test_unexpected_file_blocks(self) -> None:
        sid, path = self.planned()
        self.write(os.path.join(self.factory.storage_dir(), sid, "notes.md"), "mine", 40 * DAY)
        self.assert_stale(path)

    @requires_symlinks
    def test_transcript_replaced_by_symlink_blocks(self) -> None:
        sid, path = self.planned()
        transcript = self.factory.transcript_path(sid)
        outside = self.write(os.path.join(self.tmp, "outside.jsonl"), "{}")
        os.rename(transcript, transcript + ".moved")
        os.symlink(outside, transcript)
        self.assert_stale(path)
        self.assertTrue(os.path.exists(outside))

    @requires_symlinks
    def test_session_directory_replaced_by_symlink_blocks(self) -> None:
        sid, path = self.planned()
        session_dir = os.path.join(self.factory.storage_dir(), sid)
        outside = os.path.join(self.tmp, "outside-dir")
        os.rename(session_dir, outside)
        os.symlink(outside, session_dir)
        self.assert_stale(path)
        self.assertTrue(os.path.isdir(outside))

    def test_codex_overlap_blocks(self) -> None:
        _, path = self.planned()
        code, out, err = self.apply(path, env={"CODEX_HOME": os.path.dirname(self.factory.config)})
        self.assertEqual(3, code, out + err)
        self.assertIn("Codex", err)

    def test_swap_after_preflight_stops_before_touching_the_swapped_file(self) -> None:
        sid, path = self.planned()
        target = os.path.join(self.factory.storage_dir(), sid, "tool-results", "r1.txt")
        original_preflight = deletion.preflight

        def preflight_then_swap(config, targets):
            problems = original_preflight(config, targets)
            os.unlink(target)
            self.write(target, "z" * 10)
            return problems

        with mock.patch.object(deletion, "preflight", preflight_then_swap):
            code, out, _ = self.apply(path)
        self.assertEqual(1, code)
        self.assertIn("STOPPED", out)
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.exists(self.factory.transcript_path(sid)))


@requires_posix_deletion
class UnsafeDataTests(DeletionCase):
    """Data the tool cannot attribute safely is never planned for deletion."""

    def assert_not_planned(self, sid: str, blocker: str | None = None, status: str | None = None) -> None:
        self.factory.session(age=40 * DAY)  # something deletable, so a plan is written
        report = self.preview("--older-than", "7d", "--action", "delete")
        group = next(g for g in report["groups"] if g["group_id"] == sid)
        if status:
            self.assertEqual(status, group["status"])
        if blocker:
            self.assertIn(blocker, group["mutation_blockers"])
        path = os.path.join(self.plans, f"{sid}.json")
        code, out, err = self.cli("--older-than", "7d", "--action", "delete", "--save-plan", path)
        self.assertEqual(0, code, err)
        if os.path.exists(path):
            document = self.load(path)
            self.assertNotIn(sid, [g["group_id"] for g in document["groups"]])
            if blocker:
                self.assertIn(sid, [g["group_id"] for g in document["blocked_groups"]])

    def test_malformed_groups_are_excluded(self) -> None:
        sid = self.factory.session(age=40 * DAY, raw=b"")
        self.factory.session(age=40 * DAY)
        self.assert_not_planned(sid, status="excluded")

    def test_unrecognized_session_data_blocks_the_group(self) -> None:
        sid = self.full_session(40 * DAY)
        self.write(os.path.join(self.factory.storage_dir(), sid, "subagents", "unknown.bin"), "?", 40 * DAY)
        self.assert_not_planned(sid, "unrecognized-session-data", "selected")

    def test_unknown_associated_artifact_blocks_the_group(self) -> None:
        sid = self.factory.session(age=40 * DAY)
        self.write(os.path.join(self.factory.config, "todos", f"{sid}-agent.json"), "[]", 40 * DAY)
        self.assert_not_planned(sid, "unknown-associated-artifact", "selected")

    @requires_symlinks
    def test_symlinks_inside_session_data_exclude_the_group(self) -> None:
        for where in ("subagents/agent-x.jsonl", "tool-results/x.txt"):
            with self.subTest(where=where):
                sid = self.factory.session(age=40 * DAY)
                outside = self.write(os.path.join(self.tmp, f"secret-{sid}"), "secret")
                link = os.path.join(self.factory.storage_dir(), sid, *where.split("/"))
                os.makedirs(os.path.dirname(link), exist_ok=True)
                os.symlink(outside, link)
                self.assert_not_planned(sid, status="excluded")
                self.assertTrue(os.path.exists(outside))

    @requires_symlinks
    def test_symlinked_transcript_is_excluded(self) -> None:
        f = self.factory
        outside = self.write(os.path.join(self.tmp, "real.jsonl"), "{}\n")
        sid = session_uuid(0xABC)
        os.symlink(outside, f.transcript_path(sid))
        report = self.preview("--older-than", "7d", "--action", "delete")
        self.assertNotIn(sid, [g["group_id"] for g in report["groups"] if g["status"] == "selected"])

    def test_codex_like_transcripts_are_never_claude_sessions(self) -> None:
        f = self.factory
        codex_id = session_uuid(0xC0DE)
        record = {"timestamp": "2026-09-01T00:00:00.000Z", "type": "session_meta",
                  "payload": {"id": codex_id, "cwd": f.default_project, "originator": "codex_cli_rs"}}
        raw = (json.dumps(record) + "\n").encode()
        f.session(session_id=codex_id, raw=raw, age=40 * DAY)
        rollout = self.write(os.path.join(f.storage_dir(), f"rollout-2026-09-01T00-00-00-{session_uuid(0xC0DF)}.jsonl"),
                             json.dumps(record) + "\n", 40 * DAY)
        report = self.preview("--older-than", "7d", "--action", "delete")
        group = next(g for g in report["groups"] if g["group_id"] == codex_id)
        self.assertEqual("excluded", group["status"])
        self.assertIn("project-identity-unknown", group["reasons"])
        self.assertNotIn(session_uuid(0xC0DF), [g["group_id"] for g in report["groups"]])
        self.assertTrue(os.path.exists(rollout))


@requires_posix_deletion
@requires_symlinks
class ProcessScanTests(DeletionCase):
    def scan(self, **kwargs):
        return processes.scan(os.path.join(self.factory.config, "projects"), platform="linux",
                              proc_root=self.proc, own_pid=999, uid=os.getuid(), **kwargs)

    def test_claude_processes_are_recognized_and_others_ignored(self) -> None:
        project = self.factory.default_project
        fake_process(self.proc, 10, argv=("claude", "--continue"), exe="/home/u/.local/share/claude/versions/2.1.316",
                     cwd=project)
        fake_process(self.proc, 11, argv=("node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"),
                     exe="/usr/bin/node", cwd=project)
        fake_process(self.proc, 12, argv=("/bin/bash", "-c", "source /home/u/.claude/shell-snapshots/s.sh"),
                     exe="/usr/bin/bash", cwd=project)
        fake_process(self.proc, 13, argv=("python3", "-m", "claude_session_cleanup"), exe="/usr/bin/python3",
                     cwd=project)
        fake_process(self.proc, 14, argv=("vim", "claude"), exe="/usr/bin/vim", cwd=project)
        scan = self.scan()
        self.assertTrue(scan.available)
        self.assertEqual([10, 11], [p.pid for p in scan.processes])
        self.assertEqual(0, scan.uninspectable)

    def test_unreadable_working_directory_protects_everything_unless_the_process_exited(self) -> None:
        f = self.factory
        f.session(age=40 * DAY)
        f.session(f.project("other"), age=40 * DAY)
        fake_process(self.proc, 40, argv=("claude",), state="Z")  # exited child, cwd gone
        report = self.preview("--older-than", "7d", "--project", "all")
        self.assertEqual({"selected"}, {g["status"] for g in report["groups"]})
        fake_process(self.proc, 41, argv=("claude",))  # alive, cwd unreadable
        report = self.preview("--older-than", "7d", "--project", "all")
        self.assertEqual({("protected", ("possibly-running",))},
                         {(g["status"], tuple(g["reasons"])) for g in report["groups"]})

    def test_unavailable_off_linux(self) -> None:
        for platform in ("darwin", "win32"):
            self.assertFalse(processes.scan("/x", platform=platform, proc_root=self.proc).available)

    def test_uninspectable_processes_block_deletion(self) -> None:
        base = fake_process(self.proc, 20, argv=())
        os.unlink(os.path.join(base, "cmdline"))
        self.factory.session(age=40 * DAY)
        report = self.preview("--older-than", "7d")
        self.assertFalse(report["capabilities"]["actions"]["delete"]["supported"])
        self.assertEqual(["process-scan-incomplete"], report["extensions"]["claude_code"]["mutation_blockers"])
        self.save("--older-than", "7d", expect=1)

    def test_process_associations_protect_sessions(self) -> None:
        f = self.factory
        project = f.default_project
        other = f.project("other")
        nested = f.project("repo", "sub")
        a = f.session(age=40 * DAY)
        b = f.session(age=40 * DAY)
        c = f.session(other, age=40 * DAY)
        d = f.session(other, age=40 * DAY)
        e = f.session(f.project("third"), age=40 * DAY)

        def statuses():
            report = self.preview("--older-than", "7d", "--project", "all")
            return {g["group_id"]: (g["status"], tuple(g["reasons"])) for g in report["groups"]}

        self.assertTrue(all(s == "selected" for s, _ in statuses().values()))
        fake_process(self.proc, 30, argv=("claude",), cwd=nested)  # inside the project
        fake_process(self.proc, 31, argv=("claude", "--resume", d), cwd="/")
        fake_process(self.proc, 32, argv=("claude",), cwd=self.tmp,
                     open_files=(f.transcript_path(e, f.project("third")),))
        result = statuses()
        for sid in (a, b, d, e):
            self.assertEqual(("protected", ("possibly-running",)), result[sid], sid)
        self.assertEqual("selected", result[c][0])

    def test_invoking_process_chain_and_other_config_dirs_do_not_protect_the_project(self) -> None:
        f = self.factory
        sid = f.session(age=40 * DAY)
        fake_process(self.proc, 999, argv=("python3",), ppid=50)
        fake_process(self.proc, 50, argv=("claude",), cwd=f.default_project, ppid=1)
        fake_process(self.proc, 60, argv=("claude",), cwd=f.default_project,
                     environ={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "another-config")})
        report = self.preview("--older-than", "7d")
        self.assertEqual("selected", report["groups"][0]["status"])
        fake_process(self.proc, 61, argv=("claude",), cwd=f.default_project, environ={"HOME": self.home})
        report = self.preview("--older-than", "7d")
        self.assertEqual("selected", report["groups"][0]["status"], "HOME/.claude is not this config dir")
        fake_process(self.proc, 62, argv=("claude",), cwd=f.default_project)
        report = self.preview("--older-than", "7d")
        self.assertEqual(("protected", ["possibly-running"]), (report["groups"][0]["status"],
                                                              report["groups"][0]["reasons"]))
        self.assertIsNotNone(sid)

    def test_registry_entry_from_another_pid_namespace_is_possibly_running(self) -> None:
        f = self.factory
        sid = f.session(age=40 * DAY)
        path = f.registry(70, sid, "5")
        with open(path) as handle:
            record = json.load(handle)
        record["pidDomain"] = "linux:other:pid:[9]"
        with open(path, "w") as handle:
            json.dump(record, handle)
        report = self.preview("--older-than", "7d")
        self.assertEqual(["possibly-running"], report["groups"][0]["reasons"])
        record["pidDomain"] = DOMAIN
        with open(path, "w") as handle:
            json.dump(record, handle)
        report = self.preview("--older-than", "7d")
        self.assertEqual("selected", report["groups"][0]["status"], "pid 70 is not running here")

    def test_pid_domain_matches_claude_code_format(self) -> None:
        machine = self.write(os.path.join(self.tmp, "machine-id"), "abc123\n")
        os.makedirs(os.path.join(self.proc, "self", "ns"))
        os.symlink("pid:[4026531836]", os.path.join(self.proc, "self", "ns", "pid"))
        self.assertEqual("linux:abc123:pid:[4026531836]",
                         processes.pid_domain("linux", self.proc, machine_id_path=machine))
        self.assertIsNone(processes.pid_domain("darwin", self.proc, machine_id_path=machine))


@requires_posix_deletion
class PolicyMatrixTests(DeletionCase):
    """End-to-end deletion under several policies against an isolated writable configuration.

    Each policy gets a freshly built configuration. Expected selections are
    computed independently from the scenario table, not by the tool.
    """

    # name, project, group activity age, state; descendants listed separately.
    SCENARIO = (
        ("g1", "repo", 2 * HOUR, "idle"),
        ("g2", "repo", 12 * HOUR, "idle"),
        ("g3", "repo", 3 * DAY, "idle"),
        ("g4", "repo", 30 * HOUR, "idle"),  # root 10 days old, continuation 30 hours
        ("g5", "repo", dt.timedelta(minutes=30), "recent"),  # root 20 days old, recent subagent
        ("g6", "repo", 15 * DAY, "running"),
        ("g7", "repo", 40 * DAY, "malformed"),
        ("g8", "repo", 40 * DAY, "idle"),
        ("o1", "other", 40 * DAY, "idle"),
        ("o2", "other", 5 * HOUR, "idle"),
    )

    def build(self) -> dict:
        f = self.factory = ConfigFactory(os.path.join(self.tmp, f"cfg-{self._plan_counter}"),
                                         os.path.join(self.tmp, f"ws-{self._plan_counter}"))
        projects = {"repo": f.default_project, "other": f.project("other")}
        ids = {}
        for name, project, age, state in self.SCENARIO:
            path = projects[project]
            if name == "g4":
                child = f.session(path, age=30 * HOUR)
                ids[name] = f.session(path, age=10 * DAY, continued_in=(child,), subagents=(("x", 10 * DAY),))
                ids["g4-child"] = child
            elif name == "g5":
                ids[name] = f.session(path, age=20 * DAY, subagents=(("r", dt.timedelta(minutes=30)),))
            elif state == "malformed":
                ids[name] = f.session(path, age=age, raw=b"")
            else:
                ids[name] = f.session(path, age=age, tool_results=(("t.txt", age),), file_history=age)
        pid = 5000 + self._plan_counter
        fake_process(self.proc, pid, str(pid), argv=("claude",), cwd=self.tmp)
        f.registry(pid, ids["g6"], str(pid))
        self.write(os.path.join(self.codex, "sessions", "2026", "09", "01",
                                f"rollout-2026-09-01T00-00-00-{ids['g8']}.jsonl"), "{}\n")
        self.write(os.path.join(f.storage_dir(), "memory", "MEMORY.md"), "m")
        self.write(os.path.join(f.config, "history.jsonl"), "{}\n")
        return ids

    def expected(self, older_than, keep_latest, scope) -> set[str]:
        rows = [r for r in self.SCENARIO if scope == "all" or r[1] == "repo"]
        rankable = sorted((r for r in rows if r[3] != "malformed"), key=lambda r: (r[2], r[0]))
        selected = set()
        for rank, (name, _, age, state) in enumerate(rankable, start=1):
            if state in ("recent", "running"):
                continue
            if older_than is not None and not age > older_than:
                continue
            if keep_latest is not None and rank <= keep_latest:
                continue
            selected.add(name)
        return selected

    POLICIES = (
        (("--older-than", "1h"), HOUR, None, "current"),
        (("--older-than", "24h"), DAY, None, "current"),
        (("--older-than", "7d"), 7 * DAY, None, "current"),
        (("--keep-latest", "1"), None, 1, "current"),
        (("--keep-latest", "3"), None, 3, "current"),
        (("--older-than", "24h", "--keep-latest", "2"), DAY, 2, "current"),
        (("--older-than", "7d", "--project", "all"), 7 * DAY, None, "all"),
        (("--keep-latest", "3", "--project", "all"), None, 3, "all"),
    )

    def test_policies_delete_exactly_the_expected_groups(self) -> None:
        for args, older_than, keep_latest, scope in self.POLICIES:
            with self.subTest(policy=" ".join(args)):
                self._plan_counter += 1
                ids = self.build()
                names = {value: key for key, value in ids.items()}
                expected = self.expected(older_than, keep_latest, scope)
                self.assertTrue(expected, "every policy in the matrix selects something")
                report = self.preview(*args, "--action", "delete")
                selected = {names[g["group_id"]] for g in report["groups"] if g["status"] == "selected"}
                self.assertEqual(expected, selected)
                for group in report["groups"]:
                    if group["status"] == "selected":
                        self.assertEqual([], group["mutation_blockers"])
                path = self.save(*args)
                self.assertEqual(expected, {names[g["group_id"]] for g in self.load(path)["groups"]})
                doomed = {ids[n] for n in expected} | ({ids["g4-child"]} if "g4" in expected else set())
                before = self.everything()
                code, out, err = self.apply(path)
                self.assertEqual(0, code, out + err)
                after = self.everything()
                removed = {k for k in before if k not in after}
                self.assertTrue(removed)
                for key in removed:
                    self.assertTrue(any(sid in key for sid in doomed), key)
                for key in before:
                    if key in after:
                        self.assertEqual(before[key], after[key], key)
                for name, sid in ids.items():
                    base = name.split("-")[0]
                    project = self.factory.project("other") if base.startswith("o") else None
                    exists = os.path.lexists(self.factory.transcript_path(sid, project))
                    self.assertEqual(base not in expected, exists, name)
                codex = os.path.relpath(self.codex, self.tmp)
                self.assertTrue(any(k.startswith(codex) for k in after))
