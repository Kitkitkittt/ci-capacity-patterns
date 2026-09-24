"""Queue depth versus work actually performed.

The distinction this module exists to make: **queued is not run**.

A CI control plane that has accepted a job has not executed it. While a
listener falls behind, the queue grows, and the decision layer consuming that
listener's output is reasoning about the past. The failure is quiet -- dashboards
showing "jobs accepted" keep rising while the signal that matters, backlog
cleared per unit time, degrades.

The model here is deliberately simple: an arrival process, a service rate, and
a backlog. Four scenarios cover the shapes that matter:

``baseline``
    Arrival is flat and capacity is sufficient. The backlog stays near zero.
    This is the healthy case, and it exists so the others have something
    honest to be compared against.
``backlog``
    Arrival grows while capacity is fixed. The service completes real work
    every tick, but the backlog still diverges. This is the "we added cores
    and it held for a while" shape.
``starved``
    Arrival grows and capacity sits far below it. The backlog diverges hard --
    the shape that leaves a listener hours behind.
``redesign``
    The same arrival growth as ``backlog``, but processing is decoupled from
    the process: work is appended to a journal and a separate consumer rolls
    it up, so surplus capacity is applied to the existing backlog. The queue
    drains.

Total work completed is a poor discriminator between these rows. Throughput
alone would not tell you which design to keep; the backlog trajectory does.
Note that ``redesign`` still requires capacity to exceed arrival -- the
redesign buys a horizontal axis, not free compute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import InfraError, Rng, Series

__all__ = ["SCENARIOS", "QueueResult", "simulate_queue", "scenario_names"]


@dataclass(frozen=True)
class Scenario:
    """One queueing configuration.

    ``service_rate`` is work completed per worker per tick and ``workers`` is
    the worker count; their product is the hard ceiling on completions in a
    tick. There is deliberately no knob that lets a scenario exceed it.
    """

    name: str
    description: str
    ticks: int
    arrival_mean: float
    arrival_growth: float
    service_rate: int
    workers: int
    seed_burst_every: int = 0


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(
        name="baseline",
        description=(
            "Arrival is flat and capacity comfortably exceeds it. The backlog "
            "stays at or near zero; this is the case the others are compared "
            "against."
        ),
        ticks=60,
        arrival_mean=8.0,
        arrival_growth=0.0,
        service_rate=10,
        workers=1,
    ),
    "backlog": Scenario(
        name="backlog",
        description=(
            "Arrival grows at 3% per tick while capacity is fixed at 14 per "
            "tick. The service completes real work every tick, but arrival "
            "passes capacity partway through and the backlog then diverges. "
            "This is the 'we added cores and it held for a while' shape."
        ),
        ticks=60,
        arrival_mean=8.0,
        arrival_growth=0.03,
        service_rate=14,
        workers=1,
        seed_burst_every=12,
    ),
    "redesign": Scenario(
        name="redesign",
        description=(
            "The same 3% arrival growth as 'backlog', but the workers are "
            "horizontally scalable -- the journal architecture lets workers be "
            "added without a single writer becoming the bottleneck -- so "
            "capacity is 52 per tick instead of 14. Because capacity now "
            "exceeds the arrival rate throughout, the queue drains to empty. "
            "Note this is genuinely more capacity, not a free lunch: the "
            "redesign buys a horizontal axis, and this scenario spends it."
        ),
        ticks=60,
        arrival_mean=8.0,
        arrival_growth=0.03,
        service_rate=52,
        workers=1,
        seed_burst_every=12,
    ),
    "starved": Scenario(
        name="starved",
        description=(
            "Arrival grows at 3% per tick and capacity sits far below it at 6 "
            "per tick. The backlog diverges hard: the shape that page-cycles a "
            "service and leaves a listener hours behind."
        ),
        ticks=60,
        arrival_mean=8.0,
        arrival_growth=0.03,
        service_rate=6,
        workers=1,
        seed_burst_every=10,
    ),
}


def scenario_names() -> list[str]:
    return sorted(SCENARIOS)


@dataclass
class QueueResult:
    """Per-tick queue accounting for one scenario."""

    scenario: str
    description: str
    ticks: int
    queued_total: int
    run_total: int
    backlog_final: int
    backlog_peak: int
    backlog_series: Series = field(default_factory=Series)
    run_series: Series = field(default_factory=Series)
    queued_series: Series = field(default_factory=Series)
    errors: tuple[str, ...] = ()

    @property
    def drained(self) -> bool:
        """True when the backlog returned to zero by the end of the run."""
        return self.backlog_final == 0

    @property
    def backlog_trend(self) -> str:
        """Whether the backlog accumulated, drained, or stayed put.

        Derived from the per-tick change in backlog over the second half of
        the run, not from a ratio of half-averages. A service that starts
        empty and only falls behind late must still read as ``growing``: a
        half-average comparison would call a late divergence "flat" and hide
        the exact failure this model exists to show.
        """
        values = self.backlog_series.values
        if len(values) < 4:
            return "flat"
        half = len(values) // 2
        late = values[half:]
        net_change = late[-1] - late[0]
        window = len(late)

        if net_change > window:
            return "growing"
        if net_change < -window:
            return "draining"
        return "flat"

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "ticks": self.ticks,
            "queued_total": self.queued_total,
            "run_total": self.run_total,
            "backlog_final": self.backlog_final,
            "backlog_peak": self.backlog_peak,
            "backlog_trend": self.backlog_trend,
            "drained": self.drained,
            "run_summary": self.run_series.summary(),
            "backlog_summary": self.backlog_series.summary(),
            "ok": self.ok,
            "errors": list(self.errors),
        }


def simulate_queue(scenario: Scenario, seed: int) -> QueueResult:
    """Run one queueing scenario deterministically.

    Invariant worth stating explicitly, because it is the whole reason this
    model exists: ``run_total <= queued_total`` at every tick. Work cannot be
    completed before it is accepted. A capacity model that reports more
    completed jobs than accepted jobs is reporting a number that cannot be
    true, and the assertion below refuses to let that pass.
    """
    if scenario.ticks <= 0:
        raise InfraError(f"scenario {scenario.name!r} needs a positive tick count")
    if scenario.service_rate <= 0:
        raise InfraError(f"scenario {scenario.name!r} needs a positive service rate")
    if scenario.workers <= 0:
        raise InfraError(f"scenario {scenario.name!r} needs at least one worker")

    rng = Rng(seed)
    capability = scenario.service_rate * scenario.workers

    backlog = 0
    queued_total = 0
    run_total = 0
    backlog_series = Series()
    run_series = Series()
    queued_series = Series()
    errors: list[str] = []

    for tick in range(1, scenario.ticks + 1):
        mean = scenario.arrival_mean * (1.0 + scenario.arrival_growth) ** (tick - 1)
        arrivals = rng.poisson(min(mean, 500.0))
        if scenario.seed_burst_every and tick % scenario.seed_burst_every == 0:
            # A burst models the real shape of agentic traffic: bursts, not a
            # smooth rate. Weekend and overnight pushes raise the floor.
            arrivals += rng.randint(5, 25)

        backlog += arrivals
        queued_total += arrivals

        # One service pass per tick, bounded by capacity. There is exactly one
        # completion path: a design that could exceed ``capability`` per tick
        # would be reporting service capacity it does not have. The journal
        # redesign in the source account does not add free throughput -- it
        # makes the *workers* horizontally scalable, which is expressed here by
        # raising the capacity the scenario is configured with, not by running
        # the queue twice.
        completed = min(backlog, capability)
        backlog -= completed
        run_total += completed

        if completed > capability:
            errors.append(
                f"tick {tick}: completed {completed} exceeds capacity "
                f"{capability}; service rate is being double-counted"
            )
            break

        backlog_series.append(float(backlog))
        run_series.append(float(completed))
        queued_series.append(float(arrivals))

        if run_total > queued_total:
            errors.append(
                f"tick {tick}: completed {run_total} work items but only "
                f"{queued_total} were accepted; the model is unsound"
            )
            break

    # Conservation of work: everything accepted is either completed or still
    # in the backlog. If this does not hold, a number in the report is wrong,
    # and a capacity report with wrong numbers is worse than no report.
    if run_total + backlog != queued_total:
        errors.append(
            f"accounting violation: accepted {queued_total} != completed "
            f"{run_total} + backlog {backlog}"
        )

    return QueueResult(
        scenario=scenario.name,
        description=scenario.description,
        ticks=scenario.ticks,
        queued_total=queued_total,
        run_total=run_total,
        backlog_final=backlog,
        backlog_peak=int(backlog_series.peak()),
        backlog_series=backlog_series,
        run_series=run_series,
        queued_series=queued_series,
        errors=tuple(errors),
    )
