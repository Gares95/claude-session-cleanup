import datetime as dt
import os

from claude_session_cleanup import activity
from claude_session_cleanup.config import ConfigDir
from support import IsolatedTestCase, fake_process, session_uuid

DAY = dt.timedelta(days=1)


def fake_proc(root: str, pid: int, start_time: str) -> None:
    fake_process(root, pid, start_time, argv=("claude",))


class LivenessTests(IsolatedTestCase):
    def test_linux_liveness_checks_start_time(self) -> None:
        proc = os.path.join(self.tmp, "proc")
        fake_proc(proc, 100, "4242")
        options = {"platform": "linux", "proc_root": proc}
        self.assertEqual("running", activity.process_liveness(100, "4242", **options))
        self.assertEqual("stale-pid", activity.process_liveness(100, "9999", **options))
        self.assertEqual("possibly-running", activity.process_liveness(100, None, **options))
        self.assertEqual("not-running", activity.process_liveness(101, "4242", **options))

    def test_other_posix_uses_signal_zero(self) -> None:
        def alive(pid, sig):
            self.assertEqual(0, sig)

        def gone(pid, sig):
            raise ProcessLookupError

        def denied(pid, sig):
            raise PermissionError

        self.assertEqual("possibly-running", activity.process_liveness(5, "1", platform="darwin", kill=alive))
        self.assertEqual("not-running", activity.process_liveness(5, "1", platform="darwin", kill=gone))
        self.assertEqual("possibly-running", activity.process_liveness(5, "1", platform="darwin", kill=denied))

    def test_windows_never_signals(self) -> None:
        def explode(pid, sig):
            raise AssertionError("os.kill would terminate the process on Windows")

        self.assertEqual("possibly-running", activity.process_liveness(5, "1", platform="win32", kill=explode))


class SignalTests(IsolatedTestCase):
    def config(self) -> ConfigDir:
        return ConfigDir(self.factory.config, "CLAUDE_CONFIG_DIR")

    def test_registry_and_jobs_feed_protections(self) -> None:
        proc = os.path.join(self.tmp, "proc")
        running, stale, gone, unknown = (session_uuid(n) for n in (1, 2, 3, 4))
        fake_proc(proc, 200, "4242")
        fake_proc(proc, 201, "1111")
        self.factory.registry(200, running, "4242")
        self.factory.registry(201, stale, "4242")
        self.factory.registry(202, gone, "4242")
        self.factory.registry(203, unknown, None)
        fake_proc(proc, 203, "5")
        self.factory.job("0123abcd", session_uuid(5), resume=session_uuid(6))
        signals = activity.detect_activity(self.config(), {}, platform="linux", proc_root=proc)
        self.assertEqual(
            {
                running: "running",
                unknown: "possibly-running",
                session_uuid(5): "background-session",
                session_uuid(6): "background-session",
            },
            signals.protections(),
        )

    def test_malformed_registry_entries_are_reported(self) -> None:
        directory = os.path.join(self.factory.config, "sessions")
        os.makedirs(directory)
        with open(os.path.join(directory, "300.json"), "w") as handle:
            handle.write("{broken")
        self.factory.write_json(os.path.join(directory, "301.json"), {"pid": 301, "sessionId": "nope"})
        self.factory.write_json(os.path.join(directory, "302.json"), {"pid": True, "sessionId": session_uuid(1)})
        with open(os.path.join(directory, "303.abcdef.key"), "w") as handle:
            handle.write("ignored")
        signals = activity.detect_activity(self.config(), {}, platform="linux", proc_root=self.tmp)
        self.assertEqual(("sessions/300.json", "sessions/301.json", "sessions/302.json"), signals.registry_unreadable)
        self.assertEqual((), signals.registry)

    def test_invoking_session_id_must_be_valid(self) -> None:
        sid = session_uuid(9)
        valid = activity.detect_activity(self.config(), {"CLAUDE_CODE_SESSION_ID": sid})
        self.assertEqual(sid, valid.invoking_session_id)
        invalid = activity.detect_activity(self.config(), {"CLAUDE_CODE_SESSION_ID": "../x", "CLAUDECODE": "1"})
        self.assertIsNone(invalid.invoking_session_id)
        self.assertTrue(invalid.invoking_session_env_present)

    def test_running_registry_entry_protects_in_preview(self) -> None:
        proc = os.path.join(self.tmp, "proc")
        sid = self.factory.session(age=40 * DAY)
        fake_proc(proc, 400, "77")
        self.factory.registry(400, sid, "77")
        report = self.run_json("--older-than", "7d", liveness_options={"platform": "linux", "proc_root": proc})
        self.assertEqual(["running"], report["groups"][0]["reasons"])
        self.assertEqual("protected", report["groups"][0]["status"])
        claude = report["extensions"]["claude_code"]
        self.assertFalse(claude["activity_detection"]["complete"])
        self.assertEqual([], claude["mutation_blockers"])
        self.assertTrue(report["capabilities"]["actions"]["delete"]["supported"])
        self.assertEqual("partial", report["capabilities"]["active_session_detection"])
