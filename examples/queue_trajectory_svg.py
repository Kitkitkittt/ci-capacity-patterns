#!/usr/bin/env python3
"""Render the simulated queue/backlog trajectories as a standalone SVG figure.

Data comes from ``examples/queue_vs_run.py``: the same seeded model the tests
assert against. Nothing here is hand-drawn -- the two panels plot the per-tick
arrival, completed-work, and backlog series for the ``backlog`` and ``redesign``
scenarios at the default seed.

These are model output, not telemetry: the arrivals are synthetic and the
service ceilings are illustrative. Both scenarios receive an identical arrival
process and differ only in service ceiling (14 vs 52 per tick), which is the
point -- both plateau in throughput, and only the backlog trajectory separates
them.

Usage:
    python3 examples/queue_trajectory_svg.py docs/diagrams/queue-trajectory.svg
"""

from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
HEIGHT = 540
WIDTH = 1000
PAD_L = 62
PAD_R = 20
PAD_T = 34
PAD_B = 46

INK = "#0f172a"
MUTED = "#64748b"
GRID = "#e2e8f0"
PANEL = "#f8fafc"

SCENARIOS = (
    {
        "name": "backlog",
        "title": "backlog - capacity held at 14/tick",
        "colour": "#e11d48",
        "note": "arrival passes capacity partway through; backlog diverges",
    },
    {
        "name": "redesign",
        "title": "redesign - journal architecture, 52/tick",
        "colour": "#0891b2",
        "note": "same arrivals, capacity above arrival throughout; drains to zero",
    },
)


def load_model():
    spec = importlib.util.spec_from_file_location(
        "qvr", os.path.join(HERE, "queue_vs_run.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def panel_paths(series: list[float], x0: float, x1: float, y_of) -> str:
    step = (x1 - x0) / max(len(series) - 1, 1)
    points = [f"{x0 + i * step:.2f},{y_of(v):.2f}" for i, v in enumerate(series)]
    return "M" + " L".join(points)


def render(model) -> str:
    panels = []
    for scenario in SCENARIOS:
        result = model.simulate_queue(model.SCENARIOS[scenario["name"]], 7)
        panels.append(
            {
                **scenario,
                "backlog": list(result.backlog_series),
                "run": list(result.run_series),
                "arrival": list(result.queued_series),
                "capacity": model.SCENARIOS[scenario["name"]].service_rate,
                "backlog_final": result.backlog_final,
                "run_total": result.run_total,
                "queued_total": result.queued_total,
            }
        )

    y_max = max(max(p["backlog"]) for p in panels) or 1.0
    y_max = ((int(y_max) // 100) + 1) * 100

    panel_h = (HEIGHT - PAD_T - PAD_B - 24) / 2
    panel_w = WIDTH - PAD_L - PAD_R

    # The plot floor sits above the panel's lower edge, leaving a gutter for the
    # summary line; without it the zero-valued series and their labels collide
    # with the border and each other.
    gutter = 26

    chunks = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}"'
            f' width="{WIDTH}" height="{HEIGHT}" font-family="ui-sans-serif, system-ui,'
            ' -apple-system, Segoe UI, sans-serif" role="img"'
            ' aria-label="Queued work versus completed work for the backlog and redesign'
            ' scenarios of the queue model">'
        ),
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
        (
            f'<text x="{PAD_L}" y="19" font-size="13" font-weight="600" fill="{INK}">'
            "Queued events vs. work performed, by tick</text>"
        ),
        (
            f'<text x="{PAD_L}" y="31" font-size="10.5" fill="{MUTED}">'
            "seed 7 | identical arrival process in both panels | backlog = queued not yet"
            " completed</text>"
        ),
    ]

    for index, panel in enumerate(panels):
        top = PAD_T + 24 + index * (panel_h + 24)
        bottom = top + panel_h
        floor = bottom - gutter

        def y_of(value: float, _top=top, _floor=floor) -> float:
            return _floor - (value / y_max) * (_floor - _top)

        chunks.append(
            f'<rect x="{PAD_L}" y="{top:.1f}" width="{panel_w}" height="{panel_h:.1f}"'
            f' rx="6" fill="{PANEL}" stroke="{GRID}"/>'
        )
        chunks.append(
            f'<text x="{PAD_L + 12}" y="{top + 17:.1f}" font-size="11.5"'
            f' font-weight="600" fill="{INK}">{panel["title"]}</text>'
        )
        chunks.append(
            f'<text x="{PAD_L + 12}" y="{top + 31:.1f}" font-size="10" fill="{MUTED}">'
            f"{panel['note']}</text>"
        )

        for frac in (0.0, 0.5, 1.0):
            value = y_max * frac
            y = y_of(value)
            chunks.append(
                f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L + panel_w}" y2="{y:.1f}"'
                f' stroke="{GRID}" stroke-width="1"/>'
            )
            chunks.append(
                f'<text x="{PAD_L - 8}" y="{y + 3.5:.1f}" font-size="10" fill="{MUTED}"'
                f' text-anchor="end">{int(value)}</text>'
            )

        step = panel_w / max(len(panel["backlog"]) - 1, 1)
        for tick in (0, 20, 40, 60):
            if tick >= len(panel["backlog"]):
                continue
            x = PAD_L + tick * step
            chunks.append(
                f'<line x1="{x:.1f}" y1="{bottom:.1f}" x2="{x:.1f}"'
                f' y2="{bottom + 4:.1f}" stroke="{MUTED}"/>'
            )
            chunks.append(
                f'<text x="{x:.1f}" y="{bottom + 16:.1f}" font-size="10" fill="{MUTED}"'
                f' text-anchor="middle">{tick}</text>'
            )

        cap_y = y_of(panel["capacity"])
        chunks.append(
            f'<line x1="{PAD_L}" y1="{cap_y:.1f}" x2="{PAD_L + panel_w}"'
            f' y2="{cap_y:.1f}" stroke="{MUTED}" stroke-width="1.2"'
            ' stroke-dasharray="5 4"/>'
        )
        chunks.append(
            f'<text x="{PAD_L + 6}" y="{cap_y - 5:.1f}" font-size="10"'
            f' fill="{MUTED}">capacity {panel["capacity"]}/tick</text>'
        )

        arrival_path = panel_paths(panel["arrival"], PAD_L, PAD_L + panel_w, y_of)
        chunks.append(
            f'<path d="{arrival_path}" fill="none" stroke="{MUTED}" stroke-width="1.2"'
            ' stroke-dasharray="2 3" opacity="0.85"/>'
        )

        run_path = panel_paths(panel["run"], PAD_L, PAD_L + panel_w, y_of)
        chunks.append(
            f'<path d="{run_path}" fill="none" stroke="#16a34a" stroke-width="1.8"'
            ' stroke-linejoin="round"/>'
        )

        backlog_path = panel_paths(panel["backlog"], PAD_L, PAD_L + panel_w, y_of)
        chunks.append(
            f'<path d="{backlog_path}" fill="none" stroke="{panel["colour"]}"'
            ' stroke-width="2.1" stroke-linejoin="round"/>'
        )

        last_run = panel["run"][-1]
        chunks.append(
            f'<text x="{PAD_L + panel_w - 6:.1f}" y="{y_of(last_run) + 14:.1f}"'
            ' font-size="10" fill="#16a34a" text-anchor="end">run per tick</text>'
        )
        chunks.append(
            f'<text x="{PAD_L + panel_w - 6:.1f}" y="{y_of(last_run) + 15:.1f}"'
            ' font-size="10" fill="#16a34a" text-anchor="end">run per tick</text>'
        )

        last_y = y_of(panel["backlog"][-1])
        chunks.append(
            f'<text x="{PAD_L + 12}" y="{max(last_y - 11, top + 48):.1f}"'
            f' font-size="10.5" font-weight="600" fill="{panel["colour"]}">'
            f"backlog {int(panel['backlog_final'])}</text>"
        )
        chunks.append(
            f'<text x="{PAD_L + 12}" y="{bottom - 8:.1f}" font-size="10" fill="{INK}">'
            f"completed {int(panel['run_total'])} of {int(panel['queued_total'])} "
            "queued</text>"
        )

    chunks.append(
        f'<text x="{PAD_L}" y="{HEIGHT - 8}" font-size="10" fill="{MUTED}">'
        "Model output, not telemetry: arrivals are synthetic and the service "
        "ceilings are illustrative.</text>"
    )
    chunks.append("</svg>")
    return "\n".join(chunks) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: queue_trajectory_svg.py <output.svg>", file=sys.stderr)
        return 2
    out = argv[1]
    svg = render(load_model())
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(svg)
    print(f"wrote {out} ({len(svg.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
