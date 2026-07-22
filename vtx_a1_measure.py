#!/usr/bin/env python3
"""Switch an FT VTX to A1 through Betaflight, then take one NRP-Z11 reading.

Close Betaflight Configurator before running this program: it must have exclusive
access to the Flight Controller COM port. The VTX must be connected through the
attenuator before its RF output reaches the NRP-Z11.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

from vtx_msp_probe import (
    MSP_SET_VTX_CONFIG,
    MSP_VTX_CONFIG,
    MspClient,
    MspError,
    open_serial,
    parse_vtx_config,
    resolve_port,
)
from vtx_profiles import load_profile, select_profile
from vtx_power_logger import NrpzError, NrpzSensor, watts_to_millwatts


def load_a1_frequency(profile: dict[str, object]) -> int:
    try:
        frequency_mhz = profile["bands"]["A"][0]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"Cannot read A1 from VTX profile: {exc}") from exc
    if not isinstance(frequency_mhz, int) or frequency_mhz <= 0:
        raise ValueError(f"Invalid A1 frequency in {profile_path}: {frequency_mhz!r}")
    return frequency_mhz


def build_set_vtx_config_payload(
    current: dict[str, int | bool | None],
    band: int,
    channel: int,
    frequency_mhz: int,
    power_index: int | None = None,
    direct_frequency: bool = False,
) -> bytes:
    """Create a Betaflight API >= 1.42 MSP_SET_VTX_CONFIG payload."""
    if not 1 <= band <= 255 or not 1 <= channel <= 8:
        raise ValueError("band must be in the range 1..255 and channel in the range 1..8.")
    if not 1 <= frequency_mhz <= 0xFFFF:
        raise ValueError("frequency_mhz must fit in an unsigned 16-bit value.")

    # MSP's old packed representation has only three bits for band and works
    # for bands 1..8. For extra bands, command the frequency directly: both
    # the legacy and API 1.42 explicit fields use band=0 / frequency in MHz.
    if direct_frequency:
        packed_band_channel = frequency_mhz
        explicit_band = 0
        explicit_channel = 0
    else:
        packed_band_channel = (band - 1) * 8 + (channel - 1) if band <= 8 else 0
        explicit_band = band
        explicit_channel = channel
    selected_power_index = current["power_index"] if power_index is None else power_index
    pit_mode = current["pit_mode"]
    low_power_disarm = current["low_power_disarm"]
    pit_frequency = current["pit_mode_frequency_mhz"]
    if not all(isinstance(value, (int, bool)) for value in (selected_power_index, pit_mode, low_power_disarm, pit_frequency)):
        raise ValueError("Current VTX configuration is incomplete.")

    # Send only the 11 VTX-setting bytes. Betaflight accepts an optional four-byte
    # VTX-table extension, but this tester deliberately omits it so it can never
    # change the Flight Controller's table dimensions or clear the table.
    return struct.pack(
        "<HBBBHBBH",
        packed_band_channel,
        int(selected_power_index),
        int(bool(pit_mode)),
        int(low_power_disarm),
        int(pit_frequency),
        explicit_band,
        explicit_channel,
        frequency_mhz,
    )


def format_vtx(vtx: dict[str, int | bool | None]) -> str:
    return (
        f"Band {vtx['band']}, channel {vtx['channel']}, "
        f"{vtx['frequency_mhz']} MHz, power index {vtx['power_index']}, "
        f"pit mode: {'on' if vtx['pit_mode'] else 'off'}, ready: {vtx['ready']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Confirm, switch VTX to A1 through Betaflight, and measure its power once.")
    parser.add_argument(
        "--port",
        help="Flight Controller COM port, for example COM83. Omit to auto-detect the first MSP-capable FC.",
    )
    parser.add_argument("--profile", type=Path, help="VTX profile containing band A channel 1. Omit to select interactively.")
    parser.add_argument("--baud", type=int, default=115200, help="MSP baud rate (default: 115200).")
    parser.add_argument("--timeout", type=float, default=2.0, help="MSP response timeout in seconds (default: 2).")
    parser.add_argument("--settle-time", type=float, default=3.0, help="Seconds to wait after switching (default: 3).")
    parser.add_argument("--averages", type=int, default=16, help="NRP-Z11 manual averages (default: 16).")
    parser.add_argument("--timeout-ms", type=int, default=5_000, help="NRP-Z11 measurement timeout in ms (default: 5000).")
    parser.add_argument("--resource", default="*", help="NRP-Z11 VISA resource, or * for the first sensor.")
    parser.add_argument("--dll", help="Path to rsnrpz_64.dll.")
    args = parser.parse_args()
    if args.baud <= 0 or args.timeout <= 0 or args.settle_time < 0 or args.averages <= 0 or args.timeout_ms <= 0:
        parser.error("baud, timeout, averages and timeout-ms must be positive; settle-time cannot be negative.")
    return args


def main() -> int:
    args = parse_args()
    try:
        port = resolve_port(args.port, args.baud, args.timeout)
        if args.profile is None:
            _, profile = select_profile()
        else:
            profile = load_profile(args.profile)
        target_frequency = load_a1_frequency(profile)
        print(f"Selected VTX: {profile['model']}")
        with open_serial(port, args.baud, args.timeout) as serial_port:
            client = MspClient(serial_port, args.timeout)
            current = parse_vtx_config(client.request(MSP_VTX_CONFIG))
            print(f"Current VTX: {format_vtx(current)}")
            print(f"Target: A1 (band 1, channel 1) = {target_frequency} MHz")
            if input("Switch to A1 and measure power? [y/N]: ").strip().lower() not in {"y", "yes", "т", "так"}:
                print("Cancelled. VTX settings were not changed.")
                return 0

            payload = build_set_vtx_config_payload(current, band=1, channel=1, frequency_mhz=target_frequency)
            client.request(MSP_SET_VTX_CONFIG, payload)
            print(f"Switch command sent. Waiting {args.settle_time:g} s...")
            time.sleep(args.settle_time)
            updated = parse_vtx_config(client.request(MSP_VTX_CONFIG))

        print(f"VTX after switch: {format_vtx(updated)}")
        if (updated["band"], updated["channel"], updated["frequency_mhz"]) != (1, 1, target_frequency):
            raise MspError("VTX did not confirm A1 after the switch; power will not be measured.")
        if not updated["ready"]:
            raise MspError("VTX is not ready after the switch; power will not be measured.")

        sensor = NrpzSensor(args.dll)
        try:
            sensor.open(args.resource)
            sensor.configure(target_frequency * 1_000_000, args.averages)
            milliwatts = watts_to_millwatts(sensor.read_watts(args.timeout_ms))
        finally:
            sensor.close()
        print(f"Power at A1 ({target_frequency} MHz): {milliwatts:.0f} mW")
        return 0
    except (MspError, NrpzError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
