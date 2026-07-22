#!/usr/bin/env python3
"""Read the current VTX configuration from a Betaflight Flight Controller via MSP.

This utility is read-only: it never sends commands that change Flight Controller or
VTX settings. Close Betaflight Configurator before using it because both programs
need exclusive access to the Flight Controller serial port.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass


MSP_API_VERSION = 1
MSP_FC_VARIANT = 2
MSP_FC_VERSION = 3
MSP_VTX_CONFIG = 88
MSP_SET_VTX_CONFIG = 89
MSP_VTXTABLE_BAND = 137
MSP_VTXTABLE_POWERLEVEL = 138
DEFAULT_SCAN_TIMEOUT_S = 1.0


class MspError(RuntimeError):
    """An MSP transport or Flight Controller response error."""


@dataclass(frozen=True)
class MspFrame:
    direction: str
    command: int
    payload: bytes


@dataclass(frozen=True)
class FlightControllerInfo:
    port: str
    variant: str
    firmware_version: str
    msp_api_version: str


def msp_v1_request(command: int, payload: bytes = b"") -> bytes:
    """Build an MSP v1 request frame."""
    if not 0 <= command <= 255 or len(payload) > 255:
        raise ValueError("MSP v1 supports command IDs and payloads up to 255.")
    checksum = len(payload) ^ command
    for byte in payload:
        checksum ^= byte
    return b"$M<" + bytes((len(payload), command)) + payload + bytes((checksum,))


def parse_vtx_config(payload: bytes) -> dict[str, int | bool | None]:
    """Parse the stable core fields of the MSP_VTX_CONFIG response.

    Betaflight appends fields to this response between API versions. The first nine
    bytes have remained the essential current VTX state, so optional fields are
    decoded only when present.
    """
    if len(payload) < 9:
        raise MspError(f"MSP_VTX_CONFIG response is too short ({len(payload)} bytes).")

    result: dict[str, int | bool | None] = {
        "device_type": payload[0],
        "band": payload[1],
        "channel": payload[2],
        "power_index": payload[3],
        "pit_mode": bool(payload[4]),
        "frequency_mhz": int.from_bytes(payload[5:7], "little"),
        "ready": bool(payload[7]),
        "low_power_disarm": payload[8],
        "pit_mode_frequency_mhz": None,
        "vtx_table_available": None,
        "vtx_table_bands": None,
        "vtx_table_channels": None,
        "vtx_table_power_levels": None,
    }
    if len(payload) >= 11:
        result["pit_mode_frequency_mhz"] = int.from_bytes(payload[9:11], "little")
    if len(payload) >= 15:
        result["vtx_table_available"] = bool(payload[11])
        result["vtx_table_bands"] = payload[12]
        result["vtx_table_channels"] = payload[13]
        result["vtx_table_power_levels"] = payload[14]
    return result


def parse_vtx_power_level(payload: bytes) -> dict[str, int | str]:
    """Parse one MSP_VTXTABLE_POWERLEVEL response from Betaflight."""
    if len(payload) < 4:
        raise MspError(f"MSP_VTXTABLE_POWERLEVEL response is too short ({len(payload)} bytes).")
    level = payload[0]
    power_value = int.from_bytes(payload[1:3], "little")
    label_length = payload[3]
    label_bytes = payload[4 : 4 + label_length]
    if not level or len(label_bytes) != label_length:
        raise MspError("Invalid MSP_VTXTABLE_POWERLEVEL response.")
    return {
        "index": level,
        "value": power_value,
        "label": label_bytes.decode("ascii", errors="replace").rstrip("\0"),
    }


def parse_vtx_band(payload: bytes) -> dict[str, int | str | bool | list[int]]:
    """Parse one MSP_VTXTABLE_BAND response from Betaflight."""
    if len(payload) < 5:
        raise MspError(f"MSP_VTXTABLE_BAND response is too short ({len(payload)} bytes).")
    index = payload[0]
    name_length = payload[1]
    metadata_offset = 2 + name_length
    if not index or len(payload) < metadata_offset + 3:
        raise MspError("Invalid MSP_VTXTABLE_BAND response.")
    name_bytes = payload[2:metadata_offset]
    name = name_bytes.decode("ascii", errors="replace").rstrip("\0")
    letter = payload[metadata_offset]
    letter_char = chr(letter) if letter else ""
    if not letter_char.strip("\0 "):
        stripped_name = name.strip()
        letter_char = stripped_name[0] if stripped_name else ""
    is_factory = bool(payload[metadata_offset + 1])
    channel_count = payload[metadata_offset + 2]
    frequencies_offset = metadata_offset + 3
    expected_size = frequencies_offset + channel_count * 2
    if channel_count == 0 or len(payload) < expected_size:
        raise MspError("Invalid MSP_VTXTABLE_BAND channel data.")
    frequencies = [
        int.from_bytes(payload[offset : offset + 2], "little")
        for offset in range(frequencies_offset, expected_size, 2)
    ]
    return {
        "index": index,
        "name": name,
        "letter": letter_char,
        "is_factory": is_factory,
        "frequencies_mhz": frequencies,
    }


def read_vtx_bands(client: "MspClient", count: int) -> list[dict[str, int | str | bool | list[int]]]:
    """Read all VTX bands stored in the Flight Controller."""
    return [parse_vtx_band(client.request(MSP_VTXTABLE_BAND, bytes((index,)))) for index in range(1, count + 1)]


def read_vtx_power_levels(client: "MspClient", count: int) -> list[dict[str, int | str]]:
    """Read the VTX power table stored in the Flight Controller."""
    return [parse_vtx_power_level(client.request(MSP_VTXTABLE_POWERLEVEL, bytes((index,)))) for index in range(1, count + 1)]


class MspClient:
    """Minimal MSP v1 request/response client."""

    def __init__(self, serial_port, timeout_s: float) -> None:
        self._serial = serial_port
        self._timeout_s = timeout_s

    def request(self, command: int, payload: bytes = b"") -> bytes:
        self._serial.reset_input_buffer()
        self._serial.write(msp_v1_request(command, payload))
        self._serial.flush()
        frame = self._read_frame()
        if frame.command != command:
            raise MspError(f"Received MSP command {frame.command}; expected {command}.")
        if frame.direction == "!":
            raise MspError(f"Flight Controller rejected MSP command {command}.")
        if frame.direction != ">":
            raise MspError(f"Unexpected MSP direction {frame.direction!r}.")
        return frame.payload

    def _read_frame(self) -> MspFrame:
        deadline = time.monotonic() + self._timeout_s
        window = bytearray()
        while time.monotonic() < deadline:
            byte = self._serial.read(1)
            if not byte:
                continue
            window += byte
            if len(window) > 3:
                del window[0]
            if bytes(window) != b"$M":
                continue
            direction = self._serial.read(1)
            header = self._serial.read(2)
            if len(direction) != 1 or len(header) != 2:
                break
            payload_size, command = header
            payload = self._serial.read(payload_size)
            checksum = self._serial.read(1)
            if len(payload) != payload_size or len(checksum) != 1:
                break
            calculated = payload_size ^ command
            for item in payload:
                calculated ^= item
            if checksum[0] != calculated:
                raise MspError("Invalid MSP response checksum.")
            return MspFrame(direction.decode("ascii", errors="replace"), command, payload)
        raise MspError(f"No complete MSP response within {self._timeout_s:.1f} seconds.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read the current VTX configuration through Betaflight MSP.")
    parser.add_argument(
        "--port",
        help="Flight Controller serial port, for example COM5. Omit to auto-detect the first MSP-capable FC.",
    )
    parser.add_argument("--baud", type=int, default=115200, help="MSP serial baud rate (default: 115200).")
    parser.add_argument("--timeout", type=float, default=2.0, help="Response timeout in seconds (default: 2).")
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=DEFAULT_SCAN_TIMEOUT_S,
        help="Per-port timeout while auto-detecting the Flight Controller (default: 1).",
    )
    args = parser.parse_args()
    if args.baud <= 0 or args.timeout <= 0 or args.scan_timeout <= 0:
        parser.error("baud, timeout and scan-timeout must be positive.")
    return args


def list_serial_ports() -> list[str]:
    try:
        from serial.tools.list_ports import comports
    except ModuleNotFoundError as exc:
        raise MspError("pyserial is not installed. Run: python -m pip install -r requirements.txt") from exc
    return [port.device for port in comports()]


def open_serial(port: str, baud: int, timeout_s: float):
    try:
        import serial
    except ModuleNotFoundError as exc:
        raise MspError("pyserial is not installed. Run: python -m pip install -r requirements.txt") from exc
    try:
        return serial.Serial(port=port, baudrate=baud, timeout=0.1, write_timeout=timeout_s)
    except serial.SerialException as exc:
        raise MspError(f"Cannot open {port}: {exc}") from exc


def probe_flight_controller(port: str, baud: int, timeout_s: float) -> FlightControllerInfo | None:
    """Return FC details when the port responds to MSP, otherwise None."""
    try:
        with open_serial(port, baud, timeout_s) as serial_port:
            client = MspClient(serial_port, timeout_s)
            api = client.request(MSP_API_VERSION)
            variant = client.request(MSP_FC_VARIANT).decode("ascii", errors="replace").rstrip("\0")
            version = client.request(MSP_FC_VERSION)
    except MspError:
        return None
    if len(api) < 3 or len(version) < 3 or not variant:
        return None
    return FlightControllerInfo(
        port=port,
        variant=variant,
        firmware_version=f"{version[0]}.{version[1]}.{version[2]}",
        msp_api_version=f"{api[1]}.{api[2]}",
    )


def find_flight_controller_port(
    baud: int = 115200,
    scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
) -> FlightControllerInfo:
    ports = list_serial_ports()
    if not ports:
        raise MspError("No serial ports found. Connect the Flight Controller and try again.")

    print("Searching for Flight Controller...", flush=True)
    matches: list[FlightControllerInfo] = []
    for port in ports:
        info = probe_flight_controller(port, baud, scan_timeout_s)
        if info is not None:
            matches.append(info)

    if not matches:
        raise MspError(
            "No Flight Controller found. Close Betaflight Configurator, connect the FC via USB, "
            "and ensure MSP is enabled on the serial port."
        )
    if len(matches) > 1:
        details = ", ".join(f"{item.port} ({item.variant} {item.firmware_version})" for item in matches)
        raise MspError(f"Multiple Flight Controllers found: {details}. Pass --port to choose one.")
    return matches[0]


def resolve_port(
    port: str | None,
    baud: int = 115200,
    timeout_s: float = 2.0,
    *,
    scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
) -> str:
    if port:
        return port
    info = find_flight_controller_port(baud, scan_timeout_s)
    print(
        f"Using Flight Controller on {info.port} "
        f"({info.variant} {info.firmware_version}, MSP API {info.msp_api_version}).",
        flush=True,
    )
    return info.port


def main() -> int:
    args = parse_args()
    try:
        port = resolve_port(args.port, args.baud, args.timeout, scan_timeout_s=args.scan_timeout)
        with open_serial(port, args.baud, args.timeout) as serial_port:
            client = MspClient(serial_port, args.timeout)
            api = client.request(MSP_API_VERSION)
            variant = client.request(MSP_FC_VARIANT).decode("ascii", errors="replace").rstrip("\0")
            version = client.request(MSP_FC_VERSION)
            if len(api) < 3 or len(version) < 3:
                raise MspError("Flight Controller returned an incomplete API or firmware version.")
            vtx = parse_vtx_config(client.request(MSP_VTX_CONFIG))
            bands = read_vtx_bands(client, int(vtx["vtx_table_bands"] or 0))
            power_levels = read_vtx_power_levels(client, int(vtx["vtx_table_power_levels"] or 0))
        result = {
            "flight_controller": {
                "variant": variant,
                "firmware_version": f"{version[0]}.{version[1]}.{version[2]}",
                "msp_protocol_version": api[0],
                "msp_api_version": f"{api[1]}.{api[2]}",
            },
            "vtx": vtx,
            "vtx_bands": bands,
            "vtx_power_table": power_levels,
        }
        print(json.dumps(result, indent=2))
        return 0
    except MspError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
