#!/usr/bin/env python3
"""Single entry point for this repository's tests.

Uses unittest discovery so no third-party test runner is required:

    python3 tests/run_all.py

Exits 0 only when every test passes. Any failure, error, or unexpected skip
produces a non-zero status, because a check that cannot fail is not a check.
"""

from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")
EXAMPLES = os.path.join(REPO, "examples")


def main() -> int:
    # The example modules import their shared library from ``examples/``; make
    # that importable the same way the CLIs do, so the tests exercise the real
    # code paths rather than a copy.
    sys.path.insert(0, EXAMPLES)
    sys.path.insert(0, REPO)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=TESTS, pattern="test_*.py", top_level_dir=REPO)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.skipped:
        # This repository has no legitimately skipped tests. A skip here means
        # something silently stopped being checked.
        print(
            f"\n{len(result.skipped)} test(s) skipped; this repository expects none "
            "to skip, so a skip is treated as a failure.",
            file=sys.stderr,
        )
        return 1

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
