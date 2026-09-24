#!/usr/bin/env python3
"""Queue depth versus work actually performed.

Demonstrates the distinction that keeps capacity projects honest: a job that
has been *accepted* has not been *run*. While a listener falls behind, the
queue grows and whatever consumes its output is deciding from the past.

Run the scenarios side by side. ``backlog`` and ``redesign`` reach similar
throughput, but only one drains:

    python3 examples/queue_vs_run.py --list-scenarios
    python3 examples/queue_vs_run.py --scenario backlog --seed 7
    python3 examples/queue_vs_run.py --compare --seed 7

The numbers are a model, not a measurement. The invariant that matters is
``run_total <= queued_total`` at every tick: work cannot complete before it is
accepted, and the simulation refuses to report otherwise.

Exit status is 0 when every requested scenario completed without an internal
consistency violation, 2 on an infrastructure error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cisim import InfraError, render_report, write_report  # noqa: E402
from cisim.queue import SCENARIOS, QueueResult, scenario_names, simulate_queue  # noqa: E402

EXIT_OK = 0
EXIT_INFRA = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="queue_vs_run.py",
        description=(
            "Model queued versus run work for a CI control plane under growing "
            "arrival. Offline, deterministic, standard library only."
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=scenario_names(),
        default="backlog",
        help="which scenario to run (default: backlog)",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="run every scenario and print a side-by-side comparison",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="PRNG seed; identical seeds give byte-identical output (default: 7)",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="list available scenarios with their descriptions and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable JSON report instead of the text report",
    )
    parser.add_argument(
        "--report-dir",
        metavar="DIR",
        help="also write queue_report.txt and queue_report.json into DIR",
    )
    return parser.parse_args(argv)


def render_one(result: QueueResult) -> str:
    lines = [
        f"scenario: {result.scenario}",
        f"  {result.description}",
        "",
        f"  ticks simulated        : {result.ticks}",
        f"  work accepted (queued) : {result.queued_total}",
        f"  work completed (run)   : {result.run_total}",
        f"  backlog at end         : {result.backlog_final}",
        f"  backlog peak           : {result.backlog_peak}",
        f"  backlog trend          : {result.backlog_trend}",
        f"  backlog drained        : {result.drained}",
        "",
        "  completed per tick: " + json.dumps(result.run_series.summary()),
        "  backlog per tick  : " + json.dumps(result.backlog_series.summary()),
    ]
    if result.errors:
        lines.append("")
        lines.append("  consistency violations:")
        lines.extend(f"    - {error}" for error in result.errors)
        lines.append("  verdict: FAILED (model produced an impossible result)")
    else:
        lines.append("")
        lines.append("  verdict: OK (run never exceeded accepted work)")
    return "\n".join(lines)


def render_comparison(results: list[QueueResult]) -> str:
    header = (
        f"{'scenario':<12} {'queued':>8} {'run':>8} {'end':>8} "
        f"{'peak':>8} {'trend':>10} {'drained':>8}"
    )
    lines = [
        "queued = work accepted, run = work completed. Only the backlog",
        "trajectory distinguishes a patch from a fix.",
        "",
        header,
        "-" * len(header),
    ]
    for result in results:
        lines.append(
            f"{result.scenario:<12} {result.queued_total:>8} {result.run_total:>8} "
            f"{result.backlog_final:>8} {result.backlog_peak:>8} "
            f"{result.backlog_trend:>10} {str(result.drained):>8}"
        )
    lines.append("")
    lines.append("Reading it:")
    lines.append("  'baseline' shows a healthy service: a flat arrival rate, a backlog")
    lines.append(
        "  that never accumulates, and work completed in step with work accepted."
    )
    lines.append(
        "  'backlog' and 'starved' both diverge, with 'starved' losing the race"
    )
    lines.append("  outright because its service rate sits below the arrival rate.")
    lines.append(
        "  'redesign' handles the same arrival growth as 'backlog' and ends the"
    )
    lines.append(
        "  run with an empty queue, because surplus capacity is applied to the"
    )
    lines.append("  existing backlog instead of being discarded each tick.")
    lines.append("")
    lines.append(
        "  Total work completed is a poor discriminator between these rows. The"
    )
    lines.append("  backlog trajectory is what tells a patch apart from a fix.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list_scenarios:
        lines = []
        for name in scenario_names():
            scenario = SCENARIOS[name]
            lines.append(f"{name}")
            lines.append(f"    {scenario.description}")
            lines.append(
                f"    ticks={scenario.ticks} arrival_mean={scenario.arrival_mean} "
                f"growth={scenario.arrival_growth} service_rate="
                f"{scenario.service_rate} workers={scenario.workers}"
            )
        print(render_report("QUEUE SCENARIOS", lines), end="")
        return EXIT_OK

    selected = scenario_names() if args.compare else [args.scenario]

    try:
        results = [simulate_queue(SCENARIOS[name], args.seed) for name in selected]
    except InfraError as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                render_report(
                    "QUEUE VS RUN -- INFRASTRUCTURE ERROR",
                    [f"error: {exc}", "", "verdict: FAILED (not a pass)"],
                ),
                end="",
            )
        return EXIT_INFRA

    failed = [r for r in results if r.errors]

    if args.json:
        payload = {
            "ok": not failed,
            "seed": args.seed,
            "results": [r.as_dict() for r in results],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.compare:
        print(
            render_report(
                "QUEUE VS RUN -- COMPARISON", render_comparison(results).splitlines()
            )
        )
    else:
        print(render_report("QUEUE VS RUN", render_one(results[0]).splitlines()))

    if args.report_dir:
        text_lines = []
        if args.compare:
            text_lines.append(render_comparison(results))
            text_lines.append("")
        for result in results:
            text_lines.append(render_one(result))
            text_lines.append("")
        text = render_report("QUEUE VS RUN", "\n".join(text_lines).splitlines())
        write_report(args.report_dir, "queue_report.txt", text)
        write_report(
            args.report_dir,
            "queue_report.json",
            json.dumps(
                {
                    "ok": not failed,
                    "seed": args.seed,
                    "results": [r.as_dict() for r in results],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    return EXIT_INFRA if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
