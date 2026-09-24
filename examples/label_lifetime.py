#!/usr/bin/env python3
"""Ephemeral runner lifetime and label routing.

Models the part of runner capacity a CPU count cannot see: a job is matched to
a runner by *labels*, and if nothing matching is idle, the job waits. In a real
fleet that wait does not surface as an error. It surfaces as a job sitting in a
queue until GitHub's 24-hour timeout, which is why the model tracks allocation
and every runner's terminal state rather than just utilisation.

    python3 examples/label_lifetime.py --scenario steady --seed 7
    python3 examples/label_lifetime.py --scenario burst --seed 7
    python3 examples/label_lifetime.py --scenario fault-injection --seed 7
    python3 examples/label_lifetime.py --scenario unsafe-admission --seed 7

The ``unsafe-admission`` scenario is the interesting one: it configures
self-hosted capacity reachable from untrusted fork pull requests. The simulator
*refuses* to run it, because that configuration is a remote-code-execution path
into your infrastructure. See README section 5.

Invariants asserted here and in tests/test_lifetime.py:

* a job is never assigned to an already-busy runner;
* an ephemeral runner serves exactly one job and is then destroyed;
* every runner created reaches a terminal state (no leaked VMs);
* a provisioning failure surfaces as an error, never as a silent success.

Exit status is 0 when the run completed cleanly, 2 on an infrastructure error
or a refused (unsafe) configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cisim import InfraError, Rng, load_json, render_report, write_report  # noqa: E402
from cisim.lifetime import (  # noqa: E402
    Job,
    Pool,
    PoolKind,
    SimulationResult,
    admission_for,
    run_pool_simulation,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POOLS = os.path.join(HERE, "fixtures", "pools.json")

EXIT_OK = 0
EXIT_INFRA = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="label_lifetime.py",
        description=(
            "Simulate ephemeral runner lifetime and label routing. Offline, "
            "deterministic, standard library only."
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=["steady", "burst", "fault-injection", "unsafe-admission"],
        default="steady",
        help="traffic shape to simulate (default: steady)",
    )
    parser.add_argument(
        "--pools",
        default=DEFAULT_POOLS,
        metavar="FILE",
        help=f"pool definitions JSON (default: {os.path.relpath(DEFAULT_POOLS)})",
    )
    parser.add_argument(
        "--pool",
        metavar="ID",
        help="run only the named pool instead of every pool in the fixture",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="PRNG seed; identical seeds give identical output (default: 7)",
    )
    parser.add_argument(
        "--max-queue-wait",
        type=int,
        default=10_000,
        metavar="N",
        help=(
            "how many ticks a job may sit queued behind busy runners before it "
            "is counted as unplaced (default: 10000, i.e. effectively unbounded). "
            "Lower it to model a real queue deadline and surface saturation."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable JSON report instead of the text report",
    )
    parser.add_argument(
        "--report-dir",
        metavar="DIR",
        help="also write lifetime_report.txt and lifetime_report.json into DIR",
    )
    return parser.parse_args(argv)


def build_pool(spec: dict) -> Pool:
    """Construct a :class:`Pool` from one fixture entry, validating loudly."""
    for key in ("id", "labels", "kind", "capacity", "admission", "admission_mode"):
        if key not in spec:
            raise InfraError(f"pool entry is missing required key {key!r}: {spec}")
    try:
        kind = PoolKind(spec["kind"])
    except ValueError as exc:
        raise InfraError(
            f"pool {spec['id']!r} has unknown kind {spec['kind']!r}; "
            f"expected one of {[k.value for k in PoolKind]}"
        ) from exc
    labels = spec["labels"]
    if not isinstance(labels, list) or not labels:
        raise InfraError(f"pool {spec['id']!r} needs a non-empty label list")
    if not isinstance(spec["admission_mode"], str):
        raise InfraError(f"pool {spec['id']!r} needs a string admission_mode")
    admission_for(kind, spec["admission_mode"])
    return Pool(
        name=spec["id"],
        labels=tuple(str(label) for label in labels),
        kind=kind,
        capacity=int(spec["capacity"]),
        admission=int(spec["admission"]),
        warm_pool=int(spec.get("warm_pool", 0)),
    )


def make_jobs(scenario: str, seed: int, labels: tuple[str, ...]) -> list[Job]:
    """Generate the job stream for a scenario.

    Kept deliberately small and reproducible: the point is the shape of the
    arrival (steady vs. bursty), not a realistic volume.
    """
    rng = Rng(seed)
    count = {"steady": 20, "burst": 40, "fault-injection": 24, "unsafe-admission": 8}[
        scenario
    ]
    jobs: list[Job] = []
    for index in range(count):
        if scenario == "burst" and index % 7 == 0:
            # Bursts: several jobs land at once and contend for the same labels.
            for extra in range(4):
                jobs.append(
                    Job(
                        job_id=f"job-{index:03d}-burst{extra}",
                        labels=labels,
                        duration=rng.randint(1, 3),
                    )
                )
            continue
        jobs.append(
            Job(
                job_id=f"job-{index:03d}",
                labels=labels,
                duration=rng.randint(1, 4),
            )
        )
    return jobs


def render_result(result: SimulationResult, pool: Pool, scenario: str) -> str:
    lines = [
        f"pool          : {result.pool_name} ({pool.kind.value})",
        f"scenario      : {scenario}",
        f"labels        : {', '.join(pool.labels)}",
        f"ephemeral     : {pool.ephemeral}",
        f"admission cap : {pool.admission} (warm pool {pool.warm_pool})",
        "",
        f"  runners created   : {result.created}",
        f"  runners destroyed : {result.destroyed}",
        f"  runners failed    : {result.failed}",
        f"  runners leaked    : {result.leaked}",
        f"  jobs served       : {result.jobs_served}",
        f"  jobs unplaced     : {result.unplaced}",
        f"  utilisation       : {result.utilisation:.2%}",
        f"  max wait (ticks)  : {max(result.wait_ticks) if result.wait_ticks else 0}",
    ]
    if result.errors:
        lines.append("")
        lines.append("  errors:")
        lines.extend(f"    - {error}" for error in result.errors)
    lines.append("")
    if not result.healthy:
        lines.append(
            "  verdict: FAILED (lifecycle invariants violated; an infrastructure "
            "fault is never reported as success)"
        )
    elif result.saturated:
        lines.append(
            f"  verdict: SATURATED ({result.unplaced} job(s) unplaced) -- the pool "
            "is correctly sized for fewer concurrent jobs than arrived; this is a "
            "capacity finding, not a fault"
        )
    else:
        lines.append(
            "  verdict: OK (every job placed; all runner lifecycles terminated "
            "cleanly)"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    def emit_error(message: str) -> int:
        payload = {"ok": False, "error": message}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                render_report(
                    "RUNNER LIFETIME -- INFRASTRUCTURE ERROR",
                    [
                        f"error: {message}",
                        "",
                        "verdict: FAILED (no simulation ran; this is not a pass)",
                    ],
                ),
                end="",
            )
        return EXIT_INFRA

    try:
        payload = load_json(args.pools)
        specs = payload.get("pools") if isinstance(payload, dict) else None
        if not isinstance(specs, list) or not specs:
            raise InfraError(f"pool fixture has no 'pools' list: {args.pools}")
        if args.pool:
            specs = [s for s in specs if s.get("id") == args.pool]
            if not specs:
                raise InfraError(f"no pool with id {args.pool!r} in {args.pools}")
        if args.scenario == "unsafe-admission":
            specs = [spec for spec in specs if spec["kind"] != PoolKind.HOSTED.value]
            if not specs:
                raise InfraError("unsafe-admission requires a self-hosted pool")
        pools = [build_pool(spec) for spec in specs]
    except InfraError as exc:
        return emit_error(str(exc))

    results: list[tuple[SimulationResult, Pool]] = []
    refusal: str = ""

    for pool, spec in zip(pools, specs):
        admission_mode = spec["admission_mode"]
        if args.scenario == "unsafe-admission":
            admission_mode = "untrusted-fork"
        jobs = make_jobs(args.scenario, args.seed, pool.labels)
        try:
            result = run_pool_simulation(
                pool,
                jobs,
                admission_mode=admission_mode,
                max_queue_wait=args.max_queue_wait,
                faulty_runner_every=5 if args.scenario == "fault-injection" else 0,
            )
        except InfraError as exc:
            if args.scenario == "unsafe-admission":
                # Expected: the simulator refuses to model self-hosted capacity
                # that untrusted fork code could reach. Report it as a refusal,
                # not as a crash, and keep the exit status non-zero.
                refusal = str(exc)
                break
            return emit_error(str(exc))
        results.append((result, pool))

    if refusal:
        message = (
            "refused to simulate this configuration: " + refusal
        )
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "refused": True, "error": message},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(
                render_report(
                    "RUNNER LIFETIME -- CONFIGURATION REFUSED",
                    [
                        refusal,
                        "",
                        "This is the safety rule, not a modelling limitation.",
                        "A self-hosted runner reachable from untrusted fork pull",
                        "requests lets a fork execute arbitrary code on your",
                        "infrastructure. See README section 5.",
                        "",
                        "verdict: REFUSED (exit non-zero on purpose)",
                    ],
                ),
                end="",
            )
        return EXIT_INFRA

    # Exit status reflects *faults*, not capacity findings. A saturated pool
    # that respected every lifecycle invariant exited cleanly: the operator
    # asked how the pool behaves under this load, and got a truthful answer.
    # Only a violated invariant or a provisioning error is non-zero.
    any_failed = any(not result.healthy for result, _ in results)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not any_failed,
                    "scenario": args.scenario,
                    "seed": args.seed,
                    "results": [result.as_dict() for result, _ in results],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        blocks = [
            render_result(result, pool, args.scenario) for result, pool in results
        ]
        print(
            render_report(
                "RUNNER LIFETIME AND LABEL ROUTING",
                "\n\n".join(blocks).splitlines(),
            ),
            end="",
        )

    if args.report_dir:
        blocks = [render_result(result, pool, args.scenario) for result, pool in results]
        write_report(
            args.report_dir,
            "lifetime_report.txt",
            render_report(
                "RUNNER LIFETIME AND LABEL ROUTING",
                "\n\n".join(blocks).splitlines(),
            ),
        )
        write_report(
            args.report_dir,
            "lifetime_report.json",
            json.dumps(
                {
                    "ok": not any_failed,
                    "scenario": args.scenario,
                    "seed": args.seed,
                    "results": [result.as_dict() for result, _ in results],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    return EXIT_INFRA if any_failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
