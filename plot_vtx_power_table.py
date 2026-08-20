#!/usr/bin/env python3
"""Draw absolute and nominal-relative SVG charts from a VTX power-table CSV."""

from __future__ import annotations

import argparse
import csv
import math
import re
from pathlib import Path
from xml.sax.saxutils import escape


FREQUENCY_RE = re.compile(r"^(.*?) \((\d+) MHz\)$")
SERIES = {25: "#2563eb", 2000: "#d97706", 4000: "#dc2626"}


def read_table(path: Path) -> list[tuple[int, str, dict[int, float]]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.reader(source, delimiter=";"))
    if len(rows) < 2 or rows[0][0] != "power_mw":
        raise ValueError("Expected a semicolon-delimited VTX power-table CSV.")
    columns: list[tuple[int, str]] = []
    for cell in rows[0][1:]:
        match = FREQUENCY_RE.match(cell)
        if not match:
            raise ValueError(f"Invalid channel column: {cell!r}")
        columns.append((int(match.group(2)), match.group(1)))
    values: list[dict[int, float]] = [{} for _ in columns]
    for row in rows[1:]:
        power_mw = int(row[0])
        for index, cell in enumerate(row[1:]):
            if cell:
                values[index][power_mw] = float(cell.replace(",", "."))
    return sorted((frequency, label, value) for (frequency, label), value in zip(columns, values))


def chart(table: list[tuple[int, str, dict[int, float]]], output: Path, *, relative: bool) -> None:
    width, height = 1440, 720
    left, right, top, bottom = 92, 36, 75, 90
    plot_width, plot_height = width - left - right, height - top - bottom
    frequencies = [point[0] for point in table]
    minimum_frequency, maximum_frequency = min(frequencies), max(frequencies)
    values = [value for _, _, measurements in table for value in measurements.values()]
    if relative:
        y_min, y_max = 0.0, max(350.0, math.ceil(max(value / power * 100 for _, _, m in table for power, value in m.items()) / 50) * 50)
        y_of = lambda value: top + plot_height * (1 - (value - y_min) / (y_max - y_min))
        ticks = list(range(0, int(y_max) + 1, 50))
        title, ylabel = "VTX output relative to selected power", "Output, % of nominal"
    else:
        y_min = math.floor(math.log10(min(values)))
        y_max = math.ceil(math.log10(max(values)))
        y_of = lambda value: top + plot_height * (1 - (math.log10(value) - y_min) / (y_max - y_min))
        ticks = [10**power for power in range(y_min, y_max + 1)]
        title, ylabel = "VTX output power by frequency", "Path-corrected output, mW (log scale)"
    x_of = lambda frequency: left + plot_width * (frequency - minimum_frequency) / (maximum_frequency - minimum_frequency)
    body = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="white"/>',
            '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#172033}.axis{font-size:13px}.title{font-size:23px;font-weight:700}.note{font-size:13px;fill:#526072}.legend{font-size:14px;font-weight:600}</style>',
            f'<text x="{left}" y="34" class="title">{title}</text>',
            f'<text x="{left}" y="56" class="note">{escape(output.stem.replace("-", " "))} · {len(table)} factory channel-points</text>']
    for tick in ticks:
        y = y_of(tick if not relative else float(tick))
        label = f"{tick:g}%" if relative else (f"{tick / 1000:g} W" if tick >= 1000 else f"{tick:g} mW")
        body.extend([f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d8dee9" stroke-width="1"/>',
                     f'<text x="{left-12}" y="{y+5:.1f}" text-anchor="end" class="axis">{label}</text>'])
    for frequency in range(int(math.ceil(minimum_frequency / 200) * 200), maximum_frequency + 1, 200):
        x = x_of(frequency)
        body.extend([f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height-bottom}" stroke="#edf0f4" stroke-width="1"/>',
                     f'<text x="{x:.1f}" y="{height-bottom+26}" text-anchor="middle" class="axis">{frequency}</text>'])
    if relative:
        y = y_of(100)
        body.extend([f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#374151" stroke-width="1.5" stroke-dasharray="6 5"/>',
                     f'<text x="{width-right}" y="{y-8:.1f}" text-anchor="end" class="note">100% nominal</text>'])
    for power, color in SERIES.items():
        points = []
        for frequency, _, measurements in table:
            if power in measurements:
                value = measurements[power] / power * 100 if relative else measurements[power]
                points.append(f"{x_of(frequency):.1f},{y_of(value):.1f}")
        body.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>')
        legend = "2 W" if power == 2000 else ("4 W" if power == 4000 else "25 mW")
        x = left + list(SERIES).index(power) * 108
        body.extend([f'<line x1="{x}" y1="{height-36}" x2="{x+22}" y2="{height-36}" stroke="{color}" stroke-width="3"/>',
                     f'<text x="{x+29}" y="{height-31}" class="legend">{legend}</text>'])
    body.extend([f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#526072"/>',
                 f'<text x="{left + plot_width / 2:.1f}" y="{height-18}" text-anchor="middle" class="axis">Frequency, MHz</text>',
                 f'<text x="20" y="{top + plot_height / 2:.1f}" transform="rotate(-90 20 {top + plot_height / 2:.1f})" text-anchor="middle" class="axis">{ylabel}</text>',
                 '</svg>'])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(body), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Draw SVG charts from vtx_power_table_measure.py CSV output.")
    parser.add_argument("csv", type=Path, help="Input vtx-power-table CSV")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"), help="Directory for SVG files (default: artifacts)")
    args = parser.parse_args()
    table = read_table(args.csv)
    stem = args.csv.stem
    absolute = args.output_dir / f"{stem}-absolute.svg"
    relative = args.output_dir / f"{stem}-relative.svg"
    chart(table, absolute, relative=False)
    chart(table, relative, relative=True)
    print(f"Created: {absolute}")
    print(f"Created: {relative}")


if __name__ == "__main__":
    main()
