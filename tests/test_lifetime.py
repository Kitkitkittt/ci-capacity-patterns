"""Runner lifecycle and label-routing invariants.

These tests defend properties that are cheap to state and expensive to notice
in production:

* a job is never assigned to a runner that is already busy;
* an ephemeral runner serves exactly one job and is destroyed;
* every runner created reaches a terminal state -- no leaked instances, which
  is the cost-and-security bug that accumulates silently;
* a label requirement the pool cannot satisfy is an error, not "no capacity";
* self-hosted capacity admitted to untrusted fork code is refused outright.

Run with: python3 tests/run_all.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"))

from cisim import InfraError  # noqa: E402
from cisim.lifetime import (  # noqa: E402
    ADMISSION_MODES,
    Job,
    Pool,
    PoolKind,
    Runner,
    RunnerState,
    admission_for,
    run_pool_simulation,
)


def ephemeral_pool(admission: int = 8, warm_pool: int = 0) -> Pool:
    return Pool(
        name="eph",
        labels=("linux", "arm64"),
        kind=PoolKind.SELF_HOSTED_EPHEMERAL,
        capacity=admission,
        admission=admission,
        warm_pool=warm_pool,
    )


def simple_jobs(count: int, duration: int = 1, labels=("linux", "arm64")) -> list[Job]:
    return [
        Job(job_id=f"job-{i:03d}", labels=tuple(labels), duration=duration)
        for i in range(count)
    ]


class TestLabelRouting(unittest.TestCase):
    def test_subset_of_labels_matches(self) -> None:
        runner = Runner(runner_id="r1", pool="p", labels=("linux", "arm64", "gpu"))
        self.assertTrue(runner.can_accept(("linux",)))
        self.assertTrue(runner.can_accept(("arm64", "linux")))

    def test_missing_label_does_not_match(self) -> None:
        runner = Runner(runner_id="r1", pool="p", labels=("linux", "arm64"))
        self.assertFalse(
            runner.can_accept(("gpu",)),
            "a runner must not accept a job whose labels it does not satisfy; "
            "partial credit would place work on a machine that cannot run it",
        )

    def test_busy_runner_is_not_available(self) -> None:
        runner = Runner(runner_id="r1", pool="p", labels=("linux",))
        runner.state = RunnerState.BUSY
        self.assertFalse(runner.can_accept(("linux",)))

    def test_unsatisfiable_label_is_an_error_not_empty_capacity(self) -> None:
        pool = ephemeral_pool()
        jobs = [Job(job_id="j1", labels=("gpu",), duration=1)]
        with self.assertRaises(InfraError):
            run_pool_simulation(pool, jobs, admission_mode="trusted-only")


class TestLifecycleInvariants(unittest.TestCase):
    def test_every_runner_reaches_a_terminal_state(self) -> None:
        pool = ephemeral_pool()
        result = run_pool_simulation(
            pool, simple_jobs(10), admission_mode="trusted-only"
        )
        self.assertEqual(
            result.leaked,
            0,
            "a runner that never terminates is a leaked instance: it costs money "
            "and it is a compromised-runner risk",
        )
        self.assertTrue(result.healthy)

    def test_ephemeral_runner_serves_exactly_one_job(self) -> None:
        pool = ephemeral_pool()
        run_pool_simulation(pool, simple_jobs(10), admission_mode="trusted-only")
        for runner in pool.runners:
            self.assertLessEqual(
                runner.jobs_served,
                1,
                f"ephemeral runner {runner.runner_id} served "
                f"{runner.jobs_served} jobs; one job per runner is the whole "
                "point of an ephemeral pool",
            )

    def test_persistent_pool_is_not_modelled_as_ephemeral(self) -> None:
        persistent = Pool(
            name="persist",
            labels=("linux",),
            kind=PoolKind.SELF_HOSTED_PERSISTENT,
            capacity=4,
            admission=2,
        )
        self.assertFalse(
            persistent.ephemeral,
            "a pool configured as persistent must not be treated as ephemeral; "
            "the distinction is the security property being modelled",
        )

    def test_jobs_served_never_exceeds_jobs_submitted(self) -> None:
        pool = ephemeral_pool()
        jobs = simple_jobs(6)
        result = run_pool_simulation(pool, jobs, admission_mode="trusted-only")
        self.assertLessEqual(
            result.jobs_served,
            len(jobs),
            "more jobs completed than were submitted means the accounting is wrong",
        )

    def test_persistent_pool_reuses_one_runner_for_sequential_jobs(self) -> None:
        """A persistent runner must serve job after job, not be torn down.

        This is the property that makes a persistent pool worth its risk, and
        the one an earlier model got wrong by destroying every runner on
        completion.
        """
        pool = Pool(
            name="persist",
            labels=("linux",),
            kind=PoolKind.SELF_HOSTED_PERSISTENT,
            capacity=1,
            admission=1,
        )
        result = run_pool_simulation(
            pool,
            simple_jobs(3, duration=1, labels=("linux",)),
            admission_mode="trusted-only",
        )
        self.assertGreater(
            result.jobs_served,
            1,
            "a persistent pool of one runner must serve more than one "
            "sequential job",
        )
        self.assertEqual(
            result.created,
            1,
            "a persistent pool must not create a new runner per job; reuse is "
            "the defining behaviour",
        )
        self.assertEqual(
            max(runner.jobs_served for runner in pool.runners),
            result.jobs_served,
        )
        self.assertEqual(result.leaked, 0)

    def test_ephemeral_pool_replaces_runners_after_each_job(self) -> None:
        """Sequential jobs need distinct ephemeral runners, not one recycled.

        With capacity 1, two sequential jobs must be served by two different
        runner ids. If the model reused an ephemeral runner, the one-job-per-
        runner security property would be silently absent.
        """
        pool = ephemeral_pool(admission=1)
        result = run_pool_simulation(
            pool, simple_jobs(2, duration=1), admission_mode="trusted-only"
        )
        served_ids = [r.runner_id for r in pool.runners if r.jobs_served > 0]
        self.assertEqual(
            len(served_ids),
            2,
            "two sequential jobs on a capacity-1 ephemeral pool must be served "
            "by two distinct runners",
        )
        self.assertEqual(
            len(set(served_ids)),
            2,
            "ephemeral runner ids must be unique across replacements",
        )
        for runner in pool.runners:
            self.assertLessEqual(runner.jobs_served, 1)
        self.assertEqual(result.leaked, 0)
        self.assertEqual(
            result.still_live,
            0,
            "an ephemeral pool must end with no runners still alive",
        )

    def test_ephemeral_utilisation_counts_live_runner_time(self) -> None:
        pool = ephemeral_pool(admission=1)
        result = run_pool_simulation(
            pool, simple_jobs(2, duration=1), admission_mode="trusted-only"
        )
        self.assertEqual(result.jobs_served, 2)
        self.assertEqual(result.created, 2)
        self.assertEqual(result.utilisation, 1.0)

    def test_concurrency_never_exceeds_admission(self) -> None:
        """The limit bounds runners alive *at once*, not runners ever created.

        An earlier model compared the lifetime total against the limit, which
        made an ephemeral pool stop creating runners for good once it had
        churned through ``admission`` of them. The assertion here is on peak
        concurrent liveness, which is the property that actually holds.
        """
        pool = ephemeral_pool(admission=8)
        result = run_pool_simulation(
            pool, simple_jobs(20, duration=2), admission_mode="trusted-only"
        )
        self.assertLessEqual(
            result.peak_live,
            8,
            "the pool had more runners alive at once than its admission limit",
        )
        self.assertLessEqual(
            max(runner.jobs_served for runner in pool.runners),
            1,
            "an ephemeral runner may serve at most one job",
        )

    def test_ephemeral_pool_keeps_replacing_beyond_the_admission_limit(self) -> None:
        """Churn must not be capped by the concurrent limit.

        With admission 2 and 6 sequential jobs, a correct model serves all six
        by replacing runners. The broken predecessor stopped after the first
        two because it compared the lifetime total against the limit.
        """
        pool = ephemeral_pool(admission=2)
        result = run_pool_simulation(
            pool, simple_jobs(6, duration=1), admission_mode="trusted-only"
        )
        self.assertEqual(
            result.jobs_served,
            6,
            "all six sequential jobs must be served even though only two "
            "runners may be alive at once",
        )
        self.assertGreater(
            result.created,
            2,
            "the pool must create replacements beyond the concurrent limit",
        )
        self.assertLessEqual(result.peak_live, 2)
        self.assertEqual(result.leaked, 0)

    def test_reconcile_destroys_idle_ephemeral_runners(self) -> None:
        pool = ephemeral_pool()
        pool.provision("t")
        pool.provision("t")
        destroyed = pool.reconcile()
        self.assertEqual(destroyed, 2)
        for runner in pool.runners:
            self.assertTrue(runner.state.is_terminal)

    def test_reconcile_leaves_persistent_runners_alive(self) -> None:
        pool = Pool(
            name="persist",
            labels=("linux",),
            kind=PoolKind.SELF_HOSTED_PERSISTENT,
            capacity=2,
            admission=2,
        )
        pool.provision("p")
        pool.provision("p")
        destroyed = pool.reconcile()
        self.assertEqual(
            destroyed,
            0,
            "reconciling must not tear down persistent runners; they exist to "
            "be reused",
        )

    def test_warm_pool_runners_are_used_before_being_recycled(self) -> None:
        """Pre-warmed capacity must actually serve jobs.

        An earlier model ran reconciliation every tick, destroying the warm
        pool before any job could reach it -- making a warm pool strictly worse
        than no warm pool at all.
        """
        pool = ephemeral_pool(admission=6, warm_pool=2)
        result = run_pool_simulation(
            pool, simple_jobs(2, duration=1), admission_mode="trusted-only"
        )
        warm = [r for r in pool.runners if r.runner_id.startswith("warm")]
        self.assertEqual(len(warm), 2, "both warm runners must have been created")
        self.assertEqual(
            sum(r.jobs_served for r in warm),
            2,
            "the pre-warmed runners must serve the jobs; if they were destroyed "
            "first, the warm pool bought nothing",
        )
        self.assertEqual(result.unplaced, 0)
        self.assertEqual(result.leaked, 0)

    def test_live_runners_bound_admission_not_lifetime_total(self) -> None:
        pool = ephemeral_pool(admission=1)
        run_pool_simulation(
            pool, simple_jobs(4, duration=1), admission_mode="trusted-only"
        )
        self.assertEqual(
            pool.live_count,
            0,
            "after all work drains, no ephemeral runner should still be live",
        )
        self.assertEqual(
            pool.total_ever_created,
            len(pool.runners),
        )


class TestSaturationIsAFinding(unittest.TestCase):
    """Congestion must be reported, and must not be confused with a fault."""

    def test_bounded_wait_reports_unplaced_jobs(self) -> None:
        """When the wait budget is finite, jobs that never get placed are counted.

        Note the two distinct behaviours this pins down. With an unbounded
        wait, a capacity-1 pool still serves every job eventually -- that is
        queueing, and dropping those jobs would be the bug. With a bounded
        wait, the jobs that cannot be reached in time are reported as unplaced
        rather than silently discarded.
        """
        pool = ephemeral_pool(admission=1)
        result = run_pool_simulation(
            pool,
            simple_jobs(12, duration=3),
            admission_mode="trusted-only",
            max_queue_wait=1,
        )
        self.assertGreater(
            result.unplaced,
            0,
            "with a bounded wait, an undersized pool must report unplaced jobs",
        )
        self.assertTrue(
            result.saturated,
            "saturation must be recorded as a capacity finding",
        )

    def test_unbounded_wait_serves_every_job(self) -> None:
        """Queuing is not failure: a slow pool still serves all its work."""
        pool = ephemeral_pool(admission=1)
        result = run_pool_simulation(
            pool, simple_jobs(12, duration=3), admission_mode="trusted-only"
        )
        self.assertEqual(
            result.unplaced,
            0,
            "with an unbounded wait, every job must eventually be served; "
            "discarding queued work is the bug this guards against",
        )
        self.assertEqual(result.jobs_served, 12)
        self.assertEqual(result.peak_live, 1)
        self.assertEqual(result.leaked, 0)

    def test_saturated_run_is_still_healthy(self) -> None:
        pool = ephemeral_pool(admission=1)
        result = run_pool_simulation(
            pool,
            simple_jobs(12, duration=3),
            admission_mode="trusted-only",
            max_queue_wait=1,
        )
        self.assertTrue(
            result.healthy,
            "an undersized but well-behaved pool has not violated a lifecycle "
            "invariant; conflating that with a fault trains operators to "
            "ignore the signal",
        )
        self.assertFalse(result.ok)

    def test_adequately_sized_pool_places_everything(self) -> None:
        pool = ephemeral_pool(admission=12)
        result = run_pool_simulation(
            pool, simple_jobs(12, duration=1), admission_mode="trusted-only"
        )
        self.assertEqual(result.unplaced, 0)
        self.assertTrue(result.ok)


class TestFaultsSurface(unittest.TestCase):
    def test_provisioning_failure_is_recorded_as_an_error(self) -> None:
        pool = ephemeral_pool(admission=10)
        result = run_pool_simulation(
            pool,
            simple_jobs(10),
            admission_mode="trusted-only",
            faulty_runner_every=3,
        )
        self.assertTrue(
            result.errors,
            "a provisioning failure must appear as an error, never as a silent "
            "success",
        )
        self.assertFalse(result.healthy)
        self.assertGreater(result.failed, 0)

    def test_failed_runners_are_still_accounted_for(self) -> None:
        pool = ephemeral_pool(admission=10)
        result = run_pool_simulation(
            pool,
            simple_jobs(10),
            admission_mode="trusted-only",
            faulty_runner_every=3,
        )
        self.assertEqual(
            result.leaked,
            0,
            "failed runners must be terminal, not leaked; an unaccounted-for "
            "instance is invisible cost",
        )


class TestAdmissionSafety(unittest.TestCase):
    """The rule that keeps a capacity project from becoming an incident."""

    def test_self_hosted_pool_refuses_untrusted_fork_code(self) -> None:
        with self.assertRaises(InfraError) as ctx:
            admission_for(PoolKind.SELF_HOSTED_EPHEMERAL, "untrusted-fork")
        self.assertIn(
            "untrusted fork",
            str(ctx.exception),
            "the refusal message must name the actual risk so the operator can "
            "act on it",
        )

    def test_container_pool_refuses_untrusted_fork_code(self) -> None:
        with self.assertRaises(InfraError):
            admission_for(PoolKind.CONTAINER, "untrusted-fork")

    def test_machine_orchestrated_pool_refuses_untrusted_fork_code(self) -> None:
        with self.assertRaises(InfraError):
            admission_for(PoolKind.MACHINE_ORCHESTRATED, "untrusted-fork")

    def test_persistent_pool_refuses_untrusted_fork_code(self) -> None:
        with self.assertRaises(InfraError):
            admission_for(PoolKind.SELF_HOSTED_PERSISTENT, "untrusted-fork")

    def test_hosted_pool_accepts_hosted_only_admission(self) -> None:
        note = admission_for(PoolKind.HOSTED, "hosted-only")
        self.assertTrue(note)

    def test_hosted_pool_rejects_self_hosted_admission_mode(self) -> None:
        with self.assertRaises(InfraError):
            admission_for(PoolKind.HOSTED, "trusted-only")

    def test_unknown_admission_mode_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            admission_for(PoolKind.HOSTED, "whatever-i-want")

    def test_simulation_enforces_the_admission_rule(self) -> None:
        pool = ephemeral_pool()
        with self.assertRaises(InfraError):
            run_pool_simulation(
                pool, simple_jobs(2), admission_mode="untrusted-fork"
            )


class TestPoolValidation(unittest.TestCase):
    def test_admission_above_capacity_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            Pool(
                name="bad",
                labels=("linux",),
                kind=PoolKind.HOSTED,
                capacity=2,
                admission=5,
            )

    def test_warm_pool_above_capacity_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            Pool(
                name="bad",
                labels=("linux",),
                kind=PoolKind.HOSTED,
                capacity=2,
                admission=2,
                warm_pool=9,
            )

    def test_pool_without_labels_is_rejected(self) -> None:
        with self.assertRaises(InfraError):
            Pool(
                name="bad",
                labels=(),
                kind=PoolKind.HOSTED,
                capacity=2,
                admission=2,
            )

    def test_warm_pool_is_provisioned_and_accounted(self) -> None:
        pool = ephemeral_pool(admission=6, warm_pool=2)
        result = run_pool_simulation(
            pool, simple_jobs(4, duration=1), admission_mode="trusted-only"
        )
        self.assertGreaterEqual(result.created, 2)
        self.assertEqual(result.leaked, 0)


if __name__ == "__main__":
    unittest.main()
