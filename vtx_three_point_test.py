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

from vtx_a1_measure import build_set_vtx_config_payload, format_vtx
from vtx_msp_probe import MSP_SET_VTX_CONFIG, MSP_VTX_CONFIG, MspClient, MspError, open_serial, parse_vtx_config
from vtx_profiles import load_profile
from vtx_power_logger import NrpzError, NrpzSensor, watts_to_millwatts


PLAN_DIRECTORY = Path(__file__).with_name("configs") / "test_plans"
VTX_PLAN_FILENAMES = {
    "3.3": "ft_vtx_3_3_three_point_4w.json",
    "5.8": "ft_vtx_5_8_three_point_4w.json",
    "7.2": "ft_vtx_7_2_three_point_5w.json",
}


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


def create_csv_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("w", newline="", encoding="utf-8")
    writer = csv.writer(file)
    writer.writerow([
        "timestamp_utc", "frequency_mhz", "band", "channel", "power_index", "sample_elapsed_s", "sensor_input_mw"
    ])
    return file, writer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure FT VTX 3.3 at 3170, 3330 and 3470 MHz in 4 W mode.")
    parser.add_argument("--port", required=True, help="Flight Controller COM port, for example COM83.")
    plan_source = parser.add_mutually_exclusive_group()
    plan_source.add_argument("--vtx", choices=sorted(VTX_PLAN_FILENAMES), help="VTX band to test: 3.3, 5.8 or 7.2 GHz.")
    plan_source.add_argument("--plan", type=Path, help="Three-point test plan JSON. Omit to select interactively.")
    parser.add_argument("--baud", type=int, default=115200, help="MSP baud rate (default: 115200).")
    parser.add_argument("--timeout", type=float, default=2.0, help="MSP response timeout in seconds (default: 2).")
    parser.add_argument("--averages", type=int, default=16, help="NRP-Z11 manual averages (default: 16).")
    parser.add_argument("--timeout-ms", type=int, default=5_000, help="NRP-Z11 measurement timeout in ms (default: 5000).")
    parser.add_argument("--resource", default="*", help="NRP-Z11 VISA resource, or * for the first sensor.")
    parser.add_argument("--dll", help="Path to rsnrpz_64.dll.")
    parser.add_argument("--csv", type=Path, help="Write all samples to this CSV file. Default: logs/<timestamp>.csv.")
    args = parser.parse_args()
    if args.baud <= 0 or args.timeout <= 0 or args.averages <= 0 or args.timeout_ms <= 0:
        parser.error("baud, timeout, averages and timeout-ms must be positive.")
    return args


def main() -> int:
    args = parse_args()
    sensor: NrpzSensor | None = None
    csv_file = None
    csv_writer = None
    safe_reset_error: Exception | None = None
    test_started = False
    last_requested_frequency: int | None = None
    try:
        plan_path = plan_for_vtx(args.vtx) if args.vtx else args.plan or select_plan()
        plan = load_plan(plan_path)
        profile = plan["_profile"]
        settle_time_s = float(plan["settle_time_s"])
        observation_time_s = float(plan["observation_time_s"])
        sample_interval_s = float(plan["sample_interval_s"])
        if settle_time_s < 0 or observation_time_s <= 0 or sample_interval_s <= 0:
            raise ValueError("settle_time_s cannot be negative; observation_time_s and sample_interval_s must be positive.")
        test_power = plan["test_power"]
        safe_power = plan["safe_power_after_test"]
        print(f"Plan: {plan['name']}")
        print(f"4 W points: {', '.join(str(point['frequency_mhz']) for point in plan['points'])} MHz")
        print(f"Observation per point: {observation_time_s:g} s, every {sample_interval_s:g} s")
        print(f"The VTX will be returned to {safe_power['nominal_mw']} mW at the end.")
        if input("Start the 4 W test? [y/N]: ").strip().lower() not in {"y", "yes", "т", "так"}:
            print("Cancelled. VTX settings were not changed.")
            return 0

        with open_serial(args.port, args.baud, args.timeout) as serial_port:
            client = MspClient(serial_port, args.timeout)
            current = parse_vtx_config(client.request(MSP_VTX_CONFIG))
            print(f"Initial VTX: {format_vtx(current)}")
            test_started = True

            csv_path = args.csv or Path("logs") / f"vtx-test-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
            csv_file, csv_writer = create_csv_writer(csv_path)
            print(f"Sample log: {csv_path}")

            try:
                sensor = NrpzSensor(args.dll)
                sensor.open(args.resource)
                for point in plan["points"]:
                    band = list(profile["bands"]).index(point["band"]) + 1
                    direct_frequency = band > 8
                    last_requested_frequency = point["frequency_mhz"]
                    print(f"\nSetting {test_power['nominal_mw']} mW, {point['band']}{point['channel']} ({point['frequency_mhz']} MHz)...")
                    current = switch_and_confirm(
                        client,
                        current,
                        band=band,
                        channel=point["channel"],
                        frequency_mhz=point["frequency_mhz"],
                        power_index=test_power["index"],
                        settle_time_s=settle_time_s,
                        direct_frequency=direct_frequency,
                    )
                    sensor.configure(point["frequency_mhz"] * 1_000_000, args.averages)
                    readings = observe_power(sensor, args.timeout_ms, observation_time_s, sample_interval_s)
                    for sample_elapsed_s, raw_mw in readings:
                        csv_writer.writerow([
                            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                            point["frequency_mhz"], point["band"], point["channel"], test_power["index"],
                            f"{sample_elapsed_s:.3f}", f"{raw_mw:.6f}",
                        ])
                    csv_file.flush()
                    summary = summarize_readings(readings)
                    reference_output_mw = point.get("reference_output_mw")
                    reference_text = (
                        f" (reference output: {reference_output_mw:.0f} mW)"
                        if isinstance(reference_output_mw, (int, float))
                        else ""
                    )
                    print(
                        "Sensor input: "
                        f"avg {summary['average_mw']:.0f} mW, min {summary['minimum_mw']:.0f} mW, "
                        f"max {summary['maximum_mw']:.0f} mW, "
                        f"change {summary['change_mw']:+.0f} mW ({summary['change_percent']:+.1f}%) "
                        f"over {summary['count']} samples"
                        f"{reference_text}"
                    )
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
