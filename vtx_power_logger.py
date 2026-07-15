#!/usr/bin/env python3
"""Console logger for an R&S NRP-Z11 USB power sensor on Windows."""

from __future__ import annotations

import argparse
import csv
import ctypes
import math
import os
import sys
import time
from ctypes import byref, c_char_p, c_double, c_int32, c_uint16, c_uint32
from datetime import datetime, timezone
from pathlib import Path


class NrpzError(RuntimeError):
    """An error reported by the R&S NRP-Z VXI PnP driver."""


class NrpzSensor:
    """Minimal wrapper for the measurement functions used by this application."""

    # Constants from the R&S NRP-Z programming interface.
    CHANNEL = 1
    MODE_CONTINUOUS_AVERAGE = 0
    TRIGGER_IMMEDIATE = 3

    def __init__(self, dll_path: str | None = None) -> None:
        if os.name != "nt":
            raise NrpzError("The NRP-Z driver is supported by this program on Windows only.")

        bundled_dll = Path(sys.executable if getattr(sys, "frozen", False) else __file__).with_name("rsnrpz_64.dll")
        library = dll_path or os.environ.get("RSNRPZ_DLL") or (
            str(bundled_dll) if bundled_dll.is_file() else "rsnrpz_64.dll"
        )
        try:
            self._dll = ctypes.WinDLL(library)
        except OSError as exc:
            raise NrpzError(
                "R&S NRP Toolkit driver was not found. Install it, place rsnrpz_64.dll "
                "next to this executable, add its directory to PATH, or pass "
                "--dll <path-to-rsnrpz_64.dll>. The DLL and executable must have the "
                "same architecture."
            ) from exc

        self._configure_api()
        self._session = c_uint32(0)

    def _configure_api(self) -> None:
        # ViStatus is a signed 32-bit integer; ViSession is a 32-bit VISA handle.
        self._dll.rsnrpz_init.argtypes = [c_char_p, c_uint16, c_uint16, ctypes.POINTER(c_uint32)]
        self._dll.rsnrpz_init.restype = c_int32
        self._dll.rsnrpz_close.argtypes = [c_uint32]
        self._dll.rsnrpz_close.restype = c_int32
        self._dll.rsnrpz_error_message.argtypes = [c_uint32, c_int32, c_char_p]
        self._dll.rsnrpz_error_message.restype = c_int32
        self._dll.rsnrpz_chan_mode.argtypes = [c_uint32, c_uint16, c_int32]
        self._dll.rsnrpz_chan_mode.restype = c_int32
        self._dll.rsnrpz_chan_setCorrectionFrequency.argtypes = [c_uint32, c_uint16, c_double]
        self._dll.rsnrpz_chan_setCorrectionFrequency.restype = c_int32
        self._dll.rsnrpz_trigger_setSource.argtypes = [c_uint32, c_uint16, c_int32]
        self._dll.rsnrpz_trigger_setSource.restype = c_int32
        self._dll.rsnrpz_avg_configureAvgManual.argtypes = [c_uint32, c_uint16, c_uint32]
        self._dll.rsnrpz_avg_configureAvgManual.restype = c_int32
        self._dll.rsnrpz_chan_initiate.argtypes = [c_uint32, c_uint16]
        self._dll.rsnrpz_chan_initiate.restype = c_int32
        self._dll.rsnrpz_chan_isMeasurementComplete.argtypes = [
            c_uint32, c_uint16, ctypes.POINTER(c_uint16)
        ]
        self._dll.rsnrpz_chan_isMeasurementComplete.restype = c_int32
        self._dll.rsnrpz_meass_fetchMeasurement.argtypes = [
            c_uint32, c_uint16, ctypes.POINTER(c_double)
        ]
        self._dll.rsnrpz_meass_fetchMeasurement.restype = c_int32

    def _check(self, status: int, operation: str) -> None:
        if status < 0:
            message = ctypes.create_string_buffer(1_024)
            if self._dll.rsnrpz_error_message(self._session, status, message) >= 0:
                detail = message.value.decode("utf-8", errors="replace").strip()
            else:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise NrpzError(f"{operation} failed (R&S driver status {status}){suffix}.")

    def open(self, resource: str) -> None:
        # "*" asks the driver to open the first available sensor.
        self._check(
            self._dll.rsnrpz_init(resource.encode("ascii"), 1, 0, byref(self._session)),
            "Opening sensor",
        )

    def configure(self, frequency_hz: float, averages: int) -> None:
        self._check(self._dll.rsnrpz_chan_mode(self._session, self.CHANNEL, self.MODE_CONTINUOUS_AVERAGE), "Setting mode")
        self._check(
            self._dll.rsnrpz_chan_setCorrectionFrequency(self._session, self.CHANNEL, frequency_hz),
            "Setting correction frequency",
        )
        self._check(self._dll.rsnrpz_trigger_setSource(self._session, self.CHANNEL, self.TRIGGER_IMMEDIATE), "Setting trigger")
        self._check(self._dll.rsnrpz_avg_configureAvgManual(self._session, self.CHANNEL, averages), "Setting averaging")

    def read_watts(self, timeout_ms: int) -> float:
        value = c_double()
        complete = c_uint16()
        self._check(self._dll.rsnrpz_chan_initiate(self._session, self.CHANNEL), "Starting measurement")
        deadline = time.monotonic() + timeout_ms / 1_000
        while not complete.value:
            self._check(
                self._dll.rsnrpz_chan_isMeasurementComplete(self._session, self.CHANNEL, byref(complete)),
                "Checking measurement status",
            )
            if not complete.value:
                if time.monotonic() >= deadline:
                    raise NrpzError(f"Measurement did not complete within {timeout_ms} ms.")
                time.sleep(0.05)
        self._check(
            self._dll.rsnrpz_meass_fetchMeasurement(self._session, self.CHANNEL, byref(value)),
            "Fetching power",
        )
        if not math.isfinite(value.value) or value.value < 0:
            raise NrpzError(f"Sensor returned an invalid power value: {value.value!r} W.")
        return value.value

    def close(self) -> None:
        if self._session.value:
            try:
                self._check(self._dll.rsnrpz_close(self._session), "Closing sensor")
            finally:
                self._session = c_uint32(0)


def watts_to_millwatts(watts: float) -> float:
    return watts * 1_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read and CSV-log average power from an R&S NRP-Z11 sensor.")
    parser.add_argument("--resource", default="*", help="VISA resource, or * for the first connected sensor (default: *).")
    parser.add_argument("--frequency-hz", type=float, default=2.7e9, help="RF frequency for sensor correction (default: 2700000000).")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between printed readings (default: 1).")
    parser.add_argument("--averages", type=int, default=16, help="Manual averages per reading (default: 16).")
    parser.add_argument("--timeout-ms", type=int, default=5_000, help="Measurement timeout in milliseconds (default: 5000).")
    parser.add_argument("--csv", type=Path, help="Write readings to this CSV file. Omit for console-only mode.")
    parser.add_argument("--dll", help="Path to rsnrpz_64.dll; overrides RSNRPZ_DLL and PATH.")
    args = parser.parse_args()
    if args.interval <= 0 or args.averages <= 0 or args.timeout_ms <= 0 or args.frequency_hz <= 0:
        parser.error("interval, averages, timeout-ms and frequency-hz must be positive.")
    return args


def create_writer(csv_path: Path | None):
    if csv_path is None:
        return None, None
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not csv_path.exists() or csv_path.stat().st_size == 0
    file = csv_path.open("a", newline="", encoding="utf-8")
    writer = csv.writer(file)
    if is_new_file:
        writer.writerow(["timestamp_utc", "elapsed_s", "power_mw"])
        file.flush()
    return file, writer


def main() -> int:
    args = parse_args()
    csv_file = None
    writer = None
    sensor = None
    result = 0
    try:
        csv_file, writer = create_writer(args.csv)
        sensor = NrpzSensor(args.dll)
        started = time.monotonic()
        sensor.open(args.resource)
        sensor.configure(args.frequency_hz, args.averages)
        print(f"Connected. Frequency correction: {args.frequency_hz / 1e6:.3f} MHz. Press Ctrl+C to stop.")
        while True:
            loop_started = time.monotonic()
            watts = sensor.read_watts(args.timeout_ms)
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            elapsed = loop_started - started
            milliwatts = watts_to_millwatts(watts)
            print(f"{timestamp}  {milliwatts:.3f} mW", flush=True)
            if writer:
                writer.writerow([timestamp, f"{elapsed:.3f}", f"{milliwatts:.6f}"])
                csv_file.flush()
            time.sleep(max(0, args.interval - (time.monotonic() - loop_started)))
    except KeyboardInterrupt:
        print("\nStopped.")
    except NrpzError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        result = 2
    finally:
        try:
            if sensor is not None:
                sensor.close()
        except NrpzError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            result = 2
        if csv_file:
            csv_file.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
