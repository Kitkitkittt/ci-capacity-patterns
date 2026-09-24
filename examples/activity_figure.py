#!/usr/bin/env python3
"""Collect bounded, anonymized GitHub activity and render the frozen snapshot.

Collection requires access to both source repositories. Repository names and run/PR
identifiers are used only in memory; the committed snapshot contains aggregates.
Rendering needs only Python's standard library and the committed snapshot.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs/diagrams/activity-snapshot.json"
FIGURE = ROOT / "docs/diagrams/activity-weekly.svg"
START = date(2026, 8, 3)
END = date(2026, 9, 24)
LABELS = ("Fabric implementation", "Data platform")
COLORS = ("#0891b2", "#7c3aed")


def github(path: str) -> dict:
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=False, timeout=45
    )
    if result.returncode:
        raise RuntimeError("GitHub API request failed; check authentication and access")
    return json.loads(result.stdout)


def collect(repositories: list[str], cutoff: datetime) -> dict:
    if len(repositories) != 2 or cutoff.tzinfo is None:
        raise ValueError("Provide two authorized repositories and a UTC cutoff")
    cutoff = cutoff.astimezone(timezone.utc)
    if cutoff.date() != END:
        raise ValueError("The frozen window ends on 2026-09-24 UTC")
    mondays = [START + timedelta(days=7 * index) for index in range(8)]
    rows = [
        {"week": day.isoformat(), "runs": [0, 0], "prs": [0, 0]}
        for day in mondays
    ]
    conclusions = [{}, {}]
    for kind, repo in enumerate(repositories):
        # Runs are returned newest first; stop after crossing the lower bound.
        for page in range(1, 100):
            payload = github(f"repos/{repo}/actions/runs?per_page=100&page={page}")
            runs = payload["workflow_runs"]
            if not runs:
                break
            for run in runs:
                created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                if created > cutoff or created.date() > END or created.date() < START:
                    continue
                week = (created.date() - START).days // 7
                rows[week]["runs"][kind] += 1
                outcome = run["conclusion"] or run["status"]
                conclusions[kind][outcome] = conclusions[kind].get(outcome, 0) + 1
            oldest = datetime.fromisoformat(runs[-1]["created_at"].replace("Z", "+00:00"))
            if oldest.date() < START:
                break
        else:
            raise RuntimeError("Bounded run history exceeded 99 API pages")
        for index, monday in enumerate(mondays):
            last = min(monday + timedelta(days=6), END)
            upper = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z") if last == END else last.isoformat()
            query = quote(f"repo:{repo} type:pr created:{monday.isoformat()}..{upper}")
            result = github(f"search/issues?q={query}&per_page=1")
            if result.get("incomplete_results"):
                raise RuntimeError("GitHub PR search was incomplete; refusing to freeze an undercount")
            rows[index]["prs"][kind] = result["total_count"]
    return {
        "source": "GitHub REST Actions workflow_runs.created_at and Search Issues type:pr created",
        "window_utc": [START.isoformat(), END.isoformat()],
        "captured_through_utc": cutoff.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "categories": list(LABELS),
        "definition": "Workflow runs include successes, failures, skipped and cancelled; PRs count creation, not merge; last week is partial.",
        "weeks": rows,
        "run_conclusions": conclusions,
    }


def svg(snapshot: dict) -> str:
    weeks = snapshot["weeks"]
    if len(weeks) != 8 or snapshot["categories"] != list(LABELS):
        raise ValueError("Unsupported snapshot shape")
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1040 570" role="img" aria-labelledby="title desc" font-family="ui-sans-serif,system-ui,sans-serif">',
        '<title id="title">Weekly engineering activity in two access-controlled projects</title>',
        '<desc id="desc">Two bar panels show weekly GitHub Actions workflow runs and pull requests opened, for generic fabric implementation and data platform project categories, from August 3 to September 24, 2026. Final week is partial. This is activity, not queue time or speedup.</desc>',
        '<rect width="1040" height="570" fill="#fff"/>',
        '<text x="66" y="34" font-size="20" font-weight="700" fill="#0f172a">Weekly CI activity, not CI capacity</text>',
        '<text x="66" y="57" font-size="12" fill="#475569">Observed Actions runs and opened PRs · UTC weeks · Aug 3 – Sep 24, 2026</text>',
    ]
    for i, label in enumerate(LABELS):
        x = 66 + i * 215
        out += [f'<rect x="{x}" y="76" width="12" height="12" rx="2" fill="{COLORS[i]}"/>', f'<text x="{x+20}" y="87" font-size="12" fill="#334155">{escape(label)}</text>']
    for metric, top, bottom, tick, max_value, title in (
        ("runs", 136, 310, 100, 400, "Actions workflow runs / week"),
        ("prs", 359, 500, 10, 40, "Pull requests opened / week"),
    ):
        out.append(f'<text x="66" y="{top-17}" font-size="15" font-weight="700" fill="#0f172a">{title}</text>')
        height = bottom - top
        for v in range(0, max_value + 1, tick):
            y = bottom - v * height / max_value
            out += [f'<line x1="66" x2="1000" y1="{y:.1f}" y2="{y:.1f}" stroke="#e2e8f0"/>', f'<text x="55" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#475569">{v}</text>']
        for index, week in enumerate(weeks):
            x = 75 + index * 116
            for kind, value in enumerate(week[metric]):
                if not 0 <= value <= max_value:
                    raise ValueError("Value exceeds chart scale")
                bar_h = max(value * height / max_value, 0)
                bx = x + kind * 39
                if value:
                    out.append(f'<rect x="{bx}" y="{bottom-bar_h:.1f}" width="30" height="{bar_h:.1f}" rx="2" fill="{COLORS[kind]}"/>')
                out.append(f'<text x="{bx+15}" y="{bottom-bar_h-5:.1f}" text-anchor="middle" font-size="10" font-weight="600" fill="#334155">{value}</text>')
            if metric == "prs":
                out.append(f'<text x="{x+34}" y="{bottom+20}" text-anchor="middle" font-size="11" fill="#475569">{week["week"][5:]}</text>')
    out += [
        '<text x="66" y="535" font-size="11" fill="#475569">Each bar counts events, not jobs queued or CPU time. Skipped runs are included.</text>',
        f'<text x="66" y="553" font-size="11" fill="#475569">Last week partial; snapshot through {escape(snapshot["captured_through_utc"])}. No before/after performance claim.</text>',
        '</svg>',
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", nargs=2, metavar=("FABRIC_REPO", "DATA_REPO"))
    parser.add_argument("--cutoff", default="2026-09-24T08:43:24Z")
    args = parser.parse_args()
    if args.collect:
        cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
        SNAPSHOT.write_text(json.dumps(collect(args.collect, cutoff), indent=2) + "\n")
    snapshot = json.loads(SNAPSHOT.read_text())
    FIGURE.write_text(svg(snapshot))
    print(f"Rendered {FIGURE.relative_to(ROOT)} from {SNAPSHOT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
