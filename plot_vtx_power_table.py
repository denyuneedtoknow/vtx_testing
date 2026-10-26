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
CHANNEL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")
SERIES_COLORS = ("#2563eb", "#7c3aed", "#d97706", "#dc2626", "#0891b2", "#4d7c0f")
POWER_PANEL_COLORS = {
    25: "#dc2626",    # red
    500: "#ec4899",   # pink
    3000: "#111827",  # black
    5000: "#2563eb",  # blue
}


def format_power_label(power_mw: int) -> str:
    if power_mw >= 1000:
        watts = power_mw / 1000
        return f"{watts:g} W"
    return f"{power_mw} mW"


def nice_axis_step(maximum: float, target_ticks: int = 6) -> float:
    """Return a human-readable linear-axis increment for a positive maximum."""
    rough_step = maximum / target_ticks
    magnitude = 10 ** math.floor(math.log10(rough_step))
    for multiplier in (1, 2, 2.5, 5, 10):
        step = multiplier * magnitude
        if step >= rough_step:
            return step
    return 10 * magnitude


def read_table(path: Path, *, sort_by_frequency: bool = True) -> list[tuple[int, str, dict[int, float]]]:
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
    table = [(frequency, label, value) for (frequency, label), value in zip(columns, values)]
    return sorted(table) if sort_by_frequency else table


def band_order_chart(table: list[tuple[int, str, dict[int, float]]], output: Path) -> None:
    """Draw the table in its Betaflight band/channel order, not by frequency."""
    powers = sorted({power for _, _, measurements in table for power in measurements})
    width, height = 1800, 720
    left, right, top, bottom = 96, 36, 92, 112
    plot_width, plot_height = width - left - right, height - top - bottom
    # A full sweep normally has one output level. If more were measured, retain
    # all lines while using the same categorical band/channel X axis.
    values = [value for _, _, measurements in table for value in measurements.values()]
    value_min, value_max = min(values), max(values)
    span = max(value_max - value_min, max(value_max * 0.1, 1.0))
    step = nice_axis_step(span, target_ticks=6)
    y_min = math.floor((value_min - span * 0.12) / step) * step
    y_max = math.ceil((value_max + span * 0.12) / step) * step
    if y_min == y_max:
        y_max += step
    ticks = [y_min + step * index for index in range(int(round((y_max - y_min) / step)) + 1)]
    y_of = lambda value: top + plot_height * (1 - (value - y_min) / (y_max - y_min))
    x_of = lambda index: left + plot_width * index / max(len(table) - 1, 1)
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#172033}.axis{font-size:13px}.title{font-size:23px;font-weight:700}.note{font-size:13px;fill:#526072}.band{font-size:15px;font-weight:700}</style>',
        f'<text x="{left}" y="36" class="title">VTX output power by Betaflight band</text>',
        f'<text x="{left}" y="60" class="note">{escape(output.stem.replace("-", " "))} · channels are shown in the same order as the CSV / VTX table</text>',
    ]
    for tick in ticks:
        y = y_of(tick)
        body.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d8dee9" stroke-width="1"/>',
            f'<text x="{left-12}" y="{y+5:.1f}" text-anchor="end" class="axis">{format_power_label(int(round(tick)))}</text>',
        ])
    # Mark the requested output independently of the measured trace. This
    # makes it possible to count compliant points without interpreting a
    # connecting line as additional data.
    nominal_power = max(powers)
    if y_min <= nominal_power <= y_max:
        y = y_of(nominal_power)
        body.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#15803d" stroke-width="2.5" stroke-dasharray="9 6"/>',
            f'<text x="{width-right}" y="{y-9:.1f}" text-anchor="end" class="note">{format_power_label(nominal_power)} target</text>',
        ])
    bands: list[tuple[str, int, int]] = []
    start = 0
    current_band = None
    for index, (_, label, _) in enumerate(table):
        match = CHANNEL_RE.match(label)
        band = match.group(1) if match else label
        if current_band is None:
            current_band = band
        elif band != current_band:
            bands.append((current_band, start, index - 1))
            start, current_band = index, band
    if current_band is not None:
        bands.append((current_band, start, len(table) - 1))
    for band, first, last in bands:
        center = (x_of(first) + x_of(last)) / 2
        body.extend([
            f'<text x="{center:.1f}" y="{height-bottom+29}" text-anchor="middle" class="band">Band {escape(band)}</text>',
            f'<text x="{center:.1f}" y="{height-bottom+49}" text-anchor="middle" class="note">channels 1–{last-first+1}</text>',
        ])
        if last < len(table) - 1:
            divider = (x_of(last) + x_of(last + 1)) / 2
            body.append(f'<line x1="{divider:.1f}" y1="{top}" x2="{divider:.1f}" y2="{height-bottom}" stroke="#98a4b5" stroke-width="1.3" stroke-dasharray="5 5"/>')
    for index, power in enumerate(powers):
        color = POWER_PANEL_COLORS.get(power, SERIES_COLORS[index % len(SERIES_COLORS)])
        body.extend(
            f'<circle cx="{x_of(channel_index):.1f}" cy="{y_of(measurements[power]):.1f}" r="4.2" fill="{color}" stroke="white" stroke-width="1.2"><title>{escape(label)}: {measurements[power]:.0f} mW</title></circle>'
            for channel_index, (_, label, measurements) in enumerate(table) if power in measurements
        )
    body.extend([
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#526072"/>',
        f'<text x="{left + plot_width / 2:.1f}" y="{height-18}" text-anchor="middle" class="axis">Betaflight VTX table band / channel order</text>',
        f'<text x="20" y="{top + plot_height / 2:.1f}" transform="rotate(-90 20 {top + plot_height / 2:.1f})" text-anchor="middle" class="axis">Path-corrected output</text>',
        '</svg>',
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(body), encoding="utf-8")


def compliance_scatter_chart(table: list[tuple[int, str, dict[int, float]]], output: Path) -> None:
    """Show how many frequency points meet the selected output without a misleading line."""
    powers = sorted({power for _, _, measurements in table for power in measurements})
    if len(powers) != 1:
        raise ValueError("Compliance scatter chart expects exactly one measured power level.")
    nominal = powers[0]
    points = [(frequency, label, measurements[nominal]) for frequency, label, measurements in table]
    width, height = 1440, 760
    left, right, top, bottom = 94, 36, 138, 92
    plot_width, plot_height = width - left - right, height - top - bottom
    frequencies = [frequency for frequency, _, _ in points]
    minimum_frequency, maximum_frequency = min(frequencies), max(frequencies)
    minimum_value = min(value for _, _, value in points)
    y_min = max(0.0, math.floor((minimum_value - nominal * 0.12) / 500) * 500)
    y_max = math.ceil(max(value for _, _, value in points) / 500) * 500
    y_max = max(y_max, nominal + 500)
    x_of = lambda frequency: left + plot_width * (frequency - minimum_frequency) / (maximum_frequency - minimum_frequency)
    y_of = lambda value: top + plot_height * (1 - (value - y_min) / (y_max - y_min))
    reaches_nominal = [point for point in points if point[2] >= nominal]
    between_four_and_nominal = [point for point in points if nominal * 0.8 <= point[2] < nominal]
    below_four = [point for point in points if point[2] < nominal * 0.8]
    total = len(points)
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#172033}.axis{font-size:13px}.title{font-size:23px;font-weight:700}.note{font-size:13px;fill:#526072}.metric{font-size:19px;font-weight:700}.metricnote{font-size:13px;fill:#526072}</style>',
        f'<text x="{left}" y="36" class="title">VTX 5 W output compliance by frequency</text>',
        f'<text x="{left}" y="60" class="note">Each dot is one channel; dots are intentionally not connected by a line.</text>',
    ]
    metrics = [
        ("#15803d", f"≥ {format_power_label(nominal)}", len(reaches_nominal), "reaches selected output"),
        ("#ca8a04", f"4–{nominal // 1000:g} W", len(between_four_and_nominal), "below 5 W, but ≥ 4 W"),
        ("#dc2626", "< 4 W", len(below_four), "below 80% of selected output"),
    ]
    metric_x = left
    for color, label, count, detail in metrics:
        body.extend([
            f'<circle cx="{metric_x+8}" cy="94" r="7" fill="{color}"/>',
            f'<text x="{metric_x+24}" y="91" class="metric">{count}/{total} · {escape(label)}</text>',
            f'<text x="{metric_x+24}" y="112" class="metricnote">{escape(detail)}</text>',
        ])
        metric_x += 300
    # Background makes the three groups immediately countable without relying
    # on line geometry.
    regions = [
        (y_min, nominal * 0.8, "#fee2e2"),
        (nominal * 0.8, nominal, "#fef3c7"),
        (nominal, y_max, "#dcfce7"),
    ]
    for lower, upper, color in regions:
        if upper > lower:
            y_top, y_bottom = y_of(upper), y_of(lower)
            body.append(f'<rect x="{left}" y="{y_top:.1f}" width="{plot_width}" height="{y_bottom-y_top:.1f}" fill="{color}"/>')
    for tick in range(int(y_min), int(y_max) + 1, 500):
        y = y_of(float(tick))
        body.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#cbd5e1" stroke-width="1"/>',
            f'<text x="{left-12}" y="{y+5:.1f}" text-anchor="end" class="axis">{format_power_label(tick)}</text>',
        ])
    for frequency in range(int(math.ceil(minimum_frequency / 200) * 200), maximum_frequency + 1, 200):
        x = x_of(frequency)
        body.extend([
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height-bottom}" stroke="#e5e7eb" stroke-width="1"/>',
            f'<text x="{x:.1f}" y="{height-bottom+26}" text-anchor="middle" class="axis">{frequency}</text>',
        ])
    for threshold, label, color in ((nominal, "5 W target", "#15803d"), (nominal * 0.8, "4 W / 80%", "#ca8a04")):
        y = y_of(threshold)
        body.extend([
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="{color}" stroke-width="2" stroke-dasharray="7 5"/>',
            f'<text x="{width-right}" y="{y-8:.1f}" text-anchor="end" class="note">{label}</text>',
        ])
    for frequency, label, value in points:
        color = "#15803d" if value >= nominal else ("#ca8a04" if value >= nominal * 0.8 else "#dc2626")
        body.append(f'<circle cx="{x_of(frequency):.1f}" cy="{y_of(value):.1f}" r="5" fill="{color}" stroke="white" stroke-width="1.4"><title>{escape(label)} ({frequency} MHz): {value:.0f} mW</title></circle>')
    body.extend([
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#526072"/>',
        f'<text x="{left + plot_width / 2:.1f}" y="{height-18}" text-anchor="middle" class="axis">Frequency, MHz</text>',
        f'<text x="20" y="{top + plot_height / 2:.1f}" transform="rotate(-90 20 {top + plot_height / 2:.1f})" text-anchor="middle" class="axis">Path-corrected output</text>',
        '</svg>',
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(body), encoding="utf-8")


def power_panels_chart(table: list[tuple[int, str, dict[int, float]]], output: Path) -> None:
    """Draw one independently-scaled panel for every selected output level."""
    powers = sorted({power for _, _, measurements in table for power in measurements})
    width, panel_height = 1440, 250
    left, right, top, bottom = 112, 36, 96, 62
    height = top + panel_height * len(powers) + bottom
    plot_width = width - left - right
    frequencies = [point[0] for point in table]
    minimum_frequency, maximum_frequency = min(frequencies), max(frequencies)
    x_of = lambda frequency: left + plot_width * (frequency - minimum_frequency) / (maximum_frequency - minimum_frequency)
    body = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#172033}.axis{font-size:13px}.title{font-size:23px;font-weight:700}.note{font-size:13px;fill:#526072}.panel{font-size:16px;font-weight:700}.value{font-size:12px;fill:#526072}</style>',
        f'<text x="{left}" y="36" class="title">VTX output power by frequency — separate power levels</text>',
        f'<text x="{left}" y="60" class="note">{escape(output.stem.replace("-", " "))} · independent linear Y axis for each power level</text>',
    ]
    for index, power in enumerate(powers):
        panel_top = top + index * panel_height
        panel_bottom = panel_top + panel_height - 56
        plot_height = panel_bottom - panel_top
        points_data = [(frequency, measurements[power]) for frequency, _, measurements in table if power in measurements]
        values = [value for _, value in points_data]
        value_min, value_max = min(values), max(values)
        span = max(value_max - value_min, max(value_max * 0.1, 1.0))
        step = nice_axis_step(span, target_ticks=5)
        y_min = math.floor((value_min - span * 0.12) / step) * step
        y_max = math.ceil((value_max + span * 0.12) / step) * step
        if y_min == y_max:
            y_max += step
        ticks = [y_min + step * tick for tick in range(int(round((y_max - y_min) / step)) + 1)]
        y_of = lambda value: panel_top + plot_height * (1 - (value - y_min) / (y_max - y_min))
        color = POWER_PANEL_COLORS.get(power, SERIES_COLORS[index % len(SERIES_COLORS)])
        body.extend([
            f'<text x="{left}" y="{panel_top-12}" class="panel" fill="{color}">{format_power_label(power)} selected output</text>',
            f'<text x="{width-right}" y="{panel_top-12}" text-anchor="end" class="value">range: {format_power_label(int(round(value_min)))} – {format_power_label(int(round(value_max)))}</text>',
        ])
        for tick in ticks:
            y = y_of(tick)
            body.extend([
                f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#d8dee9" stroke-width="1"/>',
                f'<text x="{left-12}" y="{y+5:.1f}" text-anchor="end" class="axis">{format_power_label(int(round(tick)))}</text>',
            ])
        for frequency in range(int(math.ceil(minimum_frequency / 200) * 200), maximum_frequency + 1, 200):
            x = x_of(frequency)
            body.append(f'<line x1="{x:.1f}" y1="{panel_top}" x2="{x:.1f}" y2="{panel_bottom}" stroke="#edf0f4" stroke-width="1"/>')
        points = " ".join(f"{x_of(frequency):.1f},{y_of(value):.1f}" for frequency, value in points_data)
        body.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
        body.extend(f'<circle cx="{x_of(frequency):.1f}" cy="{y_of(value):.1f}" r="2.4" fill="{color}"/>' for frequency, value in points_data)
        body.append(f'<line x1="{left}" y1="{panel_bottom}" x2="{width-right}" y2="{panel_bottom}" stroke="#526072"/>')
        if index == len(powers) - 1:
            for frequency in range(int(math.ceil(minimum_frequency / 200) * 200), maximum_frequency + 1, 200):
                x = x_of(frequency)
                body.append(f'<text x="{x:.1f}" y="{panel_bottom+25}" text-anchor="middle" class="axis">{frequency}</text>')
    body.extend([
        f'<text x="{left + plot_width / 2:.1f}" y="{height-18}" text-anchor="middle" class="axis">Frequency, MHz</text>',
        '</svg>',
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(body), encoding="utf-8")


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
        # The absolute chart is intended for a quick comparison of measured
        # power.  A linear scale with regular, labelled mW/W grid lines makes
        # the actual level of every trace immediately visible.
        y_min = 0.0
        step = nice_axis_step(max(values))
        y_max = math.ceil(max(values) / step) * step
        y_of = lambda value: top + plot_height * (1 - value / y_max)
        ticks = [step * index for index in range(int(round(y_max / step)) + 1)]
        title, ylabel = "VTX output power by frequency", "Path-corrected output"
    x_of = lambda frequency: left + plot_width * (frequency - minimum_frequency) / (maximum_frequency - minimum_frequency)
    body = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="white"/>',
            '<style>text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;fill:#172033}.axis{font-size:13px}.title{font-size:23px;font-weight:700}.note{font-size:13px;fill:#526072}.legend{font-size:14px;font-weight:600}</style>',
            f'<text x="{left}" y="34" class="title">{title}</text>',
            f'<text x="{left}" y="56" class="note">{escape(output.stem.replace("-", " "))} · {len(table)} factory channel-points</text>']
    for tick in ticks:
        y = y_of(tick if not relative else float(tick))
        label = f"{tick:g}%" if relative else format_power_label(int(tick))
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
    powers = sorted({power for _, _, measurements in table for power in measurements})
    for index, power in enumerate(powers):
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        points = []
        for frequency, _, measurements in table:
            if power in measurements:
                value = measurements[power] / power * 100 if relative else measurements[power]
                points.append(f"{x_of(frequency):.1f},{y_of(value):.1f}")
        body.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>')
        legend = format_power_label(power)
        x = left + index * 108
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
    table_in_band_order = read_table(args.csv, sort_by_frequency=False)
    stem = args.csv.stem
    absolute = args.output_dir / f"{stem}-absolute.svg"
    relative = args.output_dir / f"{stem}-relative.svg"
    by_power = args.output_dir / f"{stem}-by-power.svg"
    by_band = args.output_dir / f"{stem}-by-band.svg"
    compliance = args.output_dir / f"{stem}-compliance.svg"
    chart(table, absolute, relative=False)
    chart(table, relative, relative=True)
    power_panels_chart(table, by_power)
    band_order_chart(table_in_band_order, by_band)
    if len({power for _, _, measurements in table for power in measurements}) == 1:
        compliance_scatter_chart(table, compliance)
    print(f"Created: {absolute}")
    print(f"Created: {relative}")
    print(f"Created: {by_power}")
    print(f"Created: {by_band}")
    if compliance.is_file():
        print(f"Created: {compliance}")


if __name__ == "__main__":
    main()
