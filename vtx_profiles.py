"""Load and select local VTX profiles for interactive test tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROFILE_DIRECTORY = Path(__file__).with_name("configs") / "vtx_profiles"


def load_profile(path: Path) -> dict[str, Any]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read VTX profile {path}: {exc}") from exc
    if not isinstance(profile.get("id"), str) or not isinstance(profile.get("model"), str):
        raise ValueError(f"VTX profile {path} has no valid id or model.")
    bands = profile.get("bands")
    if not isinstance(bands, dict) or not bands:
        raise ValueError(f"VTX profile {path} has no valid bands.")
    if len(bands) > 8:
        raise ValueError(f"VTX profile {path} has {len(bands)} bands; Betaflight supports at most 8.")
    return profile


def available_profiles(profile_directory: Path = PROFILE_DIRECTORY) -> list[tuple[Path, dict[str, Any]]]:
    profiles = [(path, load_profile(path)) for path in sorted(profile_directory.glob("*.json"))]
    if not profiles:
        raise ValueError(f"No VTX profiles found in {profile_directory}.")
    return profiles


def select_profile() -> tuple[Path, dict[str, Any]]:
    """Prompt the operator for one installed VTX profile."""
    profiles = available_profiles()
    print("Select VTX model:")
    for index, (_, profile) in enumerate(profiles, start=1):
        lower, upper = profile["frequency_range_mhz"]
        print(f"  {index}. {profile['model']} ({lower}-{upper} MHz)")
    while True:
        answer = input(f"Model [1-{len(profiles)}]: ").strip()
        try:
            selected = int(answer)
        except ValueError:
            selected = 0
        if 1 <= selected <= len(profiles):
            return profiles[selected - 1]
        print("Enter a number from the list.")
