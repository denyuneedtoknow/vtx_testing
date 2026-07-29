#!/usr/bin/env python3
"""Run the FT VTX 3.3 three-point 4 W test and return the VTX to 25 mW.

The test changes VTX settings through Betaflight MSP. Close Betaflight
Configurator first. An attenuator must be installed between the VTX and NRP-Z11.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any

from measurement_path import load_measurement_path, offset_db_at_frequency
from vtx_a1_measure import build_set_vtx_config_payload, format_vtx
from vtx_msp_probe import (
    MSP_SET_VTX_CONFIG,
    MSP_VTX_CONFIG,
    MspClient,
    MspError,
    open_serial,
    parse_vtx_config,
    read_vtx_bands,
    read_vtx_power_levels,
    resolve_port,
)
from vtx_profiles import load_profile
from vtx_power_logger import NrpzError, NrpzSensor, watts_to_millwatts


PLAN_DIRECTORY = Path(__file__).with_name("configs") / "test_plans"
VTX_PLAN_FILENAMES = {
    "1.2": "ft_vtx_1_2_three_point_4w.json",
    "2.7": "ft_vtx_2_7_three_point_4w.json",
    "3.3": "ft_vtx_3_3_three_point_4w.json",
    "5.8": "ft_vtx_5_8_three_point_4w.json",
    "7.2": "ft_vtx_7_2_three_point_5w.json",
}
DEFAULT_REFERENCE_THRESHOLDS_PERCENT = {"green_min": 90.0, "yellow_min": 80.0}
ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
REFERENCE_GRAPH_WIDTH = 28
REFERENCE_GRAPH_MAX_PERCENT = 125.0


def plan_for_vtx(vtx: str) -> Path:
    return PLAN_DIRECTORY / VTX_PLAN_FILENAMES[vtx]


def select_plan() -> Path:
    plans = []
    for path in sorted(PLAN_DIRECTORY.glob("*three_point*.json")):
        try:
            plans.append((path, json.loads(path.read_text(encoding="utf-8"))["name"]))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    if not plans:
        raise ValueError(f"No three-point test plans found in {PLAN_DIRECTORY}.")
    print("Select VTX test:")
    for index, (_, name) in enumerate(plans, start=1):
        print(f"  {index}. {name}")
    while True:
        answer = input(f"Test [1-{len(plans)}]: ").strip()
        try:
            selected = int(answer)
        except ValueError:
            selected = 0
        if 1 <= selected <= len(plans):
            return plans[selected - 1][0]
        print("Enter a number from the list.")


def load_plan(plan_path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        points = plan["points"]
        test_power = plan["test_power"]
        safe_power = plan["safe_power_after_test"]
        profile_path = Path(plan["vtx_profile"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read test plan {plan_path}: {exc}") from exc
    if not isinstance(points, list) or not points:
        raise ValueError("Test plan must contain at least one point.")
    if not isinstance(test_power.get("index"), int) or not isinstance(safe_power.get("index"), int):
        raise ValueError("Test plan power indices must be integers.")
    thresholds = plan.get("reference_thresholds_percent", DEFAULT_REFERENCE_THRESHOLDS_PERCENT)
    if not isinstance(thresholds, dict):
        raise ValueError("reference_thresholds_percent must be an object.")
    try:
        green_min = float(thresholds["green_min"])
        yellow_min = float(thresholds["yellow_min"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("reference_thresholds_percent must contain numeric green_min and yellow_min values.") from exc
    if not 0 <= yellow_min <= green_min <= 100:
        raise ValueError("reference thresholds must satisfy 0 <= yellow_min <= green_min <= 100.")
    profile = load_profile(profile_path if profile_path.is_absolute() else plan_path.parent / profile_path)
    for point in points:
        band = point.get("band")
        channel = point.get("channel")
        frequency_mhz = point.get("frequency_mhz")
        if not isinstance(band, str) or not isinstance(channel, int) or not isinstance(frequency_mhz, int):
            raise ValueError(f"Invalid test point: {point!r}")
        if band not in profile["bands"] or not 1 <= channel <= len(profile["bands"][band]):
            raise ValueError(f"Test point does not exist in VTX profile: {point!r}")
        if profile["bands"][band][channel - 1] != frequency_mhz:
            raise ValueError(f"Test point frequency does not match VTX profile: {point!r}")
    plan["_profile"] = profile
    plan["_reference_thresholds_percent"] = {"green_min": green_min, "yellow_min": yellow_min}
    return plan


def switch_and_confirm(
    client: MspClient,
    current: dict[str, int | bool | None],
    *,
    band: int,
    channel: int,
    frequency_mhz: int,
    power_index: int,
    settle_time_s: float,
    direct_frequency: bool = False,
) -> dict[str, int | bool | None]:
    payload = build_set_vtx_config_payload(
        current, band, channel, frequency_mhz, power_index, direct_frequency=direct_frequency
    )
    client.request(MSP_SET_VTX_CONFIG, payload)
    time.sleep(settle_time_s)
    updated = parse_vtx_config(client.request(MSP_VTX_CONFIG))
    expected = (0, 0, frequency_mhz, power_index) if direct_frequency else (band, channel, frequency_mhz, power_index)
    actual = (updated["band"], updated["channel"], updated["frequency_mhz"], updated["power_index"])
    if actual != expected or not updated["ready"]:
        raise MspError(f"VTX did not confirm requested settings. Expected {expected}; got {actual}; ready={updated['ready']}.")
    return updated


def verify_vtx_table(
    client: MspClient,
    current: dict[str, int | bool | None],
    profile: dict[str, Any],
) -> None:
    """Read and verify the FC VTX table without changing persistent configuration."""
    bands = profile.get("bands")
    power_levels = profile.get("power_levels_mw")
    if not isinstance(bands, dict) or not isinstance(power_levels, list):
        raise ValueError("VTX profile is missing bands or power_levels_mw.")
    band_items = list(bands.items())
    if not 1 <= len(band_items) <= 8 or not 1 <= len(power_levels) <= 8:
        raise ValueError("VTX table must contain 1..8 bands and power levels.")
    if not all(isinstance(value, int) for value in power_levels):
        raise ValueError("VTX power levels must be integers.")
    channel_count = max(len(frequencies) for _, frequencies in band_items)
    if channel_count > 8 or any(len(frequencies) != channel_count for _, frequencies in band_items):
        raise ValueError("Every VTX table band must contain the same 1..8 channel count.")

    expected_counts = (len(band_items), channel_count, len(power_levels))
    actual_counts = (
        current.get("vtx_table_bands"),
        current.get("vtx_table_channels"),
        current.get("vtx_table_power_levels"),
    )
    if actual_counts != expected_counts:
        raise MspError(
            "Flight Controller VTX table does not match the selected VTX profile: "
            f"expected {expected_counts[0]} bands, {expected_counts[1]} channels and "
            f"{expected_counts[2]} power levels; got {actual_counts}. FC settings were not changed."
        )

    actual_bands = read_vtx_bands(client, len(band_items))
    for index, ((expected_letter, expected_frequencies), actual_band) in enumerate(
        zip(band_items, actual_bands), start=1
    ):
        actual_frequencies = actual_band["frequencies_mhz"]
        if actual_frequencies != expected_frequencies:
            raise MspError(
                f"Flight Controller VTX band {index} does not match the selected profile: "
                f"expected {expected_letter} {expected_frequencies}; got "
                f"{actual_band['letter'] or actual_band['name']} {actual_frequencies}. FC settings were not changed."
            )

    actual_power_levels = read_vtx_power_levels(client, len(power_levels))
    actual_power_values = [int(level["value"]) for level in actual_power_levels]
    power_levels_match = all(
        actual_value == expected_value
        or (
            expected_value == 0
            and str(actual_level["label"]).strip().upper() in {"0", "OFF", "PIT"}
        )
        for expected_value, actual_value, actual_level in zip(power_levels, actual_power_values, actual_power_levels)
    )
    if not power_levels_match:
        raise MspError(
            "Flight Controller VTX power levels do not match the selected profile: "
            f"expected {power_levels}; got {actual_power_values}. FC settings were not changed."
        )


def observe_power(sensor: NrpzSensor, timeout_ms: int, duration_s: float, interval_s: float) -> list[tuple[float, float]]:
    """Collect readings for a fixed observation interval as (elapsed seconds, mW)."""
    if duration_s <= 0 or interval_s <= 0:
        raise ValueError("observation time and sample interval must be positive.")
    started = time.monotonic()
    deadline = started + duration_s
    readings: list[tuple[float, float]] = []
    while True:
        reading_started = time.monotonic()
        milliwatts = watts_to_millwatts(sensor.read_watts(timeout_ms))
        readings.append((reading_started - started, milliwatts))
        now = time.monotonic()
        if now >= deadline:
            return readings
        time.sleep(min(max(0, interval_s - (now - reading_started)), deadline - now))


def wait_for_stable_power(
    sensor: NrpzSensor,
    *,
    timeout_ms: int,
    sample_interval_s: float,
    required_samples: int,
    tolerance_percent: float,
    max_wait_s: float,
) -> list[float]:
    """Wait until consecutive readings have a bounded peak-to-peak spread."""
    if required_samples < 2 or tolerance_percent <= 0 or max_wait_s <= 0 or sample_interval_s < 0:
        raise ValueError("Invalid stability settings.")
    deadline = time.monotonic() + max_wait_s
    readings: list[float] = []
    while True:
        reading_mw = watts_to_millwatts(sensor.read_watts(timeout_ms))
        readings.append(reading_mw)
        recent = readings[-required_samples:]
        if len(recent) == required_samples:
            mean_mw = fmean(recent)
            spread_percent = (max(recent) - min(recent)) / mean_mw * 100 if mean_mw else float("inf")
            if spread_percent <= tolerance_percent:
                return recent
        if time.monotonic() >= deadline:
            latest = readings[-required_samples:]
            if latest:
                mean_mw = fmean(latest)
                spread_percent = (max(latest) - min(latest)) / mean_mw * 100 if mean_mw else float("inf")
                raise MspError(
                    f"VTX output did not stabilize within {max_wait_s:g} s: "
                    f"last {len(latest)} readings span {spread_percent:.1f}% (limit {tolerance_percent:.1f}%)."
                )
            raise MspError("VTX output did not stabilize.")
        if sample_interval_s:
            time.sleep(sample_interval_s)


def summarize_readings(readings: list[tuple[float, float]]) -> dict[str, float | int]:
    if not readings:
        raise ValueError("Cannot summarize zero readings.")
    values = [value for _, value in readings]
    first = values[0]
    last = values[-1]
    return {
        "count": len(values),
        "average_mw": fmean(values),
        "minimum_mw": min(values),
        "maximum_mw": max(values),
        "change_mw": last - first,
        "change_percent": ((last - first) / first * 100) if first else 0.0,
    }


def power_color(
    measured_mw: float,
    reference_mw: float,
    thresholds_percent: dict[str, float],
) -> str:
    """Return the ANSI color for a measurement relative to its reference."""
    percentage = measured_mw / reference_mw * 100 if reference_mw > 0 else 0.0
    if percentage >= thresholds_percent["green_min"]:
        return ANSI_GREEN
    if percentage >= thresholds_percent["yellow_min"]:
        return ANSI_YELLOW
    return ANSI_RED


def power_status(
    measured_mw: float,
    reference_mw: float,
    thresholds_percent: dict[str, float],
) -> str:
    """Return the readable result label for a reference comparison."""
    percentage = measured_mw / reference_mw * 100 if reference_mw > 0 else 0.0
    if percentage >= thresholds_percent["green_min"]:
        return "OK"
    if percentage >= thresholds_percent["yellow_min"]:
        return "LOW"
    return "FAIL"


def format_power(
    measured_mw: float,
    reference_mw: float | None,
    thresholds_percent: dict[str, float],
    *,
    use_color: bool,
) -> str:
    """Format one power result, optionally colored by its reference percentage."""
    text = f"{measured_mw:.0f} mW"
    if not use_color or reference_mw is None:
        return text
    return f"{power_color(measured_mw, reference_mw, thresholds_percent)}{text}{ANSI_RESET}"


def reference_chart_line(
    point: dict[str, Any],
    average_mw: float,
    thresholds_percent: dict[str, float],
    *,
    use_color: bool,
) -> str:
    """Build a compact terminal chart; its vertical marker is the reference."""
    reference_mw = point.get("reference_output_mw")
    label = f"{point['band']}{point['channel']} {point['frequency_mhz']} MHz"
    if not isinstance(reference_mw, (int, float)) or reference_mw <= 0:
        return f"  {label:<12}  {average_mw:.0f} mW (reference unavailable)"

    percentage = average_mw / reference_mw * 100
    fill_count = round(min(max(percentage, 0.0), REFERENCE_GRAPH_MAX_PERCENT) / REFERENCE_GRAPH_MAX_PERCENT * REFERENCE_GRAPH_WIDTH)
    marker_index = round(100 / REFERENCE_GRAPH_MAX_PERCENT * (REFERENCE_GRAPH_WIDTH - 1))
    cells = ["█" if index < fill_count else "░" for index in range(REFERENCE_GRAPH_WIDTH)]
    cells[marker_index] = "│"
    bar = "".join(cells)
    if use_color:
        bar = f"{power_color(average_mw, reference_mw, thresholds_percent)}{bar}{ANSI_RESET}"
    return (
        f"  {label:<12} [{bar}] {average_mw:.0f} / {reference_mw:.0f} mW "
        f"({percentage:.0f}%) {power_status(average_mw, reference_mw, thresholds_percent)}"
    )


def print_reference_chart(
    results: list[tuple[dict[str, Any], dict[str, float | int]]],
    thresholds_percent: dict[str, float],
    *,
    use_color: bool,
) -> None:
    """Print all completed points against their respective reference values."""
    if not results:
        return
    print("\nPower vs reference (│ = 100% reference):")
    for point, summary in results:
        print(reference_chart_line(point, float(summary["average_mw"]), thresholds_percent, use_color=use_color))


def select_manual_point(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Let the operator choose a configured point, or finish the manual session."""
    print("\nSelect VTX mode:")
    for index, point in enumerate(points, start=1):
        print(f"  {index}. {point['band']}{point['channel']} ({point['frequency_mhz']} MHz)")
    print("  0. Finish and return VTX to 25 mW")
    while True:
        answer = input(f"Mode [0-{len(points)}]: ").strip()
        try:
            selected = int(answer)
        except ValueError:
            selected = -1
        if selected == 0:
            return None
        if 1 <= selected <= len(points):
            return points[selected - 1]
        print("Enter a number from the list.")


def create_csv_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(file)
    writer.writerow([
        "timestamp_utc", "frequency_mhz", "band", "channel", "power_index", "sample_elapsed_s",
        "level_offset_db", "vtx_output_mw"
    ])
    return file, writer


def measure_and_report_point(
    sensor: NrpzSensor,
    point: dict[str, Any],
    *,
    power_index: int,
    timeout_ms: int,
    observation_time_s: float,
    sample_interval_s: float,
    averages: int,
    csv_writer,
    thresholds_percent: dict[str, float],
    use_color: bool,
    level_offset_db: float = 0.0,
) -> dict[str, float | int]:
    """Measure one active VTX point, write samples, and display its summary."""
    sensor.configure(point["frequency_mhz"] * 1_000_000, averages)
    readings = observe_power(sensor, timeout_ms, observation_time_s, sample_interval_s)
    for sample_elapsed_s, output_mw in readings:
        csv_writer.writerow([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            point["frequency_mhz"], point["band"], point["channel"], power_index,
            f"{sample_elapsed_s:.3f}", f"{level_offset_db:.6f}", f"{output_mw:.6f}",
        ])
    summary = summarize_readings(readings)
    reference_output_mw = point.get("reference_output_mw")
    reference_text = (
        f" (reference output: {reference_output_mw:.0f} mW)"
        if isinstance(reference_output_mw, (int, float)) else ""
    )
    print(
        "VTX output (path-corrected): "
        f"avg {format_power(summary['average_mw'], reference_output_mw, thresholds_percent, use_color=use_color)}, "
        f"min {format_power(summary['minimum_mw'], reference_output_mw, thresholds_percent, use_color=use_color)}, "
        f"max {format_power(summary['maximum_mw'], reference_output_mw, thresholds_percent, use_color=use_color)}, "
        f"change {summary['change_mw']:+.0f} mW ({summary['change_percent']:+.1f}%) "
        f"over {summary['count']} samples{reference_text}"
    )
    print(reference_chart_line(point, float(summary["average_mw"]), thresholds_percent, use_color=use_color))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an automated measurement or manually switch configured VTX test points.")
    parser.add_argument(
        "--port",
        help="Flight Controller COM port, for example COM83. Omit to auto-detect the first MSP-capable FC.",
    )
    plan_source = parser.add_mutually_exclusive_group()
    plan_source.add_argument("--vtx", choices=sorted(VTX_PLAN_FILENAMES), help="VTX band to test: 1.2, 2.7, 3.3, 5.8 or 7.2 GHz.")
    plan_source.add_argument("--plan", type=Path, help="Three-point test plan JSON. Omit to select interactively.")
    parser.add_argument("--baud", type=int, default=115200, help="MSP baud rate (default: 115200).")
    parser.add_argument("--timeout", type=float, default=2.0, help="MSP response timeout in seconds (default: 2).")
    parser.add_argument("--averages", type=int, default=16, help="NRP-Z11 manual averages (default: 16).")
    parser.add_argument("--timeout-ms", type=int, default=5_000, help="NRP-Z11 measurement timeout in ms (default: 5000).")
    parser.add_argument("--stability-samples", type=int, default=3, help="Consecutive readings required before a point is considered stable (default: 3).")
    parser.add_argument("--stability-tolerance-percent", type=float, default=2.0, help="Maximum peak-to-peak spread for stable readings (default: 2).")
    parser.add_argument("--stability-timeout-s", type=float, default=15.0, help="Maximum time to wait for output stabilization (default: 15).")
    parser.add_argument("--resource", default="*", help="NRP-Z11 VISA resource, or * for the first sensor.")
    parser.add_argument("--dll", help="Path to rsnrpz_64.dll.")
    parser.add_argument("--csv", type=Path, help="Write all samples to this CSV file. Default: logs/<timestamp>.csv.")
    parser.add_argument("--no-color", action="store_true", help="Disable green/yellow/red reference indicators.")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Manually select the plan's points, measuring each selection with NRP-Z11; returns VTX to 25 mW on exit.",
    )
    args = parser.parse_args()
    if (args.baud <= 0 or args.timeout <= 0 or args.averages <= 0 or args.timeout_ms <= 0
            or args.stability_samples < 2 or args.stability_tolerance_percent <= 0 or args.stability_timeout_s <= 0):
        parser.error("baud, timeout, averages, timeout-ms, stability-samples, stability-tolerance-percent and stability-timeout-s must be positive; stability-samples must be at least 2.")
    return args


def main() -> int:
    args = parse_args()
    sensor: NrpzSensor | None = None
    csv_file = None
    csv_writer = None
    safe_reset_error: Exception | None = None
    last_requested_frequency: int | None = None
    try:
        port = resolve_port(args.port, args.baud, args.timeout)
        plan_path = plan_for_vtx(args.vtx) if args.vtx else args.plan or select_plan()
        plan = load_plan(plan_path)
        profile = plan["_profile"]
        measurement_path_value = plan.get("measurement_path_profile")
        if not isinstance(measurement_path_value, str):
            raise ValueError("Test plan is missing measurement_path_profile.")
        measurement_path = load_measurement_path(plan_path.parent / measurement_path_value)
        settle_time_s = float(plan["settle_time_s"])
        observation_time_s = float(plan["observation_time_s"])
        sample_interval_s = float(plan["sample_interval_s"])
        if settle_time_s < 0 or observation_time_s <= 0 or sample_interval_s <= 0:
            raise ValueError("settle_time_s cannot be negative; observation_time_s and sample_interval_s must be positive.")
        test_power = plan["test_power"]
        safe_power = plan["safe_power_after_test"]
        thresholds_percent = plan["_reference_thresholds_percent"]
        use_color = not args.no_color and sys.stdout.isatty()
        print(f"Plan: {plan['name']}")
        print(f"{test_power['nominal_mw']} mW points: {', '.join(str(point['frequency_mhz']) for point in plan['points'])} MHz")
        if not args.manual:
            print(f"Observation per point: {observation_time_s:g} s, every {sample_interval_s:g} s")
        print(f"The VTX will be returned to {safe_power['nominal_mw']} mW at the end.")
        with open_serial(port, args.baud, args.timeout) as serial_port:
            client = MspClient(serial_port, args.timeout)
            current = parse_vtx_config(client.request(MSP_VTX_CONFIG))
            print(f"Initial VTX: {format_vtx(current)}")
            print("Verifying the Flight Controller VTX table (read-only)...")
            verify_vtx_table(client, current, profile)
            print(
                f"VTX table matches: {current['vtx_table_bands']} bands, "
                f"{current['vtx_table_channels']} channels, "
                f"{current['vtx_table_power_levels']} power levels. Persistent FC configuration unchanged."
            )

            try:
                if args.manual:
                    csv_path = args.csv or Path("logs") / f"vtx-manual-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
                    csv_file, csv_writer = create_csv_writer(csv_path)
                    print(f"Sample log: {csv_path}")
                    sensor = NrpzSensor(args.dll)
                    sensor.open(args.resource)
                    while True:
                        point = select_manual_point(plan["points"])
                        if point is None:
                            print("Manual switching finished.")
                            break
                        band = list(profile["bands"]).index(point["band"]) + 1
                        last_requested_frequency = point["frequency_mhz"]
                        print(
                            f"\nSetting {test_power['nominal_mw']} mW, "
                            f"{point['band']}{point['channel']} ({point['frequency_mhz']} MHz)..."
                        )
                        current = switch_and_confirm(
                            client,
                            current,
                            band=band,
                            channel=point["channel"],
                            frequency_mhz=point["frequency_mhz"],
                            power_index=test_power["index"],
                            settle_time_s=settle_time_s,
                        )
                        print(f"Active VTX: {format_vtx(current)}")
                        level_offset_db = offset_db_at_frequency(measurement_path, point["frequency_mhz"])
                        sensor.set_level_offset(level_offset_db)
                        stable_readings = wait_for_stable_power(
                            sensor, timeout_ms=args.timeout_ms, sample_interval_s=sample_interval_s,
                            required_samples=args.stability_samples,
                            tolerance_percent=args.stability_tolerance_percent, max_wait_s=args.stability_timeout_s,
                        )
                        print(f"VTX output stable: {fmean(stable_readings):.0f} mW over {len(stable_readings)} readings.")
                        measure_and_report_point(
                            sensor,
                            point,
                            power_index=test_power["index"],
                            timeout_ms=args.timeout_ms,
                            observation_time_s=observation_time_s,
                            sample_interval_s=sample_interval_s,
                            averages=args.averages,
                            csv_writer=csv_writer,
                            thresholds_percent=thresholds_percent,
                            use_color=use_color,
                            level_offset_db=level_offset_db,
                        )
                        csv_file.flush()
                else:
                    csv_path = args.csv or Path("logs") / f"vtx-test-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
                    csv_file, csv_writer = create_csv_writer(csv_path)
                    print(f"Sample log: {csv_path}")
                    sensor = NrpzSensor(args.dll)
                    sensor.open(args.resource)
                    results: list[tuple[dict[str, Any], dict[str, float | int]]] = []
                    for point in plan["points"]:
                        band = list(profile["bands"]).index(point["band"]) + 1
                        last_requested_frequency = point["frequency_mhz"]
                        print(f"\nSetting {test_power['nominal_mw']} mW, {point['band']}{point['channel']} ({point['frequency_mhz']} MHz)...")
                        current = switch_and_confirm(
                            client, current, band=band, channel=point["channel"],
                            frequency_mhz=point["frequency_mhz"], power_index=test_power["index"],
                            settle_time_s=settle_time_s,
                        )
                        level_offset_db = offset_db_at_frequency(measurement_path, point["frequency_mhz"])
                        sensor.set_level_offset(level_offset_db)
                        stable_readings = wait_for_stable_power(
                            sensor, timeout_ms=args.timeout_ms, sample_interval_s=sample_interval_s,
                            required_samples=args.stability_samples,
                            tolerance_percent=args.stability_tolerance_percent, max_wait_s=args.stability_timeout_s,
                        )
                        print(f"VTX output stable: {fmean(stable_readings):.0f} mW over {len(stable_readings)} readings.")
                        summary = measure_and_report_point(
                            sensor,
                            point,
                            power_index=test_power["index"],
                            timeout_ms=args.timeout_ms,
                            observation_time_s=observation_time_s,
                            sample_interval_s=sample_interval_s,
                            averages=args.averages,
                            csv_writer=csv_writer,
                            thresholds_percent=thresholds_percent,
                            use_color=use_color,
                            level_offset_db=level_offset_db,
                        )
                        results.append((point, summary))
                        csv_file.flush()
                    print_reference_chart(results, thresholds_percent, use_color=use_color)
            finally:
                sensor_close_error: NrpzError | None = None
                if sensor is not None:
                    try:
                        sensor.close()
                    except NrpzError as exc:
                        sensor_close_error = exc
                        print(f"Warning: could not close NRP-Z11 cleanly: {exc}", file=sys.stderr)
                    sensor = None
                # Keep the last frequency, but return the transmitter to 25 mW even
                # when a measurement above fails.
                try:
                    print(f"\nReturning VTX to {safe_power['nominal_mw']} mW...")
                    latest = parse_vtx_config(client.request(MSP_VTX_CONFIG))
                    latest_band = int(latest["band"])
                    latest_channel = int(latest["channel"])
                    latest_frequency = int(latest["frequency_mhz"]) or last_requested_frequency
                    if not latest_frequency:
                        raise MspError("Flight Controller did not report a usable frequency for the 25 mW safety reset.")
                    direct_frequency = not 1 <= latest_band <= 8
                    current = switch_and_confirm(
                        client,
                        latest,
                        band=latest_band if not direct_frequency else 1,
                        channel=latest_channel if 1 <= latest_channel <= 8 else 1,
                        frequency_mhz=latest_frequency,
                        power_index=safe_power["index"],
                        settle_time_s=settle_time_s,
                        direct_frequency=direct_frequency,
                    )
                    print(f"Final VTX: {format_vtx(current)}")
                except (MspError, ValueError) as exc:
                    safe_reset_error = exc
                    print(f"Warning: could not return VTX to 25 mW: {exc}", file=sys.stderr)
            if safe_reset_error:
                raise MspError("Test finished, but the VTX could not be confirmed at 25 mW.") from safe_reset_error
            if sensor_close_error:
                raise sensor_close_error
        return 0
    except (MspError, NrpzError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if csv_file is not None:
            csv_file.close()
        if sensor is not None:
            try:
                sensor.close()
            except NrpzError as exc:
                print(f"Error closing NRP-Z11: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
