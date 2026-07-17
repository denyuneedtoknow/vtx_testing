#!/usr/bin/env python3
"""Run the FT VTX 3.3 three-point 4 W test and return the VTX to 25 mW.

The test changes VTX settings through Betaflight MSP. Close Betaflight
Configurator first. An attenuator must be installed between the VTX and NRP-Z11.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from vtx_a1_measure import build_set_vtx_config_payload, format_vtx
from vtx_msp_probe import MSP_SET_VTX_CONFIG, MSP_VTX_CONFIG, MspClient, MspError, open_serial, parse_vtx_config
from vtx_power_logger import NrpzError, NrpzSensor, watts_to_millwatts


DEFAULT_PLAN = Path("configs/test_plans/ft_vtx_3_3_three_point_4w.json")


def load_plan(plan_path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        points = plan["points"]
        test_power = plan["test_power"]
        safe_power = plan["safe_power_after_test"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read test plan {plan_path}: {exc}") from exc
    if not isinstance(points, list) or not points:
        raise ValueError("Test plan must contain at least one point.")
    if not isinstance(test_power.get("index"), int) or not isinstance(safe_power.get("index"), int):
        raise ValueError("Test plan power indices must be integers.")
    for point in points:
        if not all(isinstance(point.get(key), int) for key in ("channel", "frequency_mhz")) or point.get("band") not in {"A", "B"}:
            raise ValueError(f"Invalid test point: {point!r}")
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
) -> dict[str, int | bool | None]:
    payload = build_set_vtx_config_payload(current, band, channel, frequency_mhz, power_index)
    client.request(MSP_SET_VTX_CONFIG, payload)
    time.sleep(settle_time_s)
    updated = parse_vtx_config(client.request(MSP_VTX_CONFIG))
    expected = (band, channel, frequency_mhz, power_index)
    actual = (updated["band"], updated["channel"], updated["frequency_mhz"], updated["power_index"])
    if actual != expected or not updated["ready"]:
        raise MspError(f"VTX did not confirm requested settings. Expected {expected}; got {actual}; ready={updated['ready']}.")
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure FT VTX 3.3 at 3170, 3330 and 3470 MHz in 4 W mode.")
    parser.add_argument("--port", required=True, help="Flight Controller COM port, for example COM83.")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="Three-point test plan JSON.")
    parser.add_argument("--baud", type=int, default=115200, help="MSP baud rate (default: 115200).")
    parser.add_argument("--timeout", type=float, default=2.0, help="MSP response timeout in seconds (default: 2).")
    parser.add_argument("--averages", type=int, default=16, help="NRP-Z11 manual averages (default: 16).")
    parser.add_argument("--timeout-ms", type=int, default=5_000, help="NRP-Z11 measurement timeout in ms (default: 5000).")
    parser.add_argument("--resource", default="*", help="NRP-Z11 VISA resource, or * for the first sensor.")
    parser.add_argument("--dll", help="Path to rsnrpz_64.dll.")
    args = parser.parse_args()
    if args.baud <= 0 or args.timeout <= 0 or args.averages <= 0 or args.timeout_ms <= 0:
        parser.error("baud, timeout, averages and timeout-ms must be positive.")
    return args


def main() -> int:
    args = parse_args()
    sensor: NrpzSensor | None = None
    safe_reset_error: Exception | None = None
    test_started = False
    try:
        plan = load_plan(args.plan)
        settle_time_s = float(plan["settle_time_s"])
        if settle_time_s < 0:
            raise ValueError("settle_time_s cannot be negative.")
        test_power = plan["test_power"]
        safe_power = plan["safe_power_after_test"]
        print(f"Plan: {plan['name']}")
        print(f"4 W points: {', '.join(str(point['frequency_mhz']) for point in plan['points'])} MHz")
        print(f"The VTX will be returned to {safe_power['nominal_mw']} mW at the end.")
        if input("Start the 4 W test? [y/N]: ").strip().lower() not in {"y", "yes", "т", "так"}:
            print("Cancelled. VTX settings were not changed.")
            return 0

        with open_serial(args.port, args.baud, args.timeout) as serial_port:
            client = MspClient(serial_port, args.timeout)
            current = parse_vtx_config(client.request(MSP_VTX_CONFIG))
            print(f"Initial VTX: {format_vtx(current)}")
            test_started = True

            try:
                sensor = NrpzSensor(args.dll)
                sensor.open(args.resource)
                for point in plan["points"]:
                    band = 1 if point["band"] == "A" else 2
                    print(f"\nSetting {test_power['nominal_mw']} mW, {point['band']}{point['channel']} ({point['frequency_mhz']} MHz)...")
                    current = switch_and_confirm(
                        client,
                        current,
                        band=band,
                        channel=point["channel"],
                        frequency_mhz=point["frequency_mhz"],
                        power_index=test_power["index"],
                        settle_time_s=settle_time_s,
                    )
                    sensor.configure(point["frequency_mhz"] * 1_000_000, args.averages)
                    raw_mw = watts_to_millwatts(sensor.read_watts(args.timeout_ms))
                    print(f"Measured sensor input: {raw_mw:.3f} mW (reference output: {point['reference_output_w']:.2f} W)")
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
                    current = switch_and_confirm(
                        client,
                        latest,
                        band=int(latest["band"]),
                        channel=int(latest["channel"]),
                        frequency_mhz=int(latest["frequency_mhz"]),
                        power_index=safe_power["index"],
                        settle_time_s=settle_time_s,
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
        if sensor is not None:
            try:
                sensor.close()
            except NrpzError as exc:
                print(f"Error closing NRP-Z11: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
