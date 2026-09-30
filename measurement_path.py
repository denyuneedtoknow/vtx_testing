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


def offset_db_at_frequency(profile: dict[str, Any], frequency_mhz: int, *, path_adjustment: str = "add") -> float:
    """Return the NRP Level Offset with an additive or subtractive path adjustment."""
    if frequency_mhz <= 0:
        raise ValueError("Frequency must be positive.")
    if path_adjustment not in {"add", "subtract"}:
        raise ValueError("path_adjustment must be add or subtract.")
    offsets = profile.get("_offsets")
    if not isinstance(offsets, dict) or not offsets:
        raise ValueError("Measurement-path profile has not been loaded.")
    points = sorted((int(frequency), float(offset)) for frequency, offset in offsets.items())
    fixed_attenuator_db = float(profile.get("_fixed_attenuator_db", 0.0))
    if frequency_mhz <= points[0][0]:
        path_adjustment_db = points[0][1]
    elif frequency_mhz >= points[-1][0]:
        path_adjustment_db = points[-1][1]
    else:
        path_adjustment_db = None
    for (lower_frequency, lower_offset), (upper_frequency, upper_offset) in zip(points, points[1:]):
        if lower_frequency <= frequency_mhz <= upper_frequency:
            fraction = (frequency_mhz - lower_frequency) / (upper_frequency - lower_frequency)
            path_adjustment_db = lower_offset + fraction * (upper_offset - lower_offset)
            break
    if path_adjustment_db is None:
        raise ValueError(f"Cannot interpolate a path offset at {frequency_mhz} MHz.")
    return fixed_attenuator_db + path_adjustment_db if path_adjustment == "add" else fixed_attenuator_db - path_adjustment_db


def validate_sensor_frequencies(profile: dict[str, Any], frequencies_mhz: list[int]) -> None:
    """Fail before VTX switching when a requested frequency exceeds the sensor range."""
    maximum = profile.get("sensor_max_frequency_mhz")
    if not isinstance(maximum, int) or maximum <= 0:
        raise ValueError("Measurement-path profile is missing a valid sensor_max_frequency_mhz.")
    unsupported = sorted({frequency for frequency in frequencies_mhz if frequency > maximum})
    if unsupported:
        rendered = ", ".join(f"{frequency} MHz" for frequency in unsupported[:5])
        suffix = "…" if len(unsupported) > 5 else ""
        raise ValueError(
            f"NRP-Z11 supports up to {maximum} MHz; the selected test requires {rendered}{suffix}. "
            "No VTX settings were changed."
        )
