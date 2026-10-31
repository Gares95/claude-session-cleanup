import datetime as dt
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

from claude_session_cleanup import cli
from schema_check import validate
from support import IsolatedTestCase, allow_processes, snapshot_tree

DAY = dt.timedelta(days=1)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "contract", "preview-v1.schema.json"), encoding="utf-8") as _handle:
    SCHEMA = json.load(_handle)


class ArgumentTests(IsolatedTestCase):
    def usage_error(self, *args: str) -> str:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            cli.run(list(args), environment={"CLAUDE_CONFIG_DIR": self.factory.config},
                    cwd=self.factory.default_project, stdout=io.StringIO(), stderr=err)
        self.assertEqual(2, raised.exception.code)
        return err.getvalue()

    def test_a_selector_is_required(self) -> None:
        self.assertIn("at least one of --older-than or --keep-latest", self.usage_error())

    def test_invalid_values_are_rejected(self) -> None:
        for args in (("--older-than", "0d"), ("--older-than", "7"), ("--older-than", "1w"),
                     ("--older-than", "-3d"), ("--older-than", "36501d"), ("--keep-latest", "0"),
                     ("--keep-latest", "-1"), ("--keep-latest", "1.5"), ("--project", "parent"),
                     ("--older-than", "7d", "--min-idle", "0h"), ("--older-than", "7d", "--action", "archive"),
                     ("--older-than", "7d", "--action", "quarantine"), ("--older-than", "7d", "--save-plan", "p"),
                     ("--older-than", "7d", "--apply"), ("--older-than", "7d", "--action", "delete", "--apply"),
                     ("--apply-plan", "p", "--older-than", "7d"), ("--apply-plan", "p", "--json")):
            with self.subTest(args=args):
                self.usage_error(*args)

    def test_apply_without_a_plan_explains_the_saved_plan_workflow(self) -> None:
        self.assertIn("requires a reviewed saved plan", self.usage_error("--older-than", "7d", "--action", "delete", "--apply"))

    def test_help_documents_scope_and_semantics(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.run(["--help"])
        text = out.getvalue()
        for phrase in ("read-only preview unless --apply-plan", "intersection", "--keep-latest counts root groups",
                       "never launches Claude Code", "no archive, quarantine, or restore", "irreversible",
                       "There is no --apply", "retained", "excluded"):
            self.assertIn(phrase, " ".join(text.split()))

    def test_missing_config_dir_is_a_safety_stop(self) -> None:
        code, out, err = self.run_cli("--older-than", "7d", env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "x")})
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("Safety stop", err)
        self.assertIn("No changes made.", err)


class OutputTests(IsolatedTestCase):
    def populate(self):
        self.factory.user_settings({"cleanupPeriodDays": 30})
        old = self.factory.session(age=45 * DAY, title="Fix \x1b[31mlogin\x1b[0m‮ bug",
                                   subagents=(("a", 45 * DAY),), file_history=45 * DAY)
        recent = self.factory.session(age=2 * DAY)
        excluded = self.factory.session(prompt=None, age=50 * DAY)
        return old, recent, excluded

    def test_json_report_matches_schema(self) -> None:
        self.populate()
        report = self.run_json("--older-than", "30d", "--verbose")
        self.assertEqual([], validate(report, SCHEMA))
        report = self.run_json("--keep-latest", "1", "--project", "all", "--include-titles")
        self.assertEqual([], validate(report, SCHEMA))

    def test_empty_config_report_matches_schema(self) -> None:
        report = self.run_json("--older-than", "30d")
        self.assertEqual([], validate(report, SCHEMA))
        self.assertEqual([], report["groups"])

    def test_contract_envelope(self) -> None:
        report = self.run_json("--older-than", "30d")
        self.assertEqual(("session-cleanup/preview", 1, "claude-code", "preview", False),
                         (report["schema"], report["schema_version"], report["agent"], report["mode"],
                          report["changes_made"]))
        actions = report["capabilities"]["actions"]
        # The test platform has no process scan, so deletion is unsupported here.
        self.assertFalse(any(action["supported"] for action in actions.values()))
        self.assertEqual("not-offered-by-backend", actions["quarantine"]["unsupported_reason"])
        self.assertEqual("blocked-by-safety-requirements", actions["delete"]["unsupported_reason"])
        self.assertTrue(actions["delete"]["requires_saved_plan"])
        self.assertIsNone(report["policy"]["requested_action"])
        self.assertEqual("root-group", report["policy"]["ranking_unit"])
        self.assertEqual("2026-09-25T12:00:00Z", report["policy"]["cutoff"])
        self.assertEqual(("derived", True), (report["activity_timestamp"]["kind"],
                                             report["activity_timestamp"]["conservative"]))

    def test_text_and_json_agree(self) -> None:
        old, recent, excluded = self.populate()
        report = self.run_json("--older-than", "30d", "--include-titles")
        code, text, _ = self.run_cli("--older-than", "30d", "--verbose")
        self.assertEqual(0, code)
        for group in report["groups"]:
            self.assertIn(group["group_id"][:8], text)
        totals = report["totals"]["groups"]
        self.assertIn(f"Would select  {totals['selected']:>6} ", text)
        self.assertIn(f"Retained      {totals['retained']:>6} ", text)
        self.assertIn(f"Protected     {totals['protected']:>6} ", text)
        self.assertIn(f"Excluded      {totals['excluded']:>6} ", text)
        self.assertTrue(text.rstrip().endswith("No changes made."))

    def test_titles_are_sanitized_and_omitted_from_json_by_default(self) -> None:
        old, _, _ = self.populate()
        code, text, _ = self.run_cli("--older-than", "30d")
        self.assertIn("Fix [31mlogin [0m bug", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("‮", text)
        report = self.run_json("--older-than", "30d")
        self.assertTrue(all(s["title"] is None for g in report["groups"] for s in g["sessions"]))
        self.assertEqual("custom-title", report["groups"][0]["sessions"][0]["title_source"])
        code, text, _ = self.run_cli("--older-than", "30d", "--no-titles")
        self.assertNotIn("login", text)

    def test_full_ids_and_short_ids(self) -> None:
        old, _, _ = self.populate()
        _, short, _ = self.run_cli("--older-than", "30d")
        _, full, _ = self.run_cli("--older-than", "30d", "--full-ids")
        self.assertNotIn(old, short)
        self.assertIn(old, full)

    def test_output_is_deterministic(self) -> None:
        self.populate()
        first = self.run_cli("--older-than", "30d", "--json")
        second = self.run_cli("--older-than", "30d", "--json")
        self.assertEqual(first, second)

    def test_untested_version_is_warned(self) -> None:
        self.factory.session(version="9.9.9")
        report = self.run_json("--older-than", "30d")
        self.assertIn({"code": "claude-code-version-untested", "subject": "9.9.9"}, report["warnings"])


class ReadOnlyTests(IsolatedTestCase):
    def test_preview_changes_nothing_and_launches_nothing(self) -> None:
        # The isolation guard already turns any process launch or signal into a failure.
        self.factory.user_settings({"cleanupPeriodDays": 30})
        for days in (1, 10, 40, 80):
            self.factory.session(age=days * DAY, subagents=(("x", days * DAY),), file_history=days * DAY,
                                 session_env=days * DAY)
        self.factory.registry(4321, self.factory.next_id())
        proc = os.path.join(self.tmp, "proc")
        os.makedirs(proc)
        before = snapshot_tree(self.tmp)
        for args in (("--older-than", "7d"), ("--keep-latest", "1", "--project", "all", "--json", "--verbose")):
            code, _, err = self.run_cli(*args, liveness_options={"platform": "linux", "proc_root": proc})
            self.assertEqual(0, code, err)
        self.assertEqual(before, snapshot_tree(self.tmp))


class EntryPointTests(IsolatedTestCase):
    def test_skill_script_runs_standalone(self) -> None:
        # Run outside the guard: a real interpreter process on a synthetic config directory.
        script = os.path.join(ROOT, "scripts", "cleanup_sessions.py")
        env = dict(os.environ)
        env.update({"CLAUDE_CONFIG_DIR": self.factory.config,
                    "HOME": os.environ["HOME"], "PYTHONDONTWRITEBYTECODE": "1"})
        with allow_processes():
            completed = subprocess.run([sys.executable, "-I", script, "--older-than", "1d"],
                                       cwd=self.factory.default_project, env=env,
                                       capture_output=True, text=True, timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("No changes made.", completed.stdout)
