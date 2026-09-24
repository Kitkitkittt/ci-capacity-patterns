"""Ephemeral runner lifetime, label routing, and pool reconciliation.

This module models the part of runner capacity that a CPU count cannot see:
**routing**. A job is matched to a runner by labels, and if no runner with a
matching label is idle, the job waits. In a real fleet that wait does not
present as an error -- it presents as a job that sits in a queue, which is why
the model here tracks both allocation and every runner's terminal state.

The invariants this module enforces, and which ``tests/test_lifetime.py``
breaks deliberately to prove they are checked:

* a job is never assigned to a runner that is already busy;
* an ephemeral runner serves exactly one job and is then destroyed;
* every runner that is created reaches a terminal state (no leaked VMs);
* label matching is exact over the requested label set, and an unsatisfiable
  label is an infrastructure error rather than an empty success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from . import InfraError

__all__ = [
    "ADMISSION_MODES",
    "Job",
    "Pool",
    "PoolKind",
    "Runner",
    "RunnerState",
    "SimulationResult",
    "admission_for",
    "run_pool_simulation",
]


class RunnerState(str, Enum):
    """Lifecycle states of a runner.

    ``TERMINAL`` states are the ones a runner may legitimately end in. The
    allocation loop never leaves a runner in a non-terminal state when it
    finishes, which is what makes the "no leaked runners" invariant checkable.
    """

    PENDING = "pending"
    READY = "ready"
    BUSY = "busy"
    DESTROYED = "destroyed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (RunnerState.DESTROYED, RunnerState.FAILED)


@dataclass
class Runner:
    """One runner instance in a pool."""

    runner_id: str
    pool: str
    labels: tuple[str, ...]
    state: RunnerState = RunnerState.READY
    jobs_served: int = 0
    ephemeral: bool = True

    def can_accept(self, required: Sequence[str]) -> bool:
        """True when this runner is idle and offers every required label.

        Matching is exact and set-wise: a runner offering ``{arm64, linux}``
        satisfies a job requiring ``{arm64}``, but a runner offering ``{arm64}``
        does not satisfy a job requiring ``{arm64, gpu}``. Partial credit is
        not a thing in label routing, and pretending otherwise is how a job
        lands on a machine that cannot run it.
        """
        if self.state is not RunnerState.READY:
            return False
        return set(required).issubset(set(self.labels))


@dataclass(frozen=True)
class Job:
    """A unit of work with a label requirement and a duration."""

    job_id: str
    labels: tuple[str, ...]
    duration: int


class PoolKind(str, Enum):
    """How a pool is admitted and described. Mirrors the README's table."""

    HOSTED = "hosted"
    SELF_HOSTED_PERSISTENT = "self-hosted-persistent"
    SELF_HOSTED_EPHEMERAL = "self-hosted-ephemeral"
    CONTAINER = "container"
    MACHINE_ORCHESTRATED = "machine-orchestrated"


@dataclass
class Pool:
    """A label-scoped pool of runners with an admission policy."""

    name: str
    labels: tuple[str, ...]
    kind: PoolKind
    capacity: int
    admission: int
    warm_pool: int = 0
    runners: list[Runner] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise InfraError(f"pool {self.name!r} needs positive capacity")
        if self.admission <= 0:
            raise InfraError(f"pool {self.name!r} needs a positive admission limit")
        if self.admission > self.capacity:
            raise InfraError(
                f"pool {self.name!r}: admission {self.admission} exceeds capacity "
                f"{self.capacity}; it could never be filled"
            )
        if self.warm_pool > self.capacity:
            raise InfraError(
                f"pool {self.name!r}: warm_pool {self.warm_pool} exceeds capacity "
                f"{self.capacity}"
            )
        if not self.labels:
            raise InfraError(f"pool {self.name!r} needs at least one label")

    @property
    def ephemeral(self) -> bool:
        """Whether runners in this pool serve exactly one job.

        Derived from ``kind`` rather than stored, so a caller cannot configure
        a persistent pool that the simulation then treats as ephemeral. The
        distinction matters: GitHub recommends against autoscaling persistent
        runners precisely because they do not give this guarantee.
        """
        return self.kind is not PoolKind.SELF_HOSTED_PERSISTENT

    @property
    def live_runners(self) -> list[Runner]:
        """Runners that exist right now and have not reached a terminal state.

        This -- not the total number ever created -- is what the capacity and
        admission limits bound. An earlier model compared the *lifetime* total
        against the limit, which made concurrency a function of how long the
        run had been going rather than of how many runners were up. That is
        wrong in the direction that matters: it under-counts capacity for
        ephemeral pools, where a runner is replaced after every job.
        """
        return [r for r in self.runners if not r.state.is_terminal]

    @property
    def live_count(self) -> int:
        return len(self.live_runners)

    @property
    def is_full(self) -> bool:
        """True when no further runner may be started concurrently."""
        return self.live_count >= self.admission

    @property
    def total_ever_created(self) -> int:
        return len(self.runners)

    def provision(self, prefix: str = "r") -> Runner:
        """Create one runner in this pool, respecting the *concurrent* limit.

        The runner id is derived from the lifetime total, so ids stay unique
        across replacements: two sequential ephemeral runners are distinguishable
        even though only one is live at a time.
        """
        if self.is_full:
            raise InfraError(f"pool {self.name!r} is at admission limit {self.admission}")
        runner = Runner(
            runner_id=f"{prefix}-{self.total_ever_created + 1:03d}",
            pool=self.name,
            labels=tuple(self.labels),
            ephemeral=self.ephemeral,
        )
        self.runners.append(runner)
        return runner

    def idle_runners(self, required: Sequence[str]) -> list[Runner]:
        return [r for r in self.live_runners if r.can_accept(required)]

    def retire(self, runner: Runner, state: RunnerState = RunnerState.DESTROYED) -> None:
        """Move a runner to a terminal state.

        Terminal runners stop counting against the concurrent limit, which is
        what allows an ephemeral pool to replace them. A persistent pool is
        never retired by the normal job path -- its runners go back to READY
        and are reused.
        """
        if state is not RunnerState.DESTROYED and state is not RunnerState.FAILED:
            raise InfraError(f"retire requires a terminal state, got {state.value}")
        runner.state = state

    def reconcile(self) -> int:
        """Retire idle ephemeral runners. Returns how many were retired.

        Call this **once, after all work has drained** -- not every tick. The
        controller this models (ARC, CircleCI's container runner, Machine
        Runner Orchestrator) scales the fleet to match demand, and "demand" is
        only meaningful once the queue is empty. Running it every tick destroys
        the pre-warmed pool before any job can reach it, which makes a warm
        pool strictly worse than none.

        Persistent runners are deliberately left alone: they are meant to be
        reused, and tearing them down between jobs is the anti-pattern this
        model exists to show the cost of.
        """
        retired = 0
        for runner in self.live_runners:
            if runner.state is RunnerState.READY and runner.ephemeral:
                runner.state = RunnerState.DESTROYED
                retired += 1
        return retired


#: Admission modes describe *who* is allowed onto a pool, which is the axis
#: that matters for the public-repository question in the README. It is
#: modelled as data so that the safety rule is visible in configuration rather
#: than buried in prose.
ADMISSION_MODES: Mapping[str, str] = {
    "trusted-only": "Only code already trusted (protected branch push, tag, or approval).",
    "untrusted-fork": "Fork pull-request code may reach this pool. Unsafe for self-hosted.",
    "hosted-only": "Provider-hosted compute; no runner in your network or credentials.",
}


def admission_for(kind: PoolKind, mode: str) -> str:
    """Validate that a pool's admission mode is acceptable for its kind.

    Returns a human-readable note, or raises :class:`InfraError` when the
    combination is one the sources explicitly warn against -- self-hosted
    capacity reachable from untrusted fork code. Failing here rather than
    warning is the point: this is the configuration that turns a CI cost
    project into a remote-code-execution incident.
    """
    if mode not in ADMISSION_MODES:
        raise InfraError(
            f"unknown admission mode {mode!r}; expected one of {sorted(ADMISSION_MODES)}"
        )
    if kind is PoolKind.HOSTED and mode != "hosted-only":
        raise InfraError(
            f"hosted pool cannot use admission mode {mode!r}; use 'hosted-only'"
        )
    if kind is not PoolKind.HOSTED and mode == "untrusted-fork":
        raise InfraError(
            f"refusing to model {kind.value} capacity admitted to untrusted fork code: "
            "a fork pull request can execute arbitrary code on the runner (see README §5)"
        )
    if mode == "hosted-only" and kind is not PoolKind.HOSTED:
        raise InfraError(
            f"admission mode 'hosted-only' does not apply to {kind.value} capacity"
        )
    return ADMISSION_MODES[mode]


@dataclass
class SimulationResult:
    """Outcome of one pool simulation, with a full accounting of runners."""

    pool_name: str
    created: int
    destroyed: int
    failed: int
    unplaced: int
    jobs_served: int
    wait_ticks: list[int]
    utilisation: float
    still_live: int = 0
    peak_live: int = 0
    errors: tuple[str, ...] = ()

    @property
    def leaked(self) -> int:
        """Runners neither terminated nor accounted for as still live.

        Must be zero. A persistent pool legitimately ends the run with runners
        still alive and ready for the next job -- that is the entire point of a
        persistent pool -- so those are counted in ``still_live`` rather than
        treated as leaks. For an ephemeral pool ``still_live`` is zero, because
        every runner should have been torn down.
        """
        return self.created - self.destroyed - self.failed - self.still_live

    @property
    def healthy(self) -> bool:
        """The lifecycle invariants held, regardless of how busy the pool was."""
        return not self.errors and self.leaked == 0

    @property
    def saturated(self) -> bool:
        """Some jobs could not be placed before the wait budget ran out.

        This is a capacity finding, not a fault. A pool that cannot place
        every job is telling you something true and useful about its size;
        conflating that with an infrastructure error would train operators to
        ignore the signal.
        """
        return self.unplaced > 0

    @property
    def ok(self) -> bool:
        """True only when the run was both healthy and fully placed."""
        return self.healthy and not self.saturated

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool_name,
            "created": self.created,
            "destroyed": self.destroyed,
            "failed": self.failed,
            "peak_live": self.peak_live,
            "still_live": self.still_live,
            "leaked": self.leaked,
            "unplaced": self.unplaced,
            "jobs_served": self.jobs_served,
            "utilisation": round(self.utilisation, 4),
            "max_wait": max(self.wait_ticks) if self.wait_ticks else 0,
            "mean_wait": (
                round(sum(self.wait_ticks) / len(self.wait_ticks), 4)
                if self.wait_ticks
                else 0.0
            ),
            "ok": self.ok,
            "healthy": self.healthy,
            "saturated": self.saturated,
            "errors": list(self.errors),
        }


def run_pool_simulation(
    pool: Pool,
    jobs: Sequence[Job],
    *,
    admission_mode: str,
    max_queue_wait: int = 10_000,
    faulty_runner_every: int = 0,
) -> SimulationResult:
    """Allocate ``jobs`` onto ``pool`` and account for every runner created.

    ``max_queue_wait`` bounds how long a job may sit queued behind busy
    runners before it is counted as unplaced. It is a bound on the *wait*, not
    a placement policy: queuing is ordinary congestion and is never itself a
    failure. A job that exhausts the budget is reported in ``unplaced`` rather
    than silently dropped, so an undersized pool shows up as a number instead
    of as quietly missing work.

    ``faulty_runner_every`` injects a provisioning failure on every Nth runner
    creation, so the error path is exercised by tests rather than assumed to
    work. A failed runner is retired immediately and its job is left unplaced;
    it is never silently retried onto a busy runner.

    Raises :class:`InfraError` if a job requires a label the pool cannot
    provide. That is a configuration fault, and returning "no runners
    available" for it would be indistinguishable from ordinary saturation.
    """
    admission_for(pool.kind, admission_mode)

    wait_ticks: list[int] = []
    busy_ticks = 0
    live_runner_ticks = 0
    unplaced = 0
    jobs_served = 0
    failures = 0
    created = 0
    errors: list[str] = []
    provision_attempts = 0
    peak_live = 0

    pending: list[tuple[Job, int]] = [(job, 0) for job in jobs]
    active: list[tuple[Runner, Job, int]] = []

    # Warm pool is provisioned up front: the latency-for-money trade that
    # Machine Runner Orchestrator's minReplicas expresses.
    while len(pool.runners) < pool.warm_pool:
        pool.provision("warm" if pool.ephemeral else "prs")
        created += 1

    tick = 0
    peak_live = max(peak_live, pool.live_count)
    while pending or active:
        tick += 1
        # Count only runners that exist during this tick; ephemeral replacements
        # do not retroactively consume time before they were provisioned.
        live_runner_ticks += pool.live_count
        if tick > 10_000:
            raise InfraError("simulation did not converge within 10000 ticks")

        # Complete finished jobs. This is where ephemeral and persistent pools
        # diverge, and getting it wrong is what makes persistent reuse
        # impossible: an ephemeral runner is retired and stops counting against
        # concurrency, while a persistent runner returns to READY and serves
        # the next job.
        still_active: list[tuple[Runner, Job, int]] = []
        for runner, job, remaining in active:
            busy_ticks += 1
            remaining -= 1
            if remaining <= 0:
                jobs_served += 1
                if runner.ephemeral:
                    pool.retire(runner, RunnerState.DESTROYED)
                else:
                    runner.state = RunnerState.READY
            else:
                still_active.append((runner, job, remaining))
        active = still_active

        # Admit pending jobs, in arrival order.
        remaining_pending: list[tuple[Job, int]] = []
        for job, waited in pending:
            if not set(job.labels).issubset(set(pool.labels)):
                raise InfraError(
                    f"job {job.job_id!r} requires labels {sorted(job.labels)} but pool "
                    f"{pool.name!r} offers only {sorted(pool.labels)}"
                )
            idle = pool.idle_runners(job.labels)
            if not idle:
                # Only start a new runner when there is genuine concurrent
                # headroom. ``is_full`` counts *live* runners, so a retired
                # ephemeral runner frees its slot for a replacement.
                if not pool.is_full:
                    provision_attempts += 1
                    inject_fault = (
                        faulty_runner_every > 0
                        and provision_attempts % faulty_runner_every == 0
                    )
                    if inject_fault:
                        runner = pool.provision("flt")
                        pool.retire(runner, RunnerState.FAILED)
                        failures += 1
                        created += 1
                        errors.append(
                            f"provisioning failure on runner {runner.runner_id} "
                            f"while admitting job {job.job_id}"
                        )
                        unplaced += 1
                        continue
                    runner = pool.provision("prs" if not pool.ephemeral else "eph")
                    created += 1
                    idle = [runner]
            if not idle:
                # No idle runner and no concurrent headroom: the job is queued.
                # Queuing is ordinary congestion, not a placement failure, so
                # the job is retried next tick until its wait budget is spent.
                # A predecessor discarded jobs after a fixed wait, silently
                # dropping work that capacity would eventually have served.
                if waited >= max_queue_wait:
                    unplaced += 1
                else:
                    remaining_pending.append((job, waited + 1))
                continue
            runner = idle[0]
            runner.state = RunnerState.BUSY
            runner.jobs_served += 1
            active.append((runner, job, job.duration))
            wait_ticks.append(waited)

        pending = remaining_pending
        peak_live = max(peak_live, pool.live_count)

    # Final reconciliation of anything still idle. This runs once, after all
    # work has drained -- not every tick. Running it every tick would destroy
    # the pre-warmed pool before it could ever be used, which is precisely the
    # bug that made a warm pool worse than useless.
    pool.reconcile()

    destroyed = sum(1 for r in pool.runners if r.state is RunnerState.DESTROYED)
    # A persistent pool legitimately ends with runners still up and ready for
    # the next job. Those are accounted separately from leaks.
    still_live = sum(1 for r in pool.runners if r.state is RunnerState.READY)
    utilisation = busy_ticks / live_runner_ticks if live_runner_ticks else 0.0

    return SimulationResult(
        pool_name=pool.name,
        created=created,
        destroyed=destroyed,
        failed=failures,
        unplaced=unplaced,
        jobs_served=jobs_served,
        wait_ticks=wait_ticks,
        utilisation=utilisation,
        still_live=still_live,
        peak_live=peak_live,
        errors=tuple(errors),
    )
