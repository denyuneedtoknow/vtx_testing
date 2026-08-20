#!/usr/bin/env python3
"""Draw shared SVG charts for two VTX power-table CSV files."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from xml.sax.saxutils import escape

from plot_vtx_power_table import read_table


COLORS = ("#2563eb", "#dc2626")
POWERS = (25, 2000, 4000)


def svg_chart(first, second, output: Path, *, ratio: bool, labels: tuple[str, str]) -> None:
    width, height = 1440, 1020
    left, right, top, bottom = 92, 36, 70, 72
    gap = 45
    panel_height = (height - top - bottom - gap * 2) / 3
    frequencies = [point[0] for point in first]
    frequency_min, frequency_max = min(frequencies), max(frequencies)
    x_of = lambda frequency: left + (width - left - right) * (frequency - frequency_min) / (frequency_max - frequency_min)
    body = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="white"/>',
            '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#172033}.title{font-size:23px;font-weight:700}.subtitle{font-size:13px;fill:#526072}.axis{font-size:12px}.panel{font-size:16px;font-weight:700}.legend{font-size:14px;font-weight:600}</style>',
            f'<text x="{left}" y="32" class="title">{"VTX #2 relative to VTX #1" if ratio else "VTX output comparison"}</text>',
            f'<text x="{left}" y="54" class="subtitle">{escape(labels[0])} — blue · {escape(labels[1])} — red · 176 factory channel-points</text>']
    for panel, power in enumerate(POWERS):
        y_top = top + panel * (panel_height + gap)
        y_bottom = y_top + panel_height
        raw = ([b[2][power] / a[2][power] * 100 for a, b in zip(first, second)] if ratio
               else [point[2][power] for point in first] + [point[2][power] for point in second])
        if ratio:
            y_min = max(0.0, math.floor((min(raw) - 5) / 10) * 10)
            y_max = math.ceil((max(raw) + 5) / 10) * 10
            ticks = list(range(int(y_min), int(y_max) + 1, max(10, int(math.ceil((y_max-y_min) / 5 / 10) * 10))))
            title = f"{('2 W' if power == 2000 else '4 W' if power == 4000 else '25 mW')} · VTX #2 / VTX #1, %"
        else:
            step = 50 if max(raw) <= 500 else 1000
            y_min, y_max = 0.0, math.ceil(max(raw) / step) * step
            ticks = list(range(0, int(y_max) + 1, step))
            title = f"{('2 W' if power == 2000 else '4 W' if power == 4000 else '25 mW')} · path-corrected output, mW"
        y_of = lambda value: y_top + panel_height * (1 - (value - y_min) / (y_max - y_min))
        body.append(f'<text x="{left}" y="{y_top-10:.1f}" class="panel">{title}</text>')
        for tick in ticks:
            y = y_of(tick)
            suffix = "%" if ratio else ""
            body.extend([f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d8dee9"/>',
                         f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="axis">{tick:g}{suffix}</text>'])
        if ratio and y_min <= 100 <= y_max:
            y = y_of(100)
            body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#374151" stroke-width="1.5" stroke-dasharray="6 5"/>')
        datasets = ([first, second] if not ratio else [first])
        for index, dataset in enumerate(datasets):
            points = []
            for item_index, point in enumerate(dataset):
                value = second[item_index][2][power] / point[2][power] * 100 if ratio else point[2][power]
                points.append(f"{x_of(point[0]):.1f},{y_of(value):.1f}")
            body.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{COLORS[index]}" stroke-width="2.1" stroke-linejoin="round"/>')
        if panel == len(POWERS) - 1:
            for frequency in range(int(math.ceil(frequency_min / 200) * 200), frequency_max + 1, 200):
                x = x_of(frequency)
                body.extend([f'<line x1="{x:.1f}" y1="{y_top}" x2="{x:.1f}" y2="{y_bottom}" stroke="#edf0f4"/>',
                             f'<text x="{x:.1f}" y="{y_bottom+23:.1f}" text-anchor="middle" class="axis">{frequency}</text>'])
    if not ratio:
        for index, label in enumerate(labels):
            x = left + index * 130
            body.extend([f'<line x1="{x}" y1="{height-22}" x2="{x+23}" y2="{height-22}" stroke="{COLORS[index]}" stroke-width="3"/>',
                         f'<text x="{x+30}" y="{height-17}" class="legend">{escape(label)}</text>'])
    body.append('</svg>')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(body), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create shared SVG charts from two VTX power-table CSV files.")
    parser.add_argument("first", type=Path, help="First VTX CSV")
    parser.add_argument("second", type=Path, help="Second VTX CSV")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--labels", nargs=2, metavar=("VTX1", "VTX2"), default=("VTX #1", "VTX #2"))
    args = parser.parse_args()
    first, second = read_table(args.first), read_table(args.second)
    if [(point[0], point[1]) for point in first] != [(point[0], point[1]) for point in second]:
        raise ValueError("The CSV files do not contain the same ordered channel-points.")
    stem = f"{args.first.stem}-vs-{args.second.stem}"
    absolute = args.output_dir / f"{stem}-comparison.svg"
    relative = args.output_dir / f"{stem}-ratio.svg"
    labels = tuple(args.labels)
    svg_chart(first, second, absolute, ratio=False, labels=labels)
    svg_chart(first, second, relative, ratio=True, labels=labels)
    print(f"Created: {absolute}")
    print(f"Created: {relative}")


if __name__ == "__main__":
    main()
