"""Frequency-dependent calibration for the VTX-to-NRP-Z11 measurement path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_measurement_path(path: Path) -> dict[str, Any]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        offsets = profile["level_offset_db_by_frequency_mhz"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read measurement-path profile {path}: {exc}") from exc
    if profile.get("offset_direction") != "set_on_sensor_db" or not isinstance(offsets, dict):
        raise ValueError(f"Measurement-path profile {path} has an unsupported offset configuration.")
    try:
        fixed_attenuator_db = float(profile.get("fixed_attenuator_db", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Measurement-path profile {path} has an invalid fixed_attenuator_db.") from exc
    try:
        parsed_offsets = {int(frequency): float(offset) for frequency, offset in offsets.items()}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Measurement-path profile {path} contains invalid frequency offsets.") from exc
    if not parsed_offsets or any(frequency <= 0 for frequency in parsed_offsets):
        raise ValueError(f"Measurement-path profile {path} contains no valid frequency offsets.")
    profile["_offsets"] = dict(sorted(parsed_offsets.items()))
    profile["_fixed_attenuator_db"] = fixed_attenuator_db
    return profile


def offset_db_at_frequency(profile: dict[str, Any], frequency_mhz: int) -> float:
    """Return the full NRP Level Offset, interpolating only the path-loss component."""
    if frequency_mhz <= 0:
        raise ValueError("Frequency must be positive.")
    offsets = profile.get("_offsets")
    if not isinstance(offsets, dict) or not offsets:
        raise ValueError("Measurement-path profile has not been loaded.")
    points = sorted((int(frequency), float(offset)) for frequency, offset in offsets.items())
    fixed_attenuator_db = float(profile.get("_fixed_attenuator_db", 0.0))
    if frequency_mhz <= points[0][0]:
        return fixed_attenuator_db + points[0][1]
    if frequency_mhz >= points[-1][0]:
        return fixed_attenuator_db + points[-1][1]
    for (lower_frequency, lower_offset), (upper_frequency, upper_offset) in zip(points, points[1:]):
        if lower_frequency <= frequency_mhz <= upper_frequency:
            fraction = (frequency_mhz - lower_frequency) / (upper_frequency - lower_frequency)
            return fixed_attenuator_db + lower_offset + fraction * (upper_offset - lower_offset)
    raise ValueError(f"Cannot interpolate a path offset at {frequency_mhz} MHz.")
