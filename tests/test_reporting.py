"""Infrastructure errors and error paths never report success.

This is the invariant that matters most when a CI control plane is under
strain: the failure mode of a degraded selector is not a red build, it is a
green build that ran the wrong tests. These tests assert, at the level of both
the library and the command line, that a fault yields a non-zero status and an
explicit error -- never an empty success.

Run with: python3 tests/run_all.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO, "examples")
FIXTURES = os.path.join(EXAMPLES, "fixtures")
sys.path.insert(0, EXAMPLES)

from cisim import Degraded, InfraError, Rng, load_json, percentile  # noqa: E402

GRAPH = os.path.join(FIXTURES, "dependency_graph.json")
HISTORY_FRESH = os.path.join(FIXTURES, "history_fresh.json")
HISTORY_STALE = os.path.join(FIXTURES, "history_stale.json")
HISTORY_MALFORMED = os.path.join(FIXTURES, "history_malformed.json")
POOLS = os.path.join(FIXTURES, "pools.json")


def run_script(name: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join(EXAMPLES, name), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestDegradedContract(unittest.TestCase):
    def test_degraded_requires_a_reason(self) -> None:
        with self.assertRaises(InfraError):
            Degraded(reasons=[])

    def test_degraded_rejects_blank_reasons(self) -> None:
        with self.assertRaises(InfraError):
            Degraded(reasons=["   "])

    def test_degraded_reports_itself(self) -> None:
        degraded = Degraded(reasons=["listener lag 20m"])
        self.assertTrue(degraded.is_degraded)
        self.assertIn("listener lag 20m", degraded.as_dict()["reasons"])


class TestInfraHelpers(unittest.TestCase):
    def test_missing_fixture_is_an_error_not_empty(self) -> None:
        with self.assertRaises(InfraError):
            load_json(os.path.join(FIXTURES, "definitely-not-here.json"))

    def test_malformed_fixture_is_an_error_not_empty(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not valid json")
            path = handle.name
        try:
            with self.assertRaises(InfraError):
                load_json(path)
        finally:
            os.unlink(path)

    def test_empty_series_has_no_percentile(self) -> None:
        self.assertEqual(percentile([], 0.95), 0.0)

    def test_poisson_zero_mean_returns_zero(self) -> None:
        """A zero rate means zero events, and must never yield a negative count.

        Knuth's method returns ``count - 1``; at mean zero the loop body never
        runs, so the naive implementation returns ``-1``. A negative arrival
        count would silently corrupt any accounting built on it.
        """
        for seed in range(20):
            self.assertEqual(
                Rng(seed).poisson(0),
                0,
                "poisson(0) must be exactly 0",
            )
            self.assertEqual(Rng(seed).poisson(0.0), 0)

    def test_poisson_is_never_negative(self) -> None:
        for seed in range(30):
            for mean in (0.0, 0.5, 1.0, 5.0, 25.0):
                self.assertGreaterEqual(
                    Rng(seed).poisson(mean),
                    0,
                    f"poisson({mean}) produced a negative count",
                )

    def test_poisson_rejects_negative_mean(self) -> None:
        with self.assertRaises(InfraError):
            Rng(1).poisson(-1.0)

    def test_poisson_mean_roughly_tracks_its_rate(self) -> None:
        """A large sample must average near the requested rate.

        Guards against the exponential being computed in a way that silently
        destroys the distribution's scale.
        """
        rng = Rng(1234)
        draws = [rng.poisson(12.0) for _ in range(4000)]
        mean = sum(draws) / len(draws)
        self.assertGreater(mean, 10.0)
        self.assertLess(mean, 14.0)


    def test_rng_is_deterministic_across_instances(self) -> None:
        first = [Rng(11).random() for _ in range(1)]
        second = [Rng(11).random() for _ in range(1)]
        self.assertEqual(first, second)

    def test_rng_sequence_is_reproducible(self) -> None:
        a = Rng(42)
        b = Rng(42)
        self.assertEqual([a.random() for _ in range(20)], [b.random() for _ in range(20)])

    def test_rng_rejects_non_integer_seed(self) -> None:
        with self.assertRaises(InfraError):
            Rng(1.5)  # type: ignore[arg-type]

    def test_rng_rejects_out_of_range_probability(self) -> None:
        with self.assertRaises(InfraError):
            Rng(1).chance(1.5)


class TestSelectorCliReporting(unittest.TestCase):
    def test_malformed_history_exits_non_zero(self) -> None:
        proc = run_script(
            "selector_demo.py",
            ["--changed", "api/handlers.py", "--history", HISTORY_MALFORMED],
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "a corrupt history file must not produce a zero exit status",
        )
        self.assertIn("FAILED", proc.stdout)
        self.assertNotIn(
            "verdict: OK",
            proc.stdout,
            "an errored run must not print an OK verdict",
        )

    def test_malformed_history_json_has_no_green_selection(self) -> None:
        proc = run_script(
            "selector_demo.py",
            ["--changed", "api/handlers.py", "--history", HISTORY_MALFORMED, "--json"],
        )
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIsNone(
            payload["selection"],
            "an errored run must report no selection, not an empty one",
        )

    def test_no_changed_files_exits_non_zero(self) -> None:
        proc = run_script("selector_demo.py", [])
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_graph_exits_non_zero(self) -> None:
        proc = run_script(
            "selector_demo.py",
            ["--changed", "api/handlers.py", "--graph", "/nonexistent/graph.json"],
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FAILED", proc.stdout)

    def test_fresh_selection_exits_zero_and_is_not_empty(self) -> None:
        proc = run_script(
            "selector_demo.py",
            ["--changed", "api/handlers.py", "--history", HISTORY_FRESH, "--json"],
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertGreater(
            len(payload["selection"]["suites"]),
            0,
            "a successful run must select at least one suite",
        )

    def test_freshness_override_widens_selection(self) -> None:
        normal = run_script(
            "selector_demo.py",
            ["--changed", "api/handlers.py", "--history", HISTORY_FRESH, "--json"],
        )
        strict = run_script(
            "selector_demo.py",
            ["--changed", "api/handlers.py", "--history", HISTORY_FRESH,
             "--freshness", "0", "--json"],
        )
        self.assertEqual(strict.returncode, 0)
        baseline = json.loads(normal.stdout)
        payload = json.loads(strict.stdout)
        self.assertEqual(payload["freshness_window"], 0)
        self.assertLess(len(baseline["selection"]["suites"]), len(payload["selection"]["suites"]))
        self.assertTrue(payload["selection"]["full_suite_forced"])

    def test_stale_history_exits_zero_but_flags_degradation(self) -> None:
        proc = run_script(
            "selector_demo.py",
            [
                "--changed",
                "packages/db/schema.sql",
                "--history",
                HISTORY_STALE,
                "--json",
            ],
        )
        self.assertEqual(
            proc.returncode,
            0,
            "stale history is a conservative full run, not an infrastructure fault",
        )
        payload = json.loads(proc.stdout)
        self.assertTrue(
            payload["degraded"],
            "stale history must be reported as degraded so it is visible",
        )
        self.assertTrue(payload["selection"]["stale_suites"])

    def test_unmapped_file_selects_everything_via_cli(self) -> None:
        proc = run_script(
            "selector_demo.py",
            ["--changed", "nowhere/at/all.rs", "--history", HISTORY_FRESH, "--json"],
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["selection"]["full_suite_forced"])
        self.assertEqual(
            len(payload["selection"]["suites"]),
            payload["known_suites"],
            "an unmapped file must select every known suite",
        )


class TestLifetimeCliReporting(unittest.TestCase):
    def test_unsafe_admission_is_refused_non_zero(self) -> None:
        proc = run_script(
            "label_lifetime.py",
            ["--scenario", "unsafe-admission", "--pools", POOLS, "--pool", "self-ephemeral-arm"],
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stdout)

    def test_unsafe_admission_refusal_names_the_risk(self) -> None:
        proc = run_script(
            "label_lifetime.py",
            ["--scenario", "unsafe-admission", "--pools", POOLS, "--pool", "self-ephemeral-arm"],
        )
        self.assertIn(
            "fork",
            proc.stdout.lower(),
            "the refusal must explain the actual risk, not just fail",
        )

    def test_fixture_untrusted_fork_policy_cannot_report_healthy(self) -> None:
        spec = {
            "pools": [{"id": "unsafe", "labels": ["linux"],
                       "kind": "self-hosted-ephemeral", "capacity": 1,
                       "admission": 1, "admission_mode": "untrusted-fork"}]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pools.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(spec, handle)
            proc = run_script(
                "label_lifetime.py", ["--scenario", "steady", "--pools", path, "--json"]
            )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("untrusted fork", payload["error"])

    def test_default_unsafe_scenario_refuses_self_hosted_not_hosted(self) -> None:
        proc = run_script(
            "label_lifetime.py", ["--scenario", "unsafe-admission", "--json"]
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["refused"])
        self.assertIn("self-hosted", payload["error"])

    def test_fault_injection_exits_non_zero(self) -> None:
        proc = run_script(
            "label_lifetime.py",
            ["--scenario", "fault-injection", "--pools", POOLS, "--pool", "hosted-arm"],
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "a provisioning failure must not be reported as a clean run",
        )
        self.assertIn("FAILED", proc.stdout)

    def test_saturated_pool_is_reported_but_exits_zero(self) -> None:
        """Saturation is a capacity finding, so it must not fail the run.

        The bounded wait is what makes saturation *observable*: with an
        unbounded wait the pool simply queues and eventually serves everything,
        which is correct but says nothing about whether it is large enough.
        """
        proc = run_script(
            "label_lifetime.py",
            [
                "--scenario",
                "steady",
                "--pools",
                POOLS,
                "--pool",
                "self-ephemeral-arm",
                "--max-queue-wait",
                "1",
            ],
        )
        self.assertEqual(
            proc.returncode,
            0,
            "saturation is a capacity finding, not a fault; it must not be "
            "conflated with an infrastructure error",
        )
        self.assertIn("SATURATED", proc.stdout)

    def test_bounded_wait_saturation_matches_json(self) -> None:
        proc = run_script(
            "label_lifetime.py",
            [
                "--scenario",
                "steady",
                "--pools",
                POOLS,
                "--pool",
                "self-ephemeral-arm",
                "--max-queue-wait",
                "1",
                "--json",
            ],
        )
        payload = json.loads(proc.stdout)
        result = payload["results"][0]
        self.assertTrue(result["saturated"])
        self.assertGreater(result["unplaced"], 0)
        self.assertTrue(
            result["healthy"],
            "the lifecycle invariants still hold; only capacity fell short",
        )
        self.assertEqual(result["leaked"], 0)

    def test_unknown_pool_exits_non_zero(self) -> None:
        proc = run_script(
            "label_lifetime.py",
            ["--scenario", "steady", "--pools", POOLS, "--pool", "does-not-exist"],
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_pool_fixture_exits_non_zero(self) -> None:
        proc = run_script(
            "label_lifetime.py",
            ["--scenario", "steady", "--pools", "/nonexistent/pools.json"],
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_no_leaked_runners_across_all_pools(self) -> None:
        proc = run_script(
            "label_lifetime.py", ["--scenario", "steady", "--pools", POOLS, "--json"]
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        for result in payload["results"]:
            self.assertEqual(
                result["leaked"],
                0,
                f"pool {result['pool']} leaked {result['leaked']} runner(s)",
            )


class TestQueueCliReporting(unittest.TestCase):
    def test_all_scenarios_run_clean(self) -> None:
        proc = run_script("queue_vs_run.py", ["--compare", "--json"])
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        for result in payload["results"]:
            self.assertEqual(
                result["errors"],
                [],
                f"scenario {result['scenario']} reported a consistency violation",
            )

    def test_run_never_exceeds_queued(self) -> None:
        proc = run_script("queue_vs_run.py", ["--compare", "--json"])
        payload = json.loads(proc.stdout)
        for result in payload["results"]:
            self.assertLessEqual(
                result["run_total"],
                result["queued_total"],
                f"scenario {result['scenario']} completed more work than it "
                "accepted, which cannot be true",
            )

    def test_redesign_scenario_absorbs_growth_the_backlog_scenario_cannot(self) -> None:
        """The redesigned (higher-capacity) scenario must end nearly drained.

        Its arrival process is identical to the ``backlog`` scenario, so the
        comparison isolates capacity: same traffic, different service ceiling.
        The assertion is a *dramatic* improvement rather than an exact zero,
        because arrivals continue to the final tick and a small residual is
        the honest outcome.
        """
        redesign = json.loads(
            run_script("queue_vs_run.py", ["--scenario", "redesign", "--json"]).stdout
        )["results"][0]
        backlog = json.loads(
            run_script("queue_vs_run.py", ["--scenario", "backlog", "--json"]).stdout
        )["results"][0]

        self.assertLess(
            redesign["backlog_final"],
            backlog["backlog_final"] / 4,
            "the higher-capacity scenario must end with far less backlog than "
            "the fixed-capacity one under the same arrival growth",
        )
        self.assertNotEqual(
            redesign["backlog_trend"],
            "growing",
            "the redesigned scenario must not still be diverging at the end",
        )


    def test_determinism_across_processes(self) -> None:
        first = run_script("queue_vs_run.py", ["--compare", "--seed", "99", "--json"])
        second = run_script("queue_vs_run.py", ["--compare", "--seed", "99", "--json"])
        self.assertEqual(first.stdout, second.stdout)

    def test_different_seeds_give_different_traffic(self) -> None:
        first = run_script("queue_vs_run.py", ["--scenario", "backlog", "--seed", "1", "--json"])
        second = run_script("queue_vs_run.py", ["--scenario", "backlog", "--seed", "2", "--json"])
        self.assertNotEqual(
            first.stdout,
            second.stdout,
            "the seed must actually drive the arrival process, or the "
            "determinism guarantee is vacuous",
        )


if __name__ == "__main__":
    unittest.main()
