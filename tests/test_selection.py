"""Selection correctness and staleness invariants.

Each test here is written to fail on a plausible *bug*, not to observe that the
code runs. The mutations that break them are the ones a real implementation
actually makes:

* the empty-selection bug, where an unmapped file yields "nothing to run" and
  the pipeline goes green because no test executed;
* the stale-data bug, where an old green result is treated as current;
* the fail-open bug, where a corrupt history file is read as "no failures";
* a prefix-matching bug where a broad rule shadows a specific one.

Run with: python3 tests/run_all.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"))

from cisim import InfraError  # noqa: E402
from cisim.selector import Selector, parse_history_rows  # noqa: E402

FRESH = {
    "api/handlers": {"age": 1, "status": "pass"},
    "api/middleware": {"age": 1, "status": "pass"},
    "api/other": {"age": 1, "status": "pass"},
    "db/migrations": {"age": 1, "status": "pass"},
    "db/queries": {"age": 1, "status": "pass"},
    "web/components": {"age": 1, "status": "pass"},
}


def make_selector(freshness_window: int = 5) -> Selector:
    return Selector(
        graph={
            "api/": ["api/handlers", "api/middleware"],
            "api/handlers.py": ["api/handlers"],
            "api/other/": ["api/other"],
            "db/schema.sql": ["db/migrations"],
            "db/": ["db/queries"],
            "web/": ["web/components"],
        },
        all_suites=[
            "api/handlers",
            "api/middleware",
            "api/other",
            "db/migrations",
            "db/queries",
            "web/components",
        ],
        freshness_window=freshness_window,
    )


class TestNeverSelectsNothing(unittest.TestCase):
    """The silent-skip invariant: a non-empty change set never selects nothing."""

    def test_unmapped_file_selects_everything(self) -> None:
        selector = make_selector()
        decision = selector.select(["totally/unknown/path.rs"], FRESH)
        self.assertTrue(decision.ok)
        assert decision.selection is not None
        self.assertFalse(
            decision.selection.is_empty,
            "an unmapped changed file must not produce an empty selection: that "
            "is the silent-skip bug where the build goes green having run nothing",
        )
        self.assertEqual(
            decision.selection.suite_set(),
            set(selector.all_suites),
            "an unmapped file must fall back to the complete known suite set",
        )
        self.assertTrue(decision.selection.full_suite_forced)
        self.assertEqual(decision.selection.unmapped_files, ("totally/unknown/path.rs",))

    def test_unmapped_file_is_reported_as_degraded(self) -> None:
        selector = make_selector()
        decision = selector.select(["unknown/file.rs"], FRESH)
        self.assertTrue(
            decision.degraded,
            "falling back to the full suite is a conservative choice that must be "
            "visible to the operator, not silent",
        )

    def test_mapped_file_selects_a_strict_subset(self) -> None:
        selector = make_selector()
        decision = selector.select(["api/handlers.py"], FRESH)
        assert decision.selection is not None
        self.assertEqual(decision.selection.suite_set(), {"api/handlers"})
        self.assertFalse(
            decision.selection.full_suite_forced,
            "a fully mapped, fresh change must not trigger the full-suite fallback",
        )

    def test_empty_change_set_is_rejected(self) -> None:
        selector = make_selector()
        with self.assertRaises(InfraError):
            selector.select([], FRESH)


class TestStalenessFailsClosed(unittest.TestCase):
    """Rule 2: an old result is not evidence about the current tree."""

    def test_stale_history_widens_the_selection_observably(self) -> None:
        """Stale history must change *what runs*, not merely how it is labelled.

        The defect this defends against: selecting the mapped suites first and
        only then relabelling them leaves the selected set identical to the
        fresh case, so the staleness policy changes nothing a caller can
        observe. The comparison below is between two real selections.
        """
        selector = make_selector(freshness_window=5)
        fresh_decision = selector.select(["api/handlers.py"], FRESH)
        stale_history = dict(FRESH)
        stale_history["api/handlers"] = {"age": 99, "status": "pass"}
        stale_decision = selector.select(["api/handlers.py"], stale_history)

        assert fresh_decision.selection is not None
        assert stale_decision.selection is not None

        self.assertNotEqual(
            fresh_decision.selection.suite_set(),
            stale_decision.selection.suite_set(),
            "stale history must produce a different selection from fresh history",
        )
        self.assertEqual(
            stale_decision.selection.suite_set(),
            set(selector.all_suites),
            "an impacted suite with stale history must widen the selection to "
            "every known suite; anything narrower trusts the very history that "
            "is out of date",
        )
        self.assertEqual(
            fresh_decision.selection.suite_set(),
            {"api/handlers"},
            "the fresh case must stay narrow, or the widening proves nothing",
        )
        self.assertTrue(
            stale_decision.selection.full_suite_forced,
            "a widened selection must report that the full suite was forced",
        )

    def test_stale_history_still_selects_the_suite(self) -> None:
        selector = make_selector(freshness_window=5)
        history = dict(FRESH)
        history["api/handlers"] = {"age": 99, "status": "pass"}
        decision = selector.select(["api/handlers.py"], history)
        assert decision.selection is not None
        self.assertIn(
            "api/handlers",
            decision.selection.suite_set(),
            "a suite with stale history must still run, not be skipped on the "
            "strength of an old green result",
        )
        self.assertEqual(decision.selection.stale_suites, ("api/handlers",))

    def test_stale_history_is_degraded_not_silent(self) -> None:
        selector = make_selector(freshness_window=5)
        history = dict(FRESH)
        history["api/handlers"] = {"age": 999, "status": "pass"}
        decision = selector.select(["api/handlers.py"], history)
        self.assertTrue(
            decision.degraded,
            "running on stale history must be reported, or nobody will notice "
            "the listener fell behind",
        )

    def test_fresh_history_is_not_degraded(self) -> None:
        selector = make_selector(freshness_window=5)
        decision = selector.select(["api/handlers.py"], FRESH)
        self.assertFalse(
            decision.degraded,
            "a fully fresh selection must not be reported as degraded, or the "
            "signal becomes noise and gets ignored",
        )

    def test_freshness_boundary_is_inclusive_of_the_window(self) -> None:
        selector = make_selector(freshness_window=5)
        at_window = dict(FRESH)
        at_window["api/handlers"] = {"age": 5, "status": "pass"}
        decision = selector.select(["api/handlers.py"], at_window)
        assert decision.selection is not None
        self.assertEqual(
            decision.selection.stale_suites,
            (),
            "history exactly at the freshness window is still usable; the "
            "boundary must not drift",
        )

        over_window = dict(FRESH)
        over_window["api/handlers"] = {"age": 6, "status": "pass"}
        decision = selector.select(["api/handlers.py"], over_window)
        assert decision.selection is not None
        self.assertEqual(decision.selection.stale_suites, ("api/handlers",))

    def test_missing_history_selects_the_suite(self) -> None:
        selector = make_selector()
        history = {k: v for k, v in FRESH.items() if k != "api/handlers"}
        decision = selector.select(["api/handlers.py"], history)
        assert decision.selection is not None
        self.assertIn("api/handlers", decision.selection.suite_set())
        self.assertEqual(
            decision.selection.missing_history_suites, ("api/handlers",)
        )
        self.assertTrue(decision.degraded)

    def test_missing_history_widens_the_selection_observably(self) -> None:
        selector = make_selector()
        history = {k: v for k, v in FRESH.items() if k != "api/handlers"}
        missing_decision = selector.select(["api/handlers.py"], history)
        fresh_decision = selector.select(["api/handlers.py"], FRESH)
        assert missing_decision.selection is not None
        assert fresh_decision.selection is not None
        self.assertNotEqual(
            missing_decision.selection.suite_set(),
            fresh_decision.selection.suite_set(),
            "an impacted suite with no history must widen the selection, not "
            "merely annotate it",
        )
        self.assertEqual(
            missing_decision.selection.suite_set(), set(selector.all_suites)
        )
        self.assertTrue(missing_decision.selection.full_suite_forced)


class TestUnknownIsNotEmpty(unittest.TestCase):
    """Rule 3: a corrupt history is an error, never an empty history."""

    def test_malformed_history_is_an_error_with_no_selection(self) -> None:
        selector = make_selector()
        decision = selector.select(
            ["api/handlers.py"], {"api/handlers": {"age": "recent", "status": "pass"}}
        )
        self.assertFalse(
            decision.ok,
            "a history we cannot parse must not be read as 'no failures recorded'",
        )
        self.assertIsNone(
            decision.selection,
            "an errored selection must be absent, not empty; an empty selection "
            "would be indistinguishable from 'nothing to run'",
        )

    def test_negative_age_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            parse_history_rows({"api/handlers": {"age": -1, "status": "pass"}})

    def test_non_object_entry_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            parse_history_rows({"api/handlers": "pass"})

    def test_none_history_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            parse_history_rows(None)

    def test_bool_age_is_rejected(self) -> None:
        # bool is an int subclass; accepting it would let `true` pass as an age.
        with self.assertRaises(InfraError):
            parse_history_rows({"api/handlers": {"age": True, "status": "pass"}})

    def test_list_payload_is_accepted(self) -> None:
        rows = parse_history_rows([{"suite": "api/handlers", "age": 2, "status": "pass"}])
        self.assertEqual(rows, {"api/handlers": {"age": 2, "status": "pass"}})


class TestPrefixMatching(unittest.TestCase):
    """Longest-prefix resolution: a specific rule must beat a broad one."""

    def test_exact_key_wins_over_prefix(self) -> None:
        selector = Selector(
            graph={"api/": ["api/handlers"], "api/handlers.py": ["db/queries"]},
            all_suites=["api/handlers", "db/queries"],
            freshness_window=5,
        )
        self.assertEqual(selector.suites_for_file("api/handlers.py"), ("db/queries",))

    def test_longest_prefix_wins(self) -> None:
        selector = Selector(
            graph={"api/": ["api/handlers"], "api/other/": ["api/other"]},
            all_suites=["api/handlers", "api/other"],
            freshness_window=5,
        )
        self.assertEqual(selector.suites_for_file("api/other/thing.py"), ("api/other",))

    def test_prefix_rule_does_not_match_a_sibling_directory(self) -> None:
        selector = make_selector()
        # "api/" must not swallow "apifoo/".
        self.assertEqual(selector.suites_for_file("apifoo/thing.py"), ())

    def test_unknown_suite_in_graph_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            Selector(
                graph={"api/": ["nonexistent-suite"]},
                all_suites=["api/handlers"],
                freshness_window=5,
            )


class TestDeterminism(unittest.TestCase):
    """Identical inputs must give identical decisions."""

    def test_repeated_selection_is_identical(self) -> None:
        selector = make_selector()
        first = selector.select(["api/handlers.py", "db/schema.sql"], FRESH)
        second = selector.select(["api/handlers.py", "db/schema.sql"], FRESH)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_change_order_does_not_affect_the_selection(self) -> None:
        selector = make_selector()
        forward = selector.select(["api/handlers.py", "web/thing.tsx"], FRESH)
        reverse = selector.select(["web/thing.tsx", "api/handlers.py"], FRESH)
        assert forward.selection is not None and reverse.selection is not None
        self.assertEqual(forward.selection.suite_set(), reverse.selection.suite_set())


if __name__ == "__main__":
    unittest.main()
