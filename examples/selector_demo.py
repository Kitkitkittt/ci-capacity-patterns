#!/usr/bin/env python3
"""Test impact selection with a fail-closed staleness guard.

Given a set of changed files, decide which test suites must run. The demo is
small on purpose: a dependency graph, a fixture history, and a policy. The
policy is the part worth reading.

Fail-closed rules, in the order they apply:

1. A changed file that maps to no suite in the dependency graph selects the
   *full* suite. An empty selection would make the pipeline green because
   nothing ran -- the silent-skip bug.
2. A suite whose recorded history is older than the freshness window is
   selected as a full run. An old green result is not evidence about today.
3. A suite with no history entry at all is selected, and reported as resting
   on missing data.
4. A history file that cannot be parsed is an infrastructure error: non-zero
   exit, no selection, no green.

Exit status is 0 only when a trustworthy selection was produced.

Usage
-----
    python3 examples/selector_demo.py --changed api/handlers.py
    python3 examples/selector_demo.py --changed packages/db/schema.sql \
        --history examples/fixtures/history_stale.json
    python3 examples/selector_demo.py --changed-file-list changed.txt --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cisim import (  # noqa: E402  (path set up above)
    InfraError,
    load_json,
    render_report,
    write_report,
)
from cisim.selector import Selector  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

DEFAULT_GRAPH = os.path.join(FIXTURES, "dependency_graph.json")
DEFAULT_HISTORY = os.path.join(FIXTURES, "history_fresh.json")

EXIT_OK = 0
EXIT_INFRA = 2

#: Maps package -> category so the report can group the selection. Purely
#: presentational; the selector itself does not use it.
CATEGORY_HINTS = (
    ("api/", "api"),
    ("packages/db/", "storage"),
    ("packages/web/", "web"),
    ("tools/", "tooling"),
    ("shared/", "shared"),
)


def categorise(suite: str) -> str:
    for prefix, category in CATEGORY_HINTS:
        if suite.startswith(prefix):
            return category
    return "other"


def build_selector(graph_path: str, freshness: int) -> Selector:
    payload = load_json(graph_path)
    if not isinstance(payload, dict):
        raise InfraError(f"dependency graph must be a JSON object: {graph_path}")
    graph = payload.get("graph")
    all_suites = payload.get("all_suites")
    if not isinstance(graph, dict):
        raise InfraError(f"dependency graph is missing a 'graph' object: {graph_path}")
    if not isinstance(all_suites, list) or not all_suites:
        raise InfraError(
            f"dependency graph is missing a non-empty 'all_suites' list: {graph_path}"
        )
    declared_freshness = payload.get("freshness_window")
    if declared_freshness is not None and (
        not isinstance(declared_freshness, int)
        or isinstance(declared_freshness, bool)
        or declared_freshness < 0
    ):
        raise InfraError(f"'freshness_window' must be a non-negative int: {graph_path}")
    window = freshness if freshness >= 0 else declared_freshness
    if window is None:
        raise InfraError(f"dependency graph needs a 'freshness_window': {graph_path}")
    return Selector(graph=graph, all_suites=all_suites, freshness_window=window)


def read_changed_file_list(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise InfraError(f"cannot read changed-file list {path}: {exc}") from exc
    entries = [line.strip() for line in lines]
    cleaned = [line for line in entries if line and not line.startswith("#")]
    if not cleaned:
        raise InfraError(
            f"changed-file list {path} is empty after removing blanks and comments; "
            "an empty change set would select nothing"
        )
    return cleaned


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="selector_demo.py",
        description=(
            "Map changed files to test suites with a conservative, fail-closed "
            "staleness policy. Offline and deterministic."
        ),
    )
    parser.add_argument(
        "--changed",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "a changed file path; repeat for multiple files, e.g. "
            "--changed api/handlers.py --changed shared/types.py"
        ),
    )
    parser.add_argument(
        "--changed-file-list",
        metavar="FILE",
        help="read changed paths from FILE, one per line (blank and '#' lines ignored)",
    )
    parser.add_argument(
        "--history",
        default=DEFAULT_HISTORY,
        metavar="FILE",
        help=f"recorded test history JSON (default: {os.path.relpath(DEFAULT_HISTORY)})",
    )
    parser.add_argument(
        "--graph",
        default=DEFAULT_GRAPH,
        metavar="FILE",
        help=(f"dependency graph JSON (default: {os.path.relpath(DEFAULT_GRAPH)})"),
    )
    parser.add_argument(
        "--freshness",
        type=int,
        default=-1,
        metavar="N",
        help=(
            "override the freshness window; history older than N units forces a "
            "full run. Negative means use the value declared in the graph."
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
        help="also write selection_report.txt and selection_report.json into DIR",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    changed = list(args.changed)
    if args.changed_file_list:
        changed.extend(read_changed_file_list(args.changed_file_list))
    if not changed:
        print(
            "selector_demo: no changed files given; pass --changed PATH or "
            "--changed-file-list FILE",
            file=sys.stderr,
        )
        return EXIT_INFRA

    try:
        selector = build_selector(args.graph, args.freshness)
        history = load_json(args.history)
        decision = selector.select(changed, history)
    except InfraError as exc:
        # Fail closed: an infrastructure fault is a non-zero exit with an
        # explicit error verdict, never an empty-but-green selection.
        payload = {
            "ok": False,
            "error": str(exc),
            "selection": None,
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                render_report(
                    "TEST IMPACT SELECTION -- INFRASTRUCTURE ERROR",
                    [
                        f"error: {exc}",
                        "",
                        "verdict: FAILED (no selection produced; this is not a pass)",
                    ],
                )
            )
        return EXIT_INFRA

    if not decision.ok:
        payload = decision.as_dict()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                render_report(
                    "TEST IMPACT SELECTION -- SELECTOR ERROR",
                    [
                        f"error: {decision.error}",
                        "",
                        "verdict: FAILED (no selection produced; this is not a pass)",
                    ],
                )
            )
        return EXIT_INFRA

    selection = decision.selection
    assert selection is not None  # implied by decision.ok

    if selection.is_empty:
        # Cannot normally happen: the selector refuses to return an empty
        # selection for a non-empty change set. Asserting it here means a
        # future regression surfaces as a loud failure rather than a green
        # build that ran nothing.
        print(
            "selector_demo: selector returned an empty selection for a non-empty "
            "change set, which is never a valid outcome",
            file=sys.stderr,
        )
        return EXIT_INFRA

    by_category: dict[str, list[str]] = {}
    for suite in selection.suites:
        by_category.setdefault(categorise(suite), []).append(suite)

    text_lines = [
        f"changed files ({len(selection.changed)}):",
    ]
    text_lines.extend(f"  - {path}" for path in selection.changed)
    text_lines.append("")
    text_lines.append(
        f"selected suites ({len(selection.suites)} of {len(selector.all_suites)} known):"
    )
    for category in sorted(by_category):
        suites = sorted(by_category[category])
        text_lines.append(f"  [{category}] {len(suites)} suite(s)")
        text_lines.extend(f"      {suite}" for suite in suites)
    text_lines.append("")
    text_lines.append(
        f"freshness window: {selector.freshness_window} (history older than this "
        "forces a full run)"
    )
    text_lines.append(f"full-suite fallback triggered: {selection.full_suite_forced}")
    if selection.unmapped_files:
        text_lines.append("unmapped changed files (forced full suite):")
        text_lines.extend(f"  - {path}" for path in selection.unmapped_files)
    if selection.stale_suites:
        text_lines.append("suites selected on stale history:")
        text_lines.extend(f"  - {suite}" for suite in selection.stale_suites)
    if selection.missing_history_suites:
        text_lines.append("suites with no recorded history:")
        text_lines.extend(f"  - {suite}" for suite in selection.missing_history_suites)

    text_lines.append("")
    text_lines.append("reasons:")
    for reason in selection.reasons:
        text_lines.append(f"  [{reason.code}] {reason.suite}: {reason.detail}")

    text_lines.append("")
    if decision.degraded:
        text_lines.append("degraded (selection rests on incomplete data):")
        text_lines.extend(f"  - {item}" for item in decision.degraded)
        text_lines.append(
            "verdict: OK WITH DEGRADED HISTORY "
            "(conservative full runs selected; not a silent skip)"
        )
    else:
        text_lines.append("verdict: OK (selection derived entirely from fresh history)")

    text_report = render_report("TEST IMPACT SELECTION", text_lines)

    payload: dict[str, Any] = {
        "ok": True,
        "known_suites": len(selector.all_suites),
        "freshness_window": selector.freshness_window,
        **decision.as_dict(),
    }

    if args.report_dir:
        write_report(args.report_dir, "selection_report.txt", text_report)
        write_report(
            args.report_dir,
            "selection_report.json",
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(text_report, end="")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
