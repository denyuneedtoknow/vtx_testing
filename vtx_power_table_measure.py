#!/usr/bin/env python3
"""Measure every configured VTX channel at every non-zero power level.

The resulting CSV is a power-by-channel matrix for opening in Excel.  The
Flight Controller VTX table is read and verified before any VTX switching;
the script never writes the table or EEPROM.
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

from measurement_path import load_measurement_path, offset_db_at_frequency, validate_sensor_frequencies
from vtx_a1_measure import format_vtx
from vtx_msp_probe import MSP_VTX_CONFIG, MspClient, MspError, open_serial, parse_vtx_config, resolve_port
from vtx_power_logger import NrpzError, NrpzSensor, watts_to_millwatts
from vtx_profiles import load_profile
from vtx_three_point_test import (
    PLAN_DIRECTORY,
    VTX_PLAN_FILENAMES,
    install_soft_interrupt_handler,
    resolve_power_indexes,
    switch_and_confirm,
    wait_for_stable_power,
)


# The original VTX options reuse their three-point plans because those plans
# also contain the measurement-path and settling settings.  The three new
# Betaflight tables have dedicated full-table plans: they deliberately do not
# need reference values or three-point test settings.
POWER_TABLE_PLAN_FILENAMES = {
    **VTX_PLAN_FILENAMES,
    "low": "vtx_4_9_7_2_low_full_table_4w.json",
    "mid": "vtx_4_9_7_2_mid_full_table_4w.json",
    "high": "vtx_4_9_7_2_high_full_table_4w.json",
    "5.8.test": "ft_vtx_graph58_p5_full_table_5w.json",
}
DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES = {
    "1.2.all": "ft_vtx_1_2_direct_sweep_4w.json",
    "2.7.all": "ft_vtx_2_7_direct_sweep_4w.json",
    "5.8.all": "vtx_4_9_7_2_direct_sweep_4w.json",
    "8.5": "ft_vtx_8_5_direct_sweep_5w.json",
    "4.9-7.2": "vtx_4_9_7_2_direct_sweep_4w.json",  # Backward-compatible alias.
}


def load_power_table_plan(plan_path: Path) -> dict[str, Any]:
    """Load settings required by a full VTX power-table measurement."""
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        profile_path = Path(plan["vtx_profile"])
        measurement_path_profile = plan["measurement_path_profile"]
        settle_time_s = float(plan["settle_time_s"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot read full-table test plan {plan_path}: {exc}") from exc
    if not isinstance(measurement_path_profile, str) or settle_time_s < 0:
        raise ValueError("Full-table test plan has invalid measurement-path or settle-time settings.")
    profile = load_profile(profile_path if profile_path.is_absolute() else plan_path.parent / profile_path)
    plan["_profile"] = profile
    return plan


def load_direct_frequency_sweep_plan(plan_path: Path) -> dict[str, Any]:
    """Load a factory-frequency sweep that does not require an FC VTX table."""
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        profile = plan["profile"]
        measurement_path_profile = plan["measurement_path_profile"]
        settle_time_s = float(plan["settle_time_s"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Cannot read direct-frequency sweep plan {plan_path}: {exc}") from exc
    if not isinstance(profile, dict) or not isinstance(measurement_path_profile, str) or settle_time_s < 0:
        raise ValueError("Direct-frequency sweep plan has invalid settings.")
    measurement_points(profile)
    measurement_power_levels(profile)
    plan["_profile"] = profile
    return plan


def measurement_points(profile: dict[str, Any]) -> list[dict[str, int | str]]:
    """Return one measurement point for every non-zero VTX-table frequency."""
    points: list[dict[str, int | str]] = []
    bands = profile.get("bands")
    if not isinstance(bands, dict):
        raise ValueError("VTX profile is missing bands.")
    for band_index, (band, frequencies) in enumerate(bands.items(), start=1):
        if not isinstance(band, str) or not isinstance(frequencies, list):
            raise ValueError("VTX profile bands are invalid.")
        for channel, frequency_mhz in enumerate(frequencies, start=1):
            if not isinstance(frequency_mhz, int):
                raise ValueError("VTX profile frequencies must be integers.")
            if frequency_mhz > 0:
                points.append(
                    {
                        "band": band,
                        "band_index": band_index,
                        "channel": channel,
                        "frequency_mhz": frequency_mhz,
                    }
                )
    if not points:
        raise ValueError("VTX profile has no usable frequencies.")
    return points


def measurement_power_levels(profile: dict[str, Any]) -> list[tuple[int, int]]:
    """Return (Betaflight power index, nominal mW) pairs, excluding the off level."""
    levels = profile.get("power_levels_mw")
    if not isinstance(levels, list):
        raise ValueError("VTX profile is missing power_levels_mw.")
    powers = [(index, value) for index, value in enumerate(levels, start=1) if isinstance(value, int) and value > 0]
    if not powers or len(powers) != len([value for value in levels if isinstance(value, int) and value > 0]):
        raise ValueError("VTX profile must contain positive integer power levels.")
    return powers


def selected_power_levels(profile: dict[str, Any], selection: str) -> list[tuple[int, int]]:
    """Return the requested working power level(s), without the PIT/off level."""
    powers = measurement_power_levels(profile)
    if selection == "all":
        return powers
    requested_mw = int(selection)
    selected = [(index, power_mw) for index, power_mw in powers if power_mw == requested_mw]
    if not selected:
        raise ValueError(f"The selected VTX profile does not provide {requested_mw} mW.")
    return selected


def observe_power_trend(
    sensor: NrpzSensor,
    *,
    timeout_ms: int,
    sample_interval_s: float,
    samples: int,
    threshold_percent: float,
) -> dict[str, float | int | str]:
    """Observe output drift after settling; this is informational, not a failure gate."""
    if samples < 2 or sample_interval_s < 0 or threshold_percent <= 0:
        raise ValueError("Invalid trend observation settings.")
    started = time.monotonic()
    readings: list[float] = []
    for sample_index in range(samples):
        readings.append(watts_to_millwatts(sensor.read_watts(timeout_ms)))
        if sample_index + 1 < samples and sample_interval_s:
            time.sleep(sample_interval_s)
    first_mw, last_mw = readings[0], readings[-1]
    mean_mw = fmean(readings)
    change_percent = (last_mw - first_mw) / mean_mw * 100 if mean_mw else 0.0
    trend = "rising" if change_percent > threshold_percent else "falling" if change_percent < -threshold_percent else "stable"
    return {
        "first_mw": first_mw,
        "last_mw": last_mw,
        "change_percent": change_percent,
        "elapsed_s": time.monotonic() - started,
        "samples": len(readings),
        "trend": trend,
    }


def write_matrix(path: Path, points: list[dict[str, int | str]], powers: list[tuple[int, int]], values: dict[tuple[int, str], float]) -> None:
    """Write the current result matrix, keeping incomplete cells empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ukrainian/European Excel uses a semicolon as the list separator. A UTF-8
    # BOM also makes Excel reliably recognize the text encoding on Windows.
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file, delimiter=";")
        labels = [f"{point['band']}{point['channel']} ({point['frequency_mhz']} MHz)" for point in points]
        writer.writerow(["power_mw", *labels])
        for power_index, power_mw in powers:
            writer.writerow(
                [
                    power_mw,
                    *[
                        f"{values[(power_index, str(point['band']) + str(point['channel']))]:.2f}".replace(".", ",")
                        if (power_index, str(point["band"]) + str(point["channel"])) in values
                        else ""
                        for point in points
                    ],
                ]
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure all VTX channels and power levels into a CSV matrix.")
    parser.add_argument("--vtx", choices=sorted({*POWER_TABLE_PLAN_FILENAMES, *DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES}), required=True, help="VTX profile, for example 5.8, 5.8.legacy or 5.8.all.")
    parser.add_argument("--port", help="Flight Controller COM port, for example COM35. Omit to auto-detect.")
    parser.add_argument("--baud", type=int, default=115200, help="MSP baud rate (default: 115200).")
    parser.add_argument("--timeout", type=float, default=2.0, help="MSP response timeout in seconds (default: 2).")
    parser.add_argument("--settle-time", type=float, help="Seconds to wait after each VTX switch (defaults to the selected plan).")
    parser.add_argument("--power", choices=("all", "25", "500", "2000", "2500", "3000", "4000", "5000"), default="all", help="Nominal output power to measure, or all available levels (default: all).")
    parser.add_argument("--samples", type=int, default=3, help="Independent sensor readings to average per cell (default: 3).")
    parser.add_argument("--warmup-readings", type=int, default=1, help="Sensor readings to discard after each switch, before averaging (default: 1).")
    parser.add_argument("--stability-samples", type=int, default=3, help="Consecutive readings required before a cell is measured (default: 3).")
    parser.add_argument("--stability-tolerance-percent", type=float, default=2.0, help="Maximum peak-to-peak spread for stable readings (default: 2).")
    parser.add_argument("--stability-timeout-s", type=float, default=15.0, help="Maximum time to wait for output stabilization (default: 15).")
    parser.add_argument("--trend-samples", type=int, default=10, help="Readings used to observe post-settle output trend (default: 10).")
    parser.add_argument("--trend-threshold-percent", type=float, default=2.0, help="Absolute first-to-last change classified as rising/falling (default: 2).")
    parser.add_argument("--sample-interval", type=float, default=0.5, help="Seconds between readings in a cell (default: 0.5).")
    parser.add_argument("--averages", type=int, default=16, help="NRP-Z11 averages within each reading (default: 16).")
    parser.add_argument("--timeout-ms", type=int, default=5000, help="NRP-Z11 measurement timeout in ms (default: 5000).")
    parser.add_argument("--resource", default="*", help="NRP-Z11 VISA resource, or * for the first sensor.")
    parser.add_argument("--dll", help="Path to rsnrpz_64.dll.")
    parser.add_argument("--csv", type=Path, help="Output CSV. Default: logs/vtx-power-table-<UTC timestamp>.csv.")
    args = parser.parse_args()
    if (args.baud <= 0 or args.timeout <= 0 or args.samples <= 0 or args.warmup_readings < 0 or args.sample_interval < 0
            or args.averages <= 0 or args.timeout_ms <= 0 or args.stability_samples < 2
            or args.stability_tolerance_percent <= 0 or args.stability_timeout_s <= 0 or args.trend_samples < 2
            or args.trend_threshold_percent <= 0):
        parser.error("baud, timeout, samples, averages, timeout-ms, stability-tolerance-percent and stability-timeout-s must be positive; stability-samples must be at least 2; warmup-readings and sample-interval cannot be negative.")
    if args.settle_time is not None and args.settle_time < 0:
        parser.error("settle-time cannot be negative.")
    return args


def main() -> int:
    install_soft_interrupt_handler()
    args = parse_args()
    sensor: NrpzSensor | None = None
    safe_reset_error: Exception | None = None
    try:
        direct_plan = args.vtx in DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES
        plan_path = PLAN_DIRECTORY / (DIRECT_FREQUENCY_SWEEP_PLAN_FILENAMES[args.vtx] if direct_plan else POWER_TABLE_PLAN_FILENAMES[args.vtx])
        plan = load_direct_frequency_sweep_plan(plan_path) if direct_plan else load_power_table_plan(plan_path)
        direct_frequency = bool(plan.get("direct_frequency", True))
        profile = plan["_profile"]
        measurement_path_value = plan.get("measurement_path_profile")
        if not isinstance(measurement_path_value, str):
            raise ValueError("Test plan is missing measurement_path_profile.")
        measurement_path = load_measurement_path(plan_path.parent / measurement_path_value)
        points = measurement_points(profile)
        validate_sensor_frequencies(measurement_path, [int(point["frequency_mhz"]) for point in points])
        powers = selected_power_levels(profile, args.power)
        settle_time_s = float(plan["settle_time_s"]) if args.settle_time is None else args.settle_time
        output_path = args.csv or Path("logs") / f"vtx-power-table-{args.vtx}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
        values: dict[tuple[int, str], float] = {}
        write_matrix(output_path, points, powers, values)

        print(f"Plan: {'direct-frequency sweep of' if direct_frequency else 'full'} {profile['model']} power table")
        print(f"{len(points)} channels × {len(powers)} power levels × {args.samples} averaged readings")
        print(f"Discarded warmup readings per cell: {args.warmup_readings}")
        print("Path correction: fixed attenuator minus frequency-dependent path loss.")
        print(f"Trend observation: {args.trend_samples} readings, {args.sample_interval:g} s apart; rising/falling threshold: {args.trend_threshold_percent:g}%.")
        print(f"Result matrix: {output_path}")
        print("The VTX will be returned to 25 mW at the end.")

        port = resolve_port(args.port, args.baud, args.timeout)
        with open_serial(port, args.baud, args.timeout) as serial_port:
            client = MspClient(serial_port, args.timeout)
            current = parse_vtx_config(client.request(MSP_VTX_CONFIG))
            print(f"Initial VTX: {format_vtx(current)}")
            if direct_frequency:
                power_indexes = resolve_power_indexes(client, current, [power_mw for _, power_mw in powers] + [25])
                print("Verifying required FC power levels (read-only; frequencies are sent directly)...")
                print("Required power levels found. FC frequency table is not used or changed.")
            else:
                power_indexes = {power_mw: power_index for power_index, power_mw in powers}

            try:
                sensor = NrpzSensor(args.dll)
                sensor.open(args.resource)
                for matrix_power_index, power_mw in powers:
                    power_index = power_indexes[power_mw]
                    print(f"\n{power_mw} mW (power index {power_index})")
                    for point in points:
                        label = f"{point['band']}{point['channel']} ({point['frequency_mhz']} MHz)"
                        current = switch_and_confirm(
                            client,
                            current,
                            band=int(point["band_index"]),
                            channel=int(point["channel"]),
                            frequency_mhz=int(point["frequency_mhz"]),
                            power_index=power_index,
                            settle_time_s=settle_time_s,
                            direct_frequency=direct_frequency,
                        )
                        sensor.configure(int(point["frequency_mhz"]) * 1_000_000, args.averages)
                        level_offset_db = offset_db_at_frequency(measurement_path, int(point["frequency_mhz"]))
                        sensor.set_level_offset(level_offset_db)
                        for _ in range(args.warmup_readings):
                            sensor.read_watts(args.timeout_ms)
                            if args.sample_interval:
                                time.sleep(args.sample_interval)
                        stable_readings = wait_for_stable_power(
                            sensor, timeout_ms=args.timeout_ms, sample_interval_s=args.sample_interval,
                            required_samples=args.stability_samples,
                            tolerance_percent=args.stability_tolerance_percent, max_wait_s=args.stability_timeout_s,
                        )
                        print(f"  VTX output stable: {fmean(stable_readings):.0f} mW over {len(stable_readings)} readings.")
                        trend = observe_power_trend(
                            sensor, timeout_ms=args.timeout_ms, sample_interval_s=args.sample_interval,
                            samples=args.trend_samples, threshold_percent=args.trend_threshold_percent,
                        )
                        print(
                            f"  Output trend: {label} / {power_mw} mW: "
                            f"{trend['first_mw']:.0f} → {trend['last_mw']:.0f} mW, "
                            f"{trend['change_percent']:+.1f}% over {trend['elapsed_s']:.1f} s, "
                            f"{trend['trend']}"
                        )
                        readings: list[float] = []
                        for sample_index in range(args.samples):
                            readings.append(watts_to_millwatts(sensor.read_watts(args.timeout_ms)))
                            if sample_index + 1 < args.samples and args.sample_interval:
                                time.sleep(args.sample_interval)
                        average_mw = fmean(readings)
                        values[(matrix_power_index, str(point["band"]) + str(point["channel"]))] = average_mw
                        write_matrix(output_path, points, powers, values)
                        print(f"  {label:<18} {average_mw:.0f} mW  ({len(readings)} readings; trend {trend['trend']})")
            finally:
                if sensor is not None:
                    try:
                        sensor.close()
                    finally:
                        sensor = None
                try:
                    print("\nReturning VTX to 25 mW...")
                    latest = parse_vtx_config(client.request(MSP_VTX_CONFIG))
                    frequency_mhz = int(latest["frequency_mhz"]) or int(points[-1]["frequency_mhz"])
                    band = int(latest["band"])
                    channel = int(latest["channel"])
                    if not frequency_mhz or (not direct_frequency and (not 1 <= band <= 8 or not 1 <= channel <= 8)):
                        raise MspError("Flight Controller did not report a usable VTX setting for the 25 mW safety reset.")
                    current = switch_and_confirm(
                        client, latest, band=band or 1, channel=channel or 1, frequency_mhz=frequency_mhz,
                        power_index=power_indexes[25], settle_time_s=settle_time_s, direct_frequency=direct_frequency,
                    )
                    print(f"Final VTX: {format_vtx(current)}")
                except (MspError, ValueError) as exc:
                    safe_reset_error = exc
                    print(f"Warning: could not return VTX to 25 mW: {exc}", file=sys.stderr)
        if safe_reset_error:
            raise MspError("Measurement finished, but the VTX could not be confirmed at 25 mW.") from safe_reset_error
        print(f"\nCompleted matrix: {output_path}")
        return 0
    except KeyboardInterrupt:
        print("\nStop requested by operator. Safety reset sequence completed or was attempted.", file=sys.stderr)
        return 130
    except (MspError, NrpzError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        if sensor is not None:
            try:
                sensor.close()
            except NrpzError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
