"""The generated Agensi package: layout, metadata, safety gates, and behavior.

Every behavioral test runs the standalone script extracted from the built
ZIP. ``tools/test_agensi_bundle.py`` additionally runs the whole suite
against that script.
"""

import ast
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import agensi_support
import support
import test_deletion
from claude_session_cleanup import __version__, cli
from support import NOW_ARG, IsolatedTestCase, allow_processes, fake_process, requires_symlinks, snapshot_tree

ROOT = agensi_support.ROOT
DAY = dt.timedelta(days=1)
SOURCE_URL = "https://github.com/Gares95/claude-session-cleanup"
CANONICAL_SCRIPT = os.path.join(ROOT, "scripts", "cleanup_sessions.py")

_state: dict = {}


def setUpModule() -> None:
    directory = tempfile.mkdtemp(prefix="csc-agensi-test-")
    _state["dir"] = directory
    _state["zip"], _state["package"] = agensi_support.build_and_extract(directory)
    _state["script"] = agensi_support.load_script(_state["package"])
    _state["script_path"] = _state["script"].__file__


def tearDownModule() -> None:
    sys.modules.pop("agensi_cleanup_sessions", None)
    shutil.rmtree(_state.pop("dir"), ignore_errors=True)


def tool():
    return agensi_support.builder()


def frontmatter(text: str) -> dict:
    """Parse the SKILL.md frontmatter subset used here: scalars and block lists of scalars."""
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "no frontmatter"
    fields: dict = {}
    key = None
    for line in match.group(1).split("\n"):
        item = re.fullmatch(r"  - (.+)", line)
        if item and key is not None and isinstance(fields[key], list):
            fields[key].append(item.group(1))
            continue
        pair = re.fullmatch(r"([a-z_]+):(?: (.*))?", line)
        assert pair, f"unsupported frontmatter line: {line!r}"
        key, value = pair.group(1), pair.group(2)
        assert key not in fields, f"duplicate key {key}"
        assert value is None or not value.startswith(("[", "{", "'", '"', "|", ">", "&", "*", "!")), line
        fields[key] = [] if value is None else value
    return fields


class PackageLayoutTests(unittest.TestCase):
    def test_build_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            again = tool().build(directory)
            with open(again, "rb") as first, open(_state["zip"], "rb") as second:
                self.assertEqual(first.read(), second.read())

    def test_archive_holds_exactly_the_reviewed_files(self) -> None:
        with zipfile.ZipFile(_state["zip"]) as archive:
            infos = archive.infolist()
            self.assertIsNone(archive.testzip())
        names = [info.filename for info in infos]
        self.assertEqual(sorted(names), names)
        self.assertEqual(
            [f"claude-session-cleanup/{name}" for name in
             ("LICENSE", "SKILL.md", "references/safety.md", "scripts/cleanup_sessions.py")],
            names,
        )
        self.assertEqual(f"claude-session-cleanup-{__version__}.zip", os.path.basename(_state["zip"]))
        for info in infos:
            with self.subTest(name=info.filename):
                self.assertEqual((1980, 1, 1, 0, 0, 0), info.date_time)
                self.assertEqual(0o100644, info.external_attr >> 16)  # regular file, rw-r--r--, no symlink
                self.assertEqual(zipfile.ZIP_DEFLATED, info.compress_type)
                self.assertEqual(b"", info.extra)
                self.assertFalse(info.is_dir())

    def test_static_files_are_copies_of_their_sources(self) -> None:
        for packaged, source in (("LICENSE", "LICENSE"), ("SKILL.md", "packaging/agensi/SKILL.md"),
                                 ("references/safety.md", "docs/safety-model.md")):
            with self.subTest(file=packaged):
                with open(os.path.join(_state["package"], packaged), "rb") as a, \
                        open(os.path.join(ROOT, source), "rb") as b:
                    self.assertEqual(b.read().replace(b"\r\n", b"\n"), a.read())

    def test_skill_frontmatter(self) -> None:
        with open(os.path.join(_state["package"], "SKILL.md"), encoding="utf-8") as handle:
            text = handle.read()
        fields = frontmatter(text)
        self.assertEqual("claude-session-cleanup", fields["name"])
        self.assertRegex(fields["name"], r"\A[a-z0-9]+(-[a-z0-9]+)*\Z")
        self.assertEqual(__version__, fields["version"])
        self.assertLessEqual(len(fields["description"]), 1024)
        self.assertLessEqual(len(fields["summary"]), 300)
        self.assertLessEqual(len(fields["compatibility"]), 500)
        self.assertEqual("MIT", fields["license"])
        self.assertEqual(SOURCE_URL, fields["docs_url"])
        for key in ("tags", "keywords", "use_cases"):
            self.assertIsInstance(fields[key], list)
            self.assertTrue(fields[key], key)
        self.assertIn("Linux-only", fields["compatibility"])
        self.assertIn("Python 3.10+", fields["compatibility"])

    def test_skill_body_states_the_safety_rules(self) -> None:
        with open(os.path.join(_state["package"], "SKILL.md"), encoding="utf-8") as handle:
            text = handle.read()
        for needle in (SOURCE_URL, "MIT License", "python3 -I", "--save-plan", "--apply-plan",
                       "There is no `--apply`", "Linux-only", "macOS and Windows", "references/safety.md",
                       "Safety stop:", "claude purge"):
            self.assertIn(needle, text)
        # Every flag the skill tells an agent to use exists in the script.
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", text))
        known = {o for a in cli.argument_parser()._actions for o in a.option_strings}
        self.assertEqual(set(), flags - known - {"--apply"})


class GeneratedScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        with open(_state["script_path"], encoding="utf-8") as handle:
            self.text = handle.read()

    def test_script_is_generated_from_the_current_canonical_source(self) -> None:
        self.assertEqual(tool().generate()[0], self.text)
        for name in tool().MODULES:
            with open(os.path.join(ROOT, "claude_session_cleanup", f"{name}.py"), "rb") as handle:
                digest = hashlib.sha256(handle.read().replace(b"\r\n", b"\n")).hexdigest()
            self.assertIn(f"# claude_session_cleanup/{name}.py  (sha256 {digest})", self.text)
        self.assertIn(f"claude-session-cleanup {__version__}", self.text.splitlines()[1])

    def test_every_canonical_definition_is_present(self) -> None:
        renames = tool().generate()[1]
        script = _state["script"]
        for name in tool().MODULES:
            path = os.path.join(ROOT, "claude_session_cleanup", f"{name}.py")
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    with self.subTest(definition=f"{name}.{node.name}"):
                        self.assertTrue(hasattr(script, renames.get((name, node.name), node.name)))

    def test_script_is_reviewable_and_self_contained(self) -> None:
        tree = ast.parse(self.text)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(0, node.level)
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"exec", "eval", "compile", "__import__"})
        self.assertLessEqual(imported, set(tool().ALLOWED_IMPORTS))
        for module in ("socket", "ssl", "http", "urllib", "subprocess", "base64", "zlib", "pickle", "marshal",
                       "ctypes", "importlib", "shutil", "tempfile", "zipfile", "multiprocessing"):
            self.assertNotIn(module, imported)
        self.assertNotRegex(self.text, r"os\.(system|popen|exec|spawn|fork|chmod|chown)")
        self.assertNotIn("shell=True", self.text)
        self.assertNotRegex(self.text, r"[A-Za-z0-9+/=]{120,}")
        self.assertTrue(self.text.isascii() or self.text.encode("utf-8").decode("utf-8") == self.text)
        self.assertNotIn("\r", self.text)
        self.assertTrue(self.text.startswith("#!/usr/bin/env python3\n"))

    def test_package_text_has_no_private_or_foreign_references(self) -> None:
        for directory, _, files in os.walk(_state["package"]):
            for name in files:
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                with self.subTest(file=os.path.relpath(path, _state["package"])):
                    urls = set(re.findall(r"https?://[^\s)>\"'`]+", text))
                    self.assertLessEqual({u.rstrip(".,") for u in urls}, {SOURCE_URL})
                    self.assertNotRegex(text, r"/home/[a-z]|/Users/[A-Za-z]|C:\\\\Users")
                    self.assertNotRegex(text.lower(), r"checkout|payment|paypal|stripe|unlock code")
                    self.assertNotRegex(text.lower(), r"ignore (all )?previous instructions")
                    self.assertNotRegex(text, r"(?i)(api[_-]?key|secret[_-]?key|token)\s*[:=]\s*['\"][^'\"]{8,}")

    def test_builder_refuses_unsafe_constructs(self) -> None:
        for snippet in ("import socket\n", "from urllib import request\n", "import subprocess\n",
                        "eval('1')\n", "exec('x = 1')\n", "import os\nos.system('true')\n",
                        "import os\nos.chmod('x', 0o777)\n", "f(shell=True)\n",
                        "x = '" + "QUJD" * 60 + "'\n"):
            with self.subTest(snippet=snippet):
                with self.assertRaises(tool().BuildError):
                    tool().check_script(snippet)


class _PackagedCli:
    """Run the support helpers' CLI through the extracted package script."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(support, "cli", _state["script"])
        patch.start()
        self.addCleanup(patch.stop)


# The deletion suites again, through the packaged script. Tests that patch
# the canonical deletion module are covered by tools/test_agensi_bundle.py.
_PATCHES_CANONICAL = ("test_locked_plan_is_refused", "test_swap_after_preflight_stops_before_touching_the_swapped_file",
                      "test_plan_from_another_context_is_refused")


def _packaged(base):
    attributes = {name: unittest.skip("patches the canonical module; see tools/test_agensi_bundle.py")(
        getattr(base, name)) for name in _PATCHES_CANONICAL if hasattr(base, name)}
    return type(f"Packaged{base.__name__}", (_PackagedCli, base), attributes)


PackagedPreviewTests = _packaged(test_deletion.PreviewTests)
PackagedSavedPlanTests = _packaged(test_deletion.SavedPlanTests)
PackagedApplyTests = _packaged(test_deletion.ApplyTests)
PackagedStalePlanTests = _packaged(test_deletion.StalePlanTests)
PackagedUnsafeDataTests = _packaged(test_deletion.UnsafeDataTests)
PackagedProcessScanTests = _packaged(test_deletion.ProcessScanTests)
PackagedPolicyMatrixTests = _packaged(test_deletion.PolicyMatrixTests)


class CanonicalConformanceTests(test_deletion.DeletionCase):
    """The canonical package and the packaged script agree on the same synthetic data."""

    def opts(self) -> dict:
        # POSIX registry checks off Linux probe liveness with kill(pid, 0); answer "alive".
        return {**super().opts(), "kill": lambda pid, signal: None}

    def both(self, *args, platform: str = "linux", **kwargs):
        results = []
        for implementation in (cli, _state["script"]):
            with mock.patch.object(support, "cli", implementation):
                self.platform = platform
                results.append(self.cli(*args, **kwargs))
        return results

    def populate(self) -> dict:
        f = self.factory
        ids = {}
        child = f.session(age=35 * DAY, subagents=(("c", 35 * DAY),))
        ids["old_with_child"] = self.full_session(40 * DAY, continued_in=(child,))
        ids["old"] = f.session(age=20 * DAY, tool_results=(("t.txt", 20 * DAY),))
        ids["recent"] = f.session(age=2 * DAY)
        ids["active"] = f.session(age=dt.timedelta(minutes=10))
        ids["malformed"] = f.session(age=50 * DAY, raw=b"not json\n")
        ids["running"] = f.session(age=30 * DAY)
        f.registry(4100, ids["running"], "55")
        fake_process(self.proc, 4100, "55", argv=("claude",), cwd=self.tmp)
        ids["background"] = f.session(age=45 * DAY)
        f.job("abcd1234", ids["background"])
        ids["unknown"] = f.session(age=60 * DAY)
        self.write(os.path.join(f.config, "todos", f"{ids['unknown']}-agent.json"), "[]", 60 * DAY)
        other = f.project("other")
        ids["other_old"] = f.session(other, age=90 * DAY)
        ids["other_new"] = f.session(other, age=1 * DAY)
        codex = test_deletion.session_uuid(0xC0DE)
        record = {"type": "session_meta", "payload": {"id": codex, "cwd": f.default_project}}
        f.session(session_id=codex, raw=(json.dumps(record) + "\n").encode(), age=40 * DAY)
        return ids

    POLICIES = (
        ("--older-than", "7d"),
        ("--keep-latest", "2"),
        ("--older-than", "14d", "--keep-latest", "1"),
        ("--older-than", "1d", "--project", "all"),
        ("--keep-latest", "1", "--project", "all", "--action", "delete"),
        ("--older-than", "7d", "--action", "delete", "--verbose"),
        ("--older-than", "7d", "--verbose", "--full-ids"),
    )

    def test_previews_agree_on_every_platform(self) -> None:
        self.populate()
        before = snapshot_tree(self.tmp)
        for platform in ("linux", "darwin", "win32"):
            for policy in self.POLICIES:
                for output in ((), ("--json",), ("--json", "--include-titles")):
                    with self.subTest(platform=platform, args=policy + output):
                        canonical, packaged = self.both(*policy, *output, platform=platform)
                        self.assertEqual(canonical, packaged)
                        self.assertEqual(0, canonical[0], canonical[2])
        self.assertEqual(before, snapshot_tree(self.tmp))

    def test_reported_decisions_and_capabilities_agree(self) -> None:
        self.populate()
        for platform, deletable in (("linux", True), ("darwin", False), ("win32", False)):
            with self.subTest(platform=platform):
                canonical, packaged = self.both("--older-than", "7d", "--action", "delete", "--json",
                                                platform=platform)
                first, second = json.loads(canonical[1]), json.loads(packaged[1])
                self.assertEqual(first, second)
                self.assertEqual(deletable, second["capabilities"]["actions"]["delete"]["supported"])
                for action in ("archive", "quarantine"):
                    self.assertFalse(second["capabilities"]["actions"][action]["supported"])
                statuses = {g["group_id"]: (g["status"], tuple(g["reasons"]), tuple(g.get("mutation_blockers", ())))
                            for g in second["groups"]}
                self.assertEqual(statuses, {g["group_id"]: (g["status"], tuple(g["reasons"]),
                                                            tuple(g.get("mutation_blockers", ())))
                                            for g in first["groups"]})

    def test_saved_plans_agree(self) -> None:
        self.populate()
        plans = []
        for implementation in (cli, _state["script"]):
            with mock.patch.object(support, "cli", implementation):
                plans.append(self.save("--older-than", "7d"))
        first, second = (self.load(path) for path in plans)
        for document in (first, second):
            self.assertEqual(test_deletion.deletion._digest(document), document.pop("digest"))
        # Only the random plan id differs (and therefore the digest).
        self.assertNotEqual(first.pop("plan_id"), second.pop("plan_id"))
        self.assertEqual(first, second)
        self.assertTrue(first["groups"])

    def test_rejected_requests_agree(self) -> None:
        self.populate()
        for args in (("--older-than", "7d", "--action", "delete", "--apply"),
                     ("--older-than", "7d", "--action", "archive"),
                     ("--older-than", "7d", "--action", "quarantine"),
                     ("--json",),
                     ("--older-than", "7d", "--save-plan", "x.json")):
            with self.subTest(args=args):
                for implementation in (cli, _state["script"]):
                    with mock.patch.object(support, "cli", implementation), \
                            mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
                        self.cli(*args)
                    self.assertEqual(2, raised.exception.code)
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform):
                canonical, packaged = self.both("--older-than", "7d", "--action", "delete", "--save-plan",
                                                os.path.join(self.plans, f"{platform}.json"), platform=platform)
                self.assertEqual(canonical, packaged)
                self.assertEqual(1, packaged[0])
                self.assertIn("Safety stop: deletion is not supported here", packaged[2])


class AgensiRunTests(IsolatedTestCase):
    """The script run the way Agensi runs it: alone, from a temporary file, with python3 -I."""

    def setUp(self) -> None:
        super().setUp()
        lone = os.path.join(self.tmp, "agent-temp")
        os.makedirs(lone)
        self.script = os.path.join(lone, "tmp3f9a2c.py")
        shutil.copyfile(_state["script_path"], self.script)
        self.plans = os.path.join(self.tmp, "plans")
        os.makedirs(self.plans)

    def run_script(self, *args, script: str | None = None, env: dict | None = None, now: bool = True):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        environment.update({"HOME": os.environ["HOME"], "CLAUDE_CONFIG_DIR": self.factory.config,
                            "PYTHONDONTWRITEBYTECODE": "1"})
        if sys.platform == "win32":
            environment["USERPROFILE"] = os.environ["HOME"]
        environment.update(env or {})
        command = [sys.executable, "-I", script or self.script, *(("--now", NOW_ARG) if now else ()), *args]
        with allow_processes():
            completed = subprocess.run(command, cwd=self.factory.default_project, env=environment,
                                       capture_output=True, text=True, timeout=120)
        return completed.returncode, completed.stdout, completed.stderr

    def test_version_and_help(self) -> None:
        code, out, _ = self.run_script("--version", now=False)
        self.assertEqual((0, f"claude-session-cleanup {__version__}\n"), (code, out))
        code, out, _ = self.run_script("--help", now=False)
        self.assertEqual(0, code)
        for flag in ("--older-than", "--keep-latest", "--project", "--save-plan", "--apply-plan"):
            self.assertIn(flag, out)

    def test_previews_match_the_canonical_script_and_write_nothing(self) -> None:
        f = self.factory
        for days in (1, 3, 10, 40, 80):
            f.session(age=days * DAY, subagents=(("x", days * DAY),))
        f.session(age=50 * DAY, raw=b"{broken\n")
        f.session(f.project("other"), age=60 * DAY)
        before = snapshot_tree(self.tmp)
        for args in (("--older-than", "7d"), ("--keep-latest", "2"), ("--older-than", "7d", "--keep-latest", "3"),
                     ("--older-than", "1d", "--project", "all", "--json"),
                     ("--older-than", "7d", "--action", "delete", "--verbose")):
            with self.subTest(args=args):
                packaged = self.run_script(*args)
                canonical = self.run_script(*args, script=CANONICAL_SCRIPT)
                self.assertEqual(canonical, packaged)
                self.assertEqual(0, packaged[0], packaged[2])
                if "--json" not in args:
                    self.assertIn("No changes made.", packaged[1])
        self.assertEqual(before, snapshot_tree(self.tmp))

    def test_unsupported_requests_are_rejected(self) -> None:
        self.factory.session(age=40 * DAY)
        for args, message in ((("--older-than", "7d", "--action", "delete", "--apply"), "reviewed saved plan"),
                              (("--older-than", "7d", "--action", "archive"), "archive is not offered"),
                              (("--older-than", "7d", "--action", "quarantine"), "quarantine is not offered"),
                              ((), "at least one of --older-than or --keep-latest")):
            with self.subTest(args=args):
                code, _, err = self.run_script(*args)
                self.assertEqual(2, code)
                self.assertIn(message, err)

    def test_missing_claude_code_data_stops_safely(self) -> None:
        code, out, err = self.run_script("--older-than", "7d",
                                         env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "absent")})
        self.assertEqual(1, code, out + err)
        self.assertIn("Safety stop: no Claude Code configuration directory", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "absent")))

    @unittest.skipIf(sys.platform.startswith("linux"), "deletion is supported on Linux")
    def test_deletion_is_unavailable_off_linux(self) -> None:
        self.factory.session(age=40 * DAY)
        code, out, err = self.run_script("--older-than", "7d", "--action", "delete", "--json")
        self.assertEqual(0, code, err)
        delete = json.loads(out)["capabilities"]["actions"]["delete"]
        self.assertEqual((False, "blocked-by-safety-requirements"), (delete["supported"], delete["unsupported_reason"]))
        plan = os.path.join(self.plans, "plan.json")
        code, _, err = self.run_script("--older-than", "7d", "--action", "delete", "--save-plan", plan)
        self.assertEqual(1, code)
        self.assertIn("Safety stop: deletion is not supported here", err)
        self.assertFalse(os.path.exists(plan))

    @unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/proc/self"), "Linux only")
    def test_linux_save_review_apply_and_replay(self) -> None:
        f = self.factory
        old = f.session(age=40 * DAY, subagents=(("a", 40 * DAY),), file_history=40 * DAY, session_env=40 * DAY)
        keep = f.session(age=2 * DAY)
        other = f.session(f.project("other"), age=90 * DAY)
        memory = os.path.join(f.storage_dir(), "memory", "MEMORY.md")
        os.makedirs(os.path.dirname(memory))
        with open(memory, "w") as handle:
            handle.write("notes")
        plan = os.path.join(self.plans, "delete-plan.json")
        code, out, err = self.run_script("--older-than", "7d", "--action", "delete", "--save-plan", plan)
        self.assertEqual(0, code, out + err)
        self.assertIn("Saved deletion plan", out)
        self.assertEqual(0o600, os.stat(plan).st_mode & 0o777)
        with open(plan, encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual([old], [g["group_id"] for g in document["groups"]])

        code, out, err = self.run_script("--apply-plan", plan)
        self.assertEqual(0, code, out + err)
        self.assertIn("Deleted", out)
        self.assertFalse(os.path.lexists(f.transcript_path(old)))
        self.assertFalse(os.path.lexists(os.path.join(f.storage_dir(), old)))
        self.assertFalse(os.path.lexists(os.path.join(f.config, "file-history", old)))
        self.assertFalse(os.path.lexists(os.path.join(f.config, "session-env", old)))
        self.assertTrue(os.path.exists(f.transcript_path(keep)))
        self.assertTrue(os.path.exists(f.transcript_path(other, f.project("other"))))
        self.assertTrue(os.path.exists(memory))

        code, _, err = self.run_script("--apply-plan", plan)
        self.assertEqual(2, code)
        self.assertIn("already used", err)

    @unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/proc/self"), "Linux only")
    def test_linux_stale_plan_deletes_nothing(self) -> None:
        f = self.factory
        old = f.session(age=40 * DAY)
        plan = os.path.join(self.plans, "delete-plan.json")
        self.assertEqual(0, self.run_script("--older-than", "7d", "--action", "delete", "--save-plan", plan)[0])
        with open(f.transcript_path(old), "a") as handle:
            handle.write("{}\n")
        before = snapshot_tree(f.config)
        code, _, err = self.run_script("--apply-plan", plan)
        self.assertEqual(3, code, err)
        self.assertIn("Nothing was deleted", err)
        self.assertEqual(before, snapshot_tree(f.config))

    @requires_symlinks
    @unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/proc/self"), "Linux only")
    def test_linux_substituted_directory_deletes_nothing(self) -> None:
        f = self.factory
        old = f.session(age=40 * DAY, subagents=(("a", 40 * DAY),))
        plan = os.path.join(self.plans, "delete-plan.json")
        self.assertEqual(0, self.run_script("--older-than", "7d", "--action", "delete", "--save-plan", plan)[0])
        session_dir = os.path.join(f.storage_dir(), old)
        outside = os.path.join(self.tmp, "outside")
        os.rename(session_dir, outside)
        os.symlink(outside, session_dir)
        code, _, err = self.run_script("--apply-plan", plan)
        self.assertEqual(3, code, err)
        self.assertTrue(os.path.isfile(os.path.join(outside, "subagents", "agent-a.jsonl")))
        self.assertTrue(os.path.exists(f.transcript_path(old)))


if __name__ == "__main__":
    unittest.main()
