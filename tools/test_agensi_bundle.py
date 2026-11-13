#!/usr/bin/env python3
"""Run the whole synthetic test suite against the generated Agensi script.

Builds the package, extracts the ZIP into a temporary directory, loads the
extracted standalone script, and makes ``claude_session_cleanup`` and its
modules resolve to it before the tests are imported. Every test then runs
against the script exactly as it would be uploaded, including the tests
that patch module functions.

    python3 tools/test_agensi_bundle.py [-v]

``tests/test_agensi_package.py`` is left out: it compares the script with
the canonical package, which this run replaces.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
LEFT_OUT = "test_agensi_package."


def without(suite: unittest.TestSuite, prefix: str) -> unittest.TestSuite:
    kept = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            kept.addTest(without(test, prefix))
        elif not test.id().startswith(prefix):
            kept.addTest(test)
    return kept


def main(argv: list[str]) -> int:
    if "claude_session_cleanup" in sys.modules:
        raise SystemExit("the canonical package is already imported")
    sys.path.insert(0, TESTS)
    import agensi_support

    with tempfile.TemporaryDirectory(prefix="csc-agensi-") as directory:
        archive, package = agensi_support.build_and_extract(directory)
        script = agensi_support.load_script(package)
        _, renames = agensi_support.builder().generate()
        agensi_support.install_views(script, renames)

        from claude_session_cleanup import cli  # noqa: E402 - resolved to the script

        if cli.run is not script.run or not script.__file__.startswith(directory):
            raise SystemExit("the tests would not run against the extracted script")
        for label, path in (("archive", archive), ("script", script.__file__)):
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            print(f"{label}: {os.path.getsize(path)} bytes, sha256 {digest}", file=sys.stderr)
        print("Testing the extracted package script", file=sys.stderr)
        loader = unittest.TestLoader()
        suite = without(loader.discover(TESTS, top_level_dir=TESTS), LEFT_OUT)
        verbosity = 2 if "-v" in argv else 1
        result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
