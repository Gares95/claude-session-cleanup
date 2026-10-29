import datetime as dt
import os
import re

from claude_session_cleanup import native_retention as native
from claude_session_cleanup.config import ConfigDir
from support import NOW, IsolatedTestCase

DAY = dt.timedelta(days=1)


class ConfigurationTests(IsolatedTestCase):
    def config(self) -> ConfigDir:
        return ConfigDir(self.factory.config, "CLAUDE_CONFIG_DIR")

    def assess(self, projects=(), managed=()):
        return native.assess_configuration(self.config(), list(projects), managed_dirs=tuple(managed))

    def test_default_applies_when_nothing_is_set(self) -> None:
        result = self.assess()
        self.assertEqual((None, None, (30,), 30), (result.managed_days, result.user_days,
                                                    result.applicable_days, result.lowest_applicable_days))
        self.assertFalse(result.sweep_may_pause)

    def test_project_setting_lower_than_user_setting_is_the_worst_case(self) -> None:
        self.factory.user_settings({"cleanupPeriodDays": 3650})
        project = self.factory.project("strict")
        self.factory.write_json(os.path.join(project, ".claude", "settings.json"), {"cleanupPeriodDays": 2})
        result = self.assess([project, self.factory.default_project])
        self.assertEqual(3650, result.user_days)
        self.assertEqual((2, 3650), result.applicable_days)
        self.assertEqual(2, result.lowest_applicable_days)

    def test_local_settings_at_repository_root_are_read(self) -> None:
        root = self.factory.project("mono")
        os.makedirs(os.path.join(root, ".git"))
        sub = self.factory.project("mono", "pkg")
        self.factory.write_json(os.path.join(root, ".claude", "settings.local.json"), {"cleanupPeriodDays": 5})
        self.assertEqual(5, self.assess([sub]).lowest_applicable_days)

    def test_managed_settings_override_everything(self) -> None:
        managed = os.path.join(self.tmp, "managed")
        self.factory.write_json(os.path.join(managed, "managed-settings.json"), {"cleanupPeriodDays": 90})
        self.factory.write_json(os.path.join(managed, "managed-settings.d", "20-x.json"), {"cleanupPeriodDays": 60})
        self.factory.write_json(os.path.join(managed, "managed-settings.d", ".hidden.json"), {"cleanupPeriodDays": 1})
        self.factory.user_settings({"cleanupPeriodDays": 3})
        result = self.assess(managed=[managed])
        self.assertEqual((60, (60,), 60), (result.managed_days, result.applicable_days, result.lowest_applicable_days))

    def test_server_managed_cache_counts_as_managed(self) -> None:
        self.factory.write_json(os.path.join(self.factory.config, "remote-settings.json"), {"cleanupPeriodDays": 14})
        self.assertEqual(14, self.assess().managed_days)

    def test_invalid_or_unreadable_settings_may_pause_the_sweep(self) -> None:
        for bad in ({"cleanupPeriodDays": 0}, {"cleanupPeriodDays": True}, {"cleanupPeriodDays": "30"}):
            self.factory.user_settings(bad)
            result = self.assess()
            self.assertEqual("invalid", next(s for s in result.sources if s.kind == "user").status)
            self.assertTrue(result.sweep_may_pause)
        with open(os.path.join(self.factory.config, "settings.json"), "w") as handle:
            handle.write("{oops")
        self.assertEqual("unreadable", next(s for s in self.assess().sources if s.kind == "user").status)

    def test_last_cleanup_marker_is_observed_metadata(self) -> None:
        with open(os.path.join(self.factory.config, ".last-cleanup"), "w") as handle:
            handle.write("2026-10-25T10:04:18.309Z")
        result = self.assess()
        self.assertEqual("present", result.last_cleanup_marker_status)
        self.assertEqual(dt.datetime(2026, 10, 25, 10, 4, 18, 309000, tzinfo=dt.timezone.utc),
                         result.last_cleanup_marker)

    def test_risk_buckets(self) -> None:
        retention = self.assess()
        self.assertEqual("past-period", native.transcript_risk(NOW - 31 * DAY, NOW, retention))
        self.assertEqual("approaching", native.transcript_risk(NOW - 25 * DAY, NOW, retention))
        self.assertEqual("within-period", native.transcript_risk(NOW - 22 * DAY, NOW, retention))
        self.assertEqual("past-period", native.worst_risk(["within-period", "past-period"]))


class ReportingTests(IsolatedTestCase):
    def test_protected_groups_at_native_risk_are_reported_without_dates(self) -> None:
        self.factory.user_settings({"cleanupPeriodDays": 30})
        kept = self.factory.session(age=45 * DAY)  # kept by keep-latest, older than 30 days
        self.factory.session(age=60 * DAY)
        report = self.run_json("--keep-latest", "1")
        native_report = report["extensions"]["claude_code"]["native_retention"]
        estimates = native_report["estimates"]
        self.assertEqual(1, estimates["kept_groups_at_risk"])
        self.assertEqual(1, estimates["selected_groups_at_risk"])
        group = next(g for g in report["groups"] if g["group_id"] == kept)
        self.assertEqual("past-period", group["extensions"]["claude_code"]["native_retention_risk"])
        # Observed metadata is reported; no projected deletion timestamp appears anywhere.
        self.assertIn("transcript_modified_at", group["sessions"][0]["extensions"]["claude_code"])
        flat = repr(native_report) + repr(group["extensions"])
        self.assertNotRegex(flat, r"delet\w*_(at|on|after)")
        code, text, _ = self.run_cli("--keep-latest", "1")
        self.assertIn("1 group this policy keeps is past or near that period; Claude Code may delete it anyway.", text)
        self.assertIn("cannot predict exact deletion times", text)
        future_dates = [d for d in re.findall(r"\d{4}-\d{2}-\d{2}", text) if d > "2026-10-25"]
        self.assertEqual([], future_dates)

    def test_project_settings_from_known_projects_appear_in_report(self) -> None:
        other = self.factory.project("other")
        self.factory.session(other, age=40 * DAY)
        self.factory.write_json(os.path.join(other, ".claude", "settings.json"), {"cleanupPeriodDays": 7})
        report = self.run_json("--older-than", "1d", "--project", "all")
        configured = report["extensions"]["claude_code"]["native_retention"]["configured"]
        self.assertEqual(7, configured["lowest_applicable_days"])
        self.assertIn({"kind": "project-shared", "path": os.path.join(other, ".claude", "settings.json"),
                       "status": "set", "value_days": 7}, configured["sources"])
        self.assertFalse(configured["complete"])
