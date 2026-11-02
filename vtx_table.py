"""Install and verify the per-model Betaflight VTX tables used by the tester."""

from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import Any

from vtx_a1_measure import build_set_vtx_config_payload
from vtx_msp_probe import (
    MSP_EEPROM_WRITE,
    MSP_SET_VTX_CONFIG,
    MSP_SET_VTXTABLE_BAND,
    MSP_SET_VTXTABLE_POWERLEVEL,
    MSP_VTX_CONFIG,
    MspClient,
    MspError,
    parse_vtx_config,
)
from vtx_profiles import load_profile


ROOT = Path(__file__).resolve().parent
PROFILE_DIRECTORY = ROOT / "configs" / "vtx_profiles"
TABLE_PROFILE_FILENAMES = {
    "1.2": "ft_vtx_1_2_4w.json",
    "2.7": "ft_vtx_2_7_4w.json",
    "3.3": "ft_vtx_3_3_4w.json",
    "5.8": "ft_vtx_5_8_4w_64ch.json",
    "5.8.legacy": "ft_vtx_5_8_legacy_4w.json",
    "5.8.test": "ft_vtx_graph58_p5_5w.json",
    "7.2": "ft_vtx_7_2_5w.json",
    "8.5": "ft_vtx_8_5_5w_64ch.json",
}


def load_vtx_table(vtx: str) -> dict[str, Any]:
    """Return the exact <=8-band table to install for an operator VTX choice."""
    try:
        profile = load_profile(PROFILE_DIRECTORY / TABLE_PROFILE_FILENAMES[vtx])
    except KeyError as exc:
        raise ValueError(f"No Betaflight VTX table is defined for {vtx!r}.") from exc
    validate_table(profile)
    return profile


def validate_table(profile: dict[str, Any]) -> None:
    bands, powers = profile.get("bands"), profile.get("power_levels_mw")
    if not isinstance(bands, dict) or not isinstance(powers, list) or not 1 <= len(bands) <= 8 or not 1 <= len(powers) <= 8:
        raise ValueError("VTX table must contain 1..8 bands and 1..8 power levels.")
    lengths = {len(frequencies) for frequencies in bands.values() if isinstance(frequencies, list)}
    if len(lengths) != 1 or not lengths or not 1 <= next(iter(lengths)) <= 8:
        raise ValueError("Every VTX table band must have the same 1..8 channel count.")
    if not all(isinstance(letter, str) and len(letter) == 1 and all(isinstance(freq, int) and freq >= 0 for freq in frequencies) for letter, frequencies in bands.items()):
        raise ValueError("VTX table bands must use one-letter names and non-negative integer frequencies.")
    if not all(isinstance(power, int) and power >= 0 for power in powers) or 25 not in powers:
        raise ValueError("VTX table needs integer power values including the 25 mW safety level.")


def _band_payload(index: int, letter: str, frequencies: list[int]) -> bytes:
    name = f"BAND_{letter}".encode("ascii")[:8]
    return bytes((index, len(name))) + name + letter.encode("ascii") + b"\0" + bytes((len(frequencies),)) + struct.pack("<" + "H" * len(frequencies), *frequencies)


def _power_payload(index: int, value: int) -> bytes:
    label = "PIT" if value == 0 else (f"{value // 1000}W" if value >= 1000 and value % 1000 == 0 else (f"{value / 1000:g}W" if value >= 1000 else str(value)))
    encoded = label.encode("ascii")[:8]
    return bytes((index,)) + struct.pack("<H", value) + bytes((len(encoded),)) + encoded


def install_vtx_table(client: MspClient, profile: dict[str, Any]) -> dict[str, int | bool | None]:
    """Replace FC table, persist it, then leave the VTX at band 1/channel 1/25mW.

    The FC must be disarmed. Every write is followed by a read-back verification by
    the caller before a test is allowed to begin.
    """
    validate_table(profile)
    bands: dict[str, list[int]] = profile["bands"]
    powers: list[int] = profile["power_levels_mw"]
    current = parse_vtx_config(client.request(MSP_VTX_CONFIG))
    first_frequency = next(iter(bands.values()))[0]
    safe_index = powers.index(25) + 1
    base = build_set_vtx_config_payload(current, 1, 1, first_frequency, safe_index)
    client.request(MSP_SET_VTX_CONFIG, base + bytes((len(bands), len(next(iter(bands.values()))), len(powers), 1)))
    for index, (letter, frequencies) in enumerate(bands.items(), start=1):
        client.request(MSP_SET_VTXTABLE_BAND, _band_payload(index, letter, frequencies))
    for index, value in enumerate(powers, start=1):
        client.request(MSP_SET_VTXTABLE_POWERLEVEL, _power_payload(index, value))
    client.request(MSP_EEPROM_WRITE)
    time.sleep(0.25)
    # Import here to avoid a module cycle: the test runner also imports this
    # table helper through the GUI.
    from vtx_three_point_test import switch_and_confirm, verify_vtx_table

    updated = parse_vtx_config(client.request(MSP_VTX_CONFIG))
    verify_vtx_table(client, updated, profile)
    updated = switch_and_confirm(
        client, updated, band=1, channel=1, frequency_mhz=first_frequency,
        power_index=safe_index, settle_time_s=0.5, direct_frequency=False,
    )
    client.request(MSP_EEPROM_WRITE)
    return updated
