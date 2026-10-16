"""Local version-13 profile IO; machine metadata is never sent to the PLC."""

from __future__ import annotations

import copy
from datetime import datetime

from .recipes import LIMITS, validate_parameters


def empty_parameters():
    return {
        "conveyor_speed_mm_per_sec": 0.0,
        "arrays": [
            {
                "index": i,
                "enabled": False,
                "nozzles_enabled": [False] * 6,
                "pressure_mbar": 0,
                "pulse_duration_ms": 100,
                "delay_ms": 0,
                "offset_mm": 0.0,
                "conveyor_velocity_delay": False,
            }
            for i in range(1, 5)
        ],
    }


def profile_parameters(profile):
    if not isinstance(profile, dict) or profile.get("version") != 13:
        raise ValueError("AutoTune unterstützt Pressure-Profile der Version 13.")
    arrays = profile.get("arrays")
    if not isinstance(arrays, list):
        raise ValueError("Profil enthält keine Arrays.")
    fields = {"index", "enabled", "conveyor_velocity_delay", *LIMITS}
    normalized = []
    for array in arrays:
        if not isinstance(array, dict) or not fields <= array.keys():
            raise ValueError("Unvollständige Array-Parameter im Profil.")
        nozzles = array.get("nozzles_enabled")
        if nozzles is None:
            nozzles = [array.get(f"nozzle_{n}_enabled", False) for n in range(1, 7)]
        normalized.append({**{key: array[key] for key in fields}, "nozzles_enabled": nozzles})
    return validate_parameters(
        {
            "arrays": normalized,
            "conveyor_speed_mm_per_sec": profile.get("conveyor_speed_mm_per_sec"),
        }
    )


def make_profile(parameters, context, machine_metadata=None, template=None, conveyor=None):
    parameters = validate_parameters(parameters)
    profile = copy.deepcopy(template or {})
    profile.update(copy.deepcopy(machine_metadata or {}))
    # Robot angle is configured metadata, not a measurement or a robot command.
    profile.setdefault("ur_ry_angle_deg", 18.0)
    profile.update(
        {
            "version": 13,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "title": context.get("title")
            or f"Transition {context['source_pose_id']}-{context['target_pose_id']}",
            "roadmap_transition": copy.deepcopy(context),
            "conveyor_enabled": bool((conveyor or {}).get("enabled", False)),
            "conveyor_reverse": bool((conveyor or {}).get("reverse", False)),
            **parameters,
        }
    )
    for array in profile["arrays"]:
        array.update(
            {
                f"nozzle_{n}_enabled": enabled
                for n, enabled in enumerate(array["nozzles_enabled"], 1)
            }
        )
    return profile
