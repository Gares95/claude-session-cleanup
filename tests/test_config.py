import os

from claude_session_cleanup import config
from claude_session_cleanup.errors import CleanupError
from support import IsolatedTestCase, requires_symlinks


class SanitizeTests(IsolatedTestCase):
    def test_matches_claude_code_encoding(self) -> None:
        self.assertEqual("-home-user-my-repo", config.sanitize_path("/home/user/my.repo"))
        self.assertEqual("C--Users-me-repo", config.sanitize_path("C:\\Users\\me\\repo"))
        self.assertEqual("-tmp-caf--x", config.sanitize_path("/tmp/café x"))

    def test_lossy_encoding_is_detected_as_a_possible_match_only(self) -> None:
        # Both paths encode to the same storage name; identity needs more evidence.
        self.assertTrue(config.project_dir_matches("-home-a-b", "/home/a-b"))
        self.assertTrue(config.project_dir_matches("-home-a-b", "/home/a/b"))

    def test_short_paths_require_exact_name(self) -> None:
        self.assertFalse(config.project_dir_matches("-home-a-b-c", "/home/a/b"))
        self.assertFalse(config.project_dir_matches("-home-a", "/home/a/b"))

    def test_long_paths_match_prefix_and_alphanumeric_hash_only(self) -> None:
        path = "/" + "x" * 250
        prefix = config.sanitize_path(path)
        self.assertEqual(200, len(prefix))
        self.assertTrue(config.project_dir_matches(prefix + "-abc123", path))
        self.assertFalse(config.project_dir_matches(prefix + "-abc-123", path))
        self.assertFalse(config.project_dir_matches(prefix, path))
        self.assertFalse(config.project_dir_matches(prefix + "-", path))


class PathTests(IsolatedTestCase):
    def test_recorded_paths_are_normalized_lexically(self) -> None:
        root = os.path.abspath(os.sep)
        a_b = os.path.join(root, "a", "b")
        self.assertEqual(a_b, config.normalize_recorded_path(os.path.join(root, "a", ".", "b") + os.sep))
        self.assertIsNone(config.normalize_recorded_path("relative/path"))
        self.assertIsNone(config.normalize_recorded_path(""))
        self.assertIsNone(config.normalize_recorded_path(a_b + "\x00"))
        self.assertIsNone(config.normalize_recorded_path(42))
        # NFD input is normalized to NFC.
        self.assertEqual(os.path.join(root, "caf\u00e9"),
                         config.normalize_recorded_path(os.path.join(root, "cafe\u0301")))

    @requires_symlinks
    def test_invoking_directory_is_resolved_through_symlinks(self) -> None:
        real = self.factory.project("real")
        link = os.path.join(self.factory.workspace, "link")
        os.symlink(real, link)
        self.assertEqual(real, config.canonical_project_path(link))

    def test_missing_or_relative_project_is_refused(self) -> None:
        with self.assertRaises(CleanupError):
            config.canonical_project_path("relative")
        with self.assertRaises(CleanupError):
            config.canonical_project_path(os.path.join(self.tmp, "missing"))

    def test_session_ids_must_be_canonical_lowercase_uuids(self) -> None:
        self.assertTrue(config.is_session_id("2e2f0637-fb76-4dca-b519-b76ff1779830"))
        self.assertFalse(config.is_session_id("2E2F0637-FB76-4DCA-B519-B76FF1779830"))
        self.assertFalse(config.is_session_id("2e2f0637fb764dcab519b76ff1779830"))
        self.assertFalse(config.is_session_id("../2e2f0637-fb76-4dca-b519-b76ff1779830"))


class ConfigDirTests(IsolatedTestCase):
    def test_resolution_order(self) -> None:
        explicit = config.resolve_config_dir({"CLAUDE_CONFIG_DIR": "/nonexistent"}, self.factory.config)
        self.assertEqual(("--config-dir", self.factory.config), (explicit.source, explicit.path))
        from_env = config.resolve_config_dir({"CLAUDE_CONFIG_DIR": self.factory.config})
        self.assertEqual("CLAUDE_CONFIG_DIR", from_env.source)
        home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(home, ".claude"))
        default = config.resolve_config_dir({}, home=home)
        self.assertEqual(("default", os.path.join(home, ".claude")), (default.source, default.path))

    def test_missing_config_dir_is_a_safety_stop(self) -> None:
        with self.assertRaisesRegex(CleanupError, "no Claude Code configuration directory"):
            config.resolve_config_dir({"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "nope")})
