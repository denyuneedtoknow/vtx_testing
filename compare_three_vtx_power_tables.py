#!/usr/bin/env python3
"""Overlay three VTX power-table CSV files on their common frequencies."""

from __future__ import annotations

import argparse
from pathlib import Path
from xml.sax.saxutils import escape

from plot_vtx_power_table import read_table


POWERS = ((25, "25 mW"), (2000, "2 W"), (4000, "4 W"))
COLORS = ("#2563eb", "#d97706", "#dc2626")


def load_by_frequency(path: Path) -> dict[int, dict[int, float]]:
    values: dict[int, dict[int, float]] = {}
    for frequency, _, measurements in read_table(path):
        values.setdefault(frequency, measurements)
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Overlay three VTX power-table CSV files.")
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("third", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/vtx-three-way-comparison.svg"))
    parser.add_argument("--labels", nargs=3, default=("VTX #1", "VTX #2", "VTX #3"))
    args = parser.parse_args()
    tables = [load_by_frequency(path) for path in (args.first, args.second, args.third)]
    frequencies = sorted(set.intersection(*(set(table) for table in tables)))
    if not frequencies:
        raise ValueError("The CSV files have no common frequencies.")
    width, height, left, right, top = 1440, 1030, 100, 45, 70
    panel_height, gap = 255, 52
    plot_width = width - left - right
    x_of = lambda frequency: left + plot_width * (frequency - frequencies[0]) / (frequencies[-1] - frequencies[0])
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="white"/>', '<style>text{font-family:Arial,sans-serif;fill:#172033}.title{font-size:23px;font-weight:bold}.panel{font-size:16px;font-weight:bold}.axis{font-size:12px}.legend{font-size:14px;font-weight:bold}</style>', f'<text x="{left}" y="32" class="title">Three VTX comparison — {len(frequencies)} common frequencies</text>']
    for panel, (power, title) in enumerate(POWERS):
        y_top = top + panel * (panel_height + gap)
        y_bottom = y_top + panel_height
        maximum = max(table[frequency][power] for table in tables for frequency in frequencies) * 1.08
        y_of = lambda value: y_bottom - value / maximum * panel_height
        svg.append(f'<text x="{left}" y="{y_top-12}" class="panel">{title} — path-corrected output, mW</text>')
        for part in range(6):
            value, y = maximum * part / 5, y_of(maximum * part / 5)
            svg.extend([f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d8dee9"/>', f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="axis">{value / 1000:.1f} W</text>'])
        for index, table in enumerate(tables):
            points = " ".join(f"{x_of(frequency):.1f},{y_of(table[frequency][power]):.1f}" for frequency in frequencies)
            svg.append(f'<polyline points="{points}" fill="none" stroke="{COLORS[index]}" stroke-width="2.3" stroke-linejoin="round"/>')
        if panel == 2:
            for frequency in range(((frequencies[0] + 199) // 200) * 200, frequencies[-1] + 1, 200):
                x = x_of(frequency)
                svg.extend([f'<line x1="{x:.1f}" y1="{y_top}" x2="{x:.1f}" y2="{y_bottom}" stroke="#edf0f4"/>', f'<text x="{x:.1f}" y="{y_bottom+23}" text-anchor="middle" class="axis">{frequency}</text>'])
    for index, label in enumerate(args.labels):
        x = left + index * 165
        svg.extend([f'<line x1="{x}" y1="{height-24}" x2="{x+25}" y2="{height-24}" stroke="{COLORS[index]}" stroke-width="3"/>', f'<text x="{x+32}" y="{height-19}" class="legend">{escape(label)}</text>'])
    svg.append('</svg>')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(svg), encoding="utf-8")
    print(f"Created: {args.output}")


if __name__ == "__main__":
    main()
