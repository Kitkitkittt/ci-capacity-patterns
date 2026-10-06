#!/usr/bin/env python3
"""Collect bounded, anonymized GitHub activity and render the frozen snapshot.

Collection requires access to both source repositories. Repository names and run/PR
identifiers are used only in memory; the committed snapshot contains aggregates.
Rendering needs only Python's standard library and the committed snapshot.

The weekly window is explicit: ``--start`` (a Monday, default 2026-08-03) and
``--cutoff`` (the capture instant for run/PR creation) define every week. Week
count and all chart scales are derived from the snapshot, so a later collection
extends the series without hand-editing this file or clipping any value.

Run *conclusions* are whatever the API reports at collection time; they are not
reconstructed as of the creation cutoff, because a run created before the cutoff
may have finished afterwards. ``window_utc`` is the creation bound,
``captured_through_utc`` the run/PR creation cutoff, and ``collected_at_utc`` the
instant the API was actually read.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs/diagrams/activity-snapshot.json"
FIGURE = ROOT / "docs/diagrams/activity-weekly.svg"
# The frozen series begins Mon 2026-08-03 UTC. A refresh may pass a later Monday,
# but never moves the lower bound implicitly.
DEFAULT_START = date(2026, 8, 3)
LABELS = ("Fabric implementation", "Data platform")
COLORS = ("#0891b2", "#7c3aed")
OUTCOME_COLORS = {
    "success": "#0f766e",
    "failure": "#b91c1c",
    "cancelled": "#a16207",
    "skipped": "#64748b",
    "timed_out": "#9a3412",
    "startup_failure": "#7c2d12",
    "action_required": "#7e22ce",
    "neutral": "#475569",
    "stale": "#57534e",
    # A run whose conclusion is unset and whose status is unknown is labeled, not
    # dropped, so the outcome series is never silently short of runs.
    "unrecorded": "#334155",
}
FALLBACK_COLORS = ("#1d4ed8", "#be185d", "#4d7c0f", "#0369a1", "#92400e")


def github(path: str) -> dict:
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=False, timeout=45
    )
    if result.returncode:
        raise RuntimeError("GitHub API request failed; check authentication and access")
    return json.loads(result.stdout)


API_PAGE_LIMIT = 500


def parse_instant(value: str, label: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO 8601 UTC instant") from error
    if moment.tzinfo is None:
        raise ValueError(f"{label} must carry a timezone (use a trailing Z)")
    return moment.astimezone(timezone.utc)


def parse_start(value: str) -> date:
    try:
        day = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("--start must be an ISO 8601 UTC date") from error
    if day.weekday() != 0:
        raise ValueError("--start must be a Monday so weeks tile evenly")
    return day


def render_instant(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def collect(repositories: list[str], start: date, cutoff: datetime) -> dict:
    if len(repositories) != 2:
        raise ValueError("Provide two authorized repositories")
    if start.weekday() != 0:
        raise ValueError("start must be a Monday so weeks tile evenly")
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must carry a timezone")
    cutoff = cutoff.astimezone(timezone.utc)
    if cutoff.date() < start:
        raise ValueError("cutoff precedes the first week")
    last_index = (cutoff.date() - start).days // 7
    mondays = [start + timedelta(days=7 * index) for index in range(last_index + 1)]
    rows = [{"week": day.isoformat(), "runs": [0, 0], "prs": [0, 0]} for day in mondays]
    conclusions: list[dict[str, int]] = [{}, {}]
    for kind, repo in enumerate(repositories):
        # Runs are returned newest first; a page that ends before --start, or a
        # short page, proves the whole window has been read.
        for page in range(1, API_PAGE_LIMIT + 1):
            payload = github(f"repos/{repo}/actions/runs?per_page=100&page={page}")
            runs = payload["workflow_runs"]
            if not runs:
                break
            crossed_start = False
            for run in runs:
                created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                if created.date() < start:
                    crossed_start = True
                    continue
                if created > cutoff:
                    continue
                index = (created.date() - start).days // 7
                if index >= len(rows):
                    continue
                rows[index]["runs"][kind] += 1
                outcome = run.get("conclusion") or run.get("status") or "unrecorded"
                conclusions[kind][outcome] = conclusions[kind].get(outcome, 0) + 1
            if crossed_start or len(runs) < 100:
                break
        else:
            raise RuntimeError(f"Bounded run history exceeded {API_PAGE_LIMIT} API pages")
        for index, monday in enumerate(mondays):
            last = min(monday + timedelta(days=6), cutoff.date())
            upper = render_instant(cutoff) if last == cutoff.date() else last.isoformat()
            query = quote(f"repo:{repo} type:pr created:{monday.isoformat()}..{upper}")
            result = github(f"search/issues?q={query}&per_page=1")
            if result.get("incomplete_results"):
                raise RuntimeError("GitHub PR search was incomplete; refusing to freeze an undercount")
            rows[index]["prs"][kind] = result["total_count"]
    return {
        "source": "GitHub REST Actions workflow_runs.created_at and Search Issues type:pr created",
        "window_utc": [start.isoformat(), cutoff.date().isoformat()],
        "captured_through_utc": render_instant(cutoff),
        "collected_at_utc": render_instant(datetime.now(timezone.utc)),
        "categories": list(LABELS),
        "definition": (
            "Workflow runs counted by created_at; PRs by creation, not merge. Run outcomes are read "
            "at collected_at_utc, not reconstructed as of captured_through_utc. The final week is "
            "partial. An outcome is the run conclusion when present, otherwise its lifecycle status "
            "(for example queued or in_progress); 'unrecorded' means the API returned neither at "
            "collection, so no run is dropped."
        ),
        "weeks": rows,
        "run_conclusions": conclusions,
    }


def scale_axis(maximum: float, tick: int) -> tuple[float, list[int]]:
    """Round a data maximum up to a whole multiple of its tick."""
    scale = max(tick, int(math.ceil(maximum / tick)) * tick)
    return scale, list(range(0, scale + 1, tick))


def ordered_outcomes(conclusions: list[dict[str, int]]) -> list[str]:
    seen = {name for entry in conclusions for name in entry}
    ordered = [name for name in OUTCOME_COLORS if name in seen]
    return ordered + sorted(name for name in seen if name not in OUTCOME_COLORS)


def outcome_layout(snapshot: dict) -> tuple[list[str], dict[str, str], dict[str, int], int]:
    conclusions = snapshot["run_conclusions"]
    if len(conclusions) != len(LABELS):
        raise ValueError("Snapshot conclusion series does not match categories")
    totals = {label: sum(conclusions[index].values()) for index, label in enumerate(LABELS)}
    names = ordered_outcomes(conclusions)
    colors: dict[str, str] = {}
    fallback = 0
    for name in names:
        if name in OUTCOME_COLORS:
            colors[name] = OUTCOME_COLORS[name]
        else:
            colors[name] = FALLBACK_COLORS[fallback % len(FALLBACK_COLORS)]
            fallback += 1
    return names, colors, totals, max(1, *totals.values())


def svg(snapshot: dict) -> str:
    weeks = snapshot["weeks"]
    if not weeks or snapshot["categories"] != list(LABELS):
        raise ValueError("Unsupported snapshot shape")
    start = date.fromisoformat(snapshot["window_utc"][0])
    end = date.fromisoformat(snapshot["window_utc"][1])
    horizon = f"{start.strftime('%b')} {start.day} \u2013 {end.strftime('%b')} {end.day}, {end.year}"
    names, colors, totals, grand = outcome_layout(snapshot)
    read_at = snapshot.get("collected_at_utc", snapshot["captured_through_utc"])

    # Vertical layout is computed so a wider window or a longer outcome list
    # stays inside the viewBox instead of running past it. Bands are separated
    # so the outcome heading clears the PR week labels, the legend clears the
    # footer, and the right edge keeps room for a category total.
    runs_top, runs_bottom = 126, 276
    prs_top, prs_bottom = 316, 456
    xlabel_y = prs_bottom + 22
    heading_y = xlabel_y + 36
    outcome_top = heading_y + 38
    outcome_row = 46
    legend_row = 18
    legend_columns = 3
    legend_rows = math.ceil(len(names) / legend_columns)
    legend_top = outcome_top + outcome_row * len(LABELS) + 22
    footer_top = legend_top + legend_row * legend_rows + 12
    height = footer_top + 34
    bar_left, bar_area = 210.0, 688.0

    plot_left, plot_right = 66, 1000
    step = (plot_right - plot_left) / len(weeks)
    bar_w = min(30.0, max(4.0, (step - 18) / 2))
    group_w = 2 * bar_w + 4
    label_every = 1
    if step < 54:
        label_every = math.ceil(54 / step)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1040 {height}" role="img" aria-labelledby="title desc" font-family="ui-sans-serif,system-ui,sans-serif">',
        '<title id="title">Weekly engineering activity in two access-controlled projects</title>',
        (
            '<desc id="desc">Panels show weekly GitHub Actions workflow runs, pull requests opened, '
            f'and run-outcome composition by category, for UTC weeks {horizon}. The final week is '
            'partial. Run outcomes are as recorded at collection, not reconstructed as of the '
            'creation cutoff. This is activity, not queue time or speedup.</desc>'
        ),
        f'<rect width="1040" height="{height}" fill="#fff"/>',
        '<text x="66" y="34" font-size="20" font-weight="700" fill="#0f172a">Weekly CI activity, not CI capacity</text>',
        f'<text x="66" y="57" font-size="12" fill="#475569">Observed Actions runs and opened PRs \u00b7 UTC weeks \u00b7 {horizon}</text>',
    ]
    for i, label in enumerate(LABELS):
        x = 66 + i * 215
        out += [
            f'<rect x="{x}" y="76" width="12" height="12" rx="2" fill="{COLORS[i]}"/>',
            f'<text x="{x+20}" y="87" font-size="12" fill="#334155">{escape(label)}</text>',
        ]
    for metric, top, bottom, tick, title in (
        ("runs", runs_top, runs_bottom, 100, "Actions workflow runs / week"),
        ("prs", prs_top, prs_bottom, 10, "Pull requests opened / week"),
    ):
        values = [value for week in weeks for value in week[metric]]
        scale, ticks = scale_axis(max(values), tick)
        panel_h = bottom - top
        out.append(f'<text x="66" y="{top-18}" font-size="15" font-weight="700" fill="#0f172a">{title}</text>')
        for v in ticks:
            y = bottom - v * panel_h / scale
            out += [
                f'<line x1="{plot_left}" x2="{plot_right}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e2e8f0"/>',
                f'<text x="55" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#475569">{v}</text>',
            ]
        for index, week in enumerate(weeks):
            gx = plot_left + step * index + (step - group_w) / 2
            for kind, value in enumerate(week[metric]):
                bar_h = value * panel_h / scale
                bx = gx + kind * (bar_w + 2)
                if value:
                    out.append(
                        f'<rect x="{bx:.1f}" y="{bottom-bar_h:.1f}" width="{bar_w:.1f}" '
                        f'height="{bar_h:.1f}" rx="2" fill="{COLORS[kind]}"/>'
                    )
                out.append(
                    f'<text x="{bx+bar_w/2:.1f}" y="{bottom-bar_h-5:.1f}" text-anchor="middle" '
                    f'font-size="10" font-weight="600" fill="#334155">{value}</text>'
                )
            if metric == "prs" and (index % label_every == 0 or index == len(weeks) - 1):
                out.append(
                    f'<text x="{plot_left+step*index+step/2:.1f}" y="{xlabel_y}" text-anchor="middle" '
                    f'font-size="11" fill="#475569">{week["week"][5:]}</text>'
                )

    out.append(
        f'<text x="66" y="{heading_y}" font-size="15" font-weight="700" fill="#0f172a">'
        f'Run outcomes recorded at collection \u00b7 {sum(totals.values())} runs \u00b7 read {escape(read_at)}</text>'
    )

    for kind, caption in enumerate(LABELS):
        entry = snapshot["run_conclusions"][kind]
        y = outcome_top + 6 + kind * outcome_row
        out.append(f'<text x="66" y="{y+19}" font-size="12" fill="#334155">{escape(caption)}</text>')
        offset = bar_left
        for name in names:
            count = entry.get(name, 0)
            if not count:
                continue
            width = count / grand * bar_area
            out.append(f'<rect x="{offset:.1f}" y="{y}" width="{width:.1f}" height="26" fill="{colors[name]}"/>')
            if width >= 28:
                out.append(
                    f'<text x="{offset+width/2:.1f}" y="{y+17}" text-anchor="middle" '
                    f'font-size="10" fill="#f8fafc">{count}</text>'
                )
            offset += width
        out.append(f'<text x="{offset+8:.1f}" y="{y+18}" font-size="11" fill="#475569">{totals[caption]} runs</text>')

    column_w = (plot_right - plot_left) / legend_columns
    for index, name in enumerate(names):
        row, column = divmod(index, legend_columns)
        lx = plot_left + column * column_w
        ly = legend_top + row * legend_row
        out.append(f'<rect x="{lx:.1f}" y="{ly-10}" width="11" height="11" rx="2" fill="{colors[name]}"/>')
        count = sum(entry.get(name, 0) for entry in snapshot["run_conclusions"])
        out.append(f'<text x="{lx+17:.1f}" y="{ly}" font-size="11" fill="#334155">{escape(name)} \u00b7 {count}</text>')

    out += [
        f'<text x="66" y="{footer_top}" font-size="11" fill="#475569">Each bar counts events, not jobs queued or CPU time; skipped runs are included; outcome bars scale to the larger category.</text>',
        (
            f'<text x="66" y="{footer_top+16}" font-size="11" fill="#475569">Last week partial; runs and PRs '
            f'created by {escape(snapshot["captured_through_utc"])}. No before/after performance claim.</text>'
        ),
        '</svg>',
    ]
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", nargs=2, metavar=("FABRIC_REPO", "DATA_REPO"))
    parser.add_argument(
        "--start",
        default=DEFAULT_START.isoformat(),
        metavar="YYYY-MM-DD",
        help="first UTC week (must be a Monday); default %(default)s",
    )
    parser.add_argument(
        "--cutoff",
        metavar="ISO8601",
        help="creation cutoff for collection; default now (only used with --collect)",
    )
    args = parser.parse_args()
    try:
        if args.collect:
            start = parse_start(args.start)
            cutoff = parse_instant(args.cutoff, "--cutoff") if args.cutoff else datetime.now(timezone.utc)
            SNAPSHOT.write_text(json.dumps(collect(args.collect, start, cutoff), indent=2) + "\n")
    except ValueError as error:
        raise SystemExit(f"error: {error}")
    snapshot = json.loads(SNAPSHOT.read_text())
    FIGURE.write_text(svg(snapshot))
    print(f"Rendered {FIGURE.relative_to(ROOT)} from {SNAPSHOT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
