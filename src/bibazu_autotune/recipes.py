"""Portable JSON recipe validation; server remains authoritative for writes."""

import copy
import math

from .trial_context import manual_identity, read_context

LIMITS = {
    "pressure_mbar": (0, 6000, int),
    "pulse_duration_ms": (1, 500, int),
    "delay_ms": (0, 1000, int),
    "offset_mm": (0, 5000, float),
}


def number(value, low, high, kind):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (
        math.isfinite(value)
        and low <= value <= high
        and (kind is not int or isinstance(value, int))
    )


def validate_parameters(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {
        "arrays",
        "conveyor_speed_mm_per_sec",
    }:
        raise ValueError("Parametersatz benötigt arrays und conveyor_speed_mm_per_sec.")
    speed = parameters["conveyor_speed_mm_per_sec"]
    if not number(speed, 0, 200, float):
        raise ValueError("Bandgeschwindigkeit muss zwischen 0 und 200 mm/s liegen.")
    arrays = parameters["arrays"]
    if not isinstance(arrays, list) or len(arrays) != 4:
        raise ValueError("Genau vier Arrays sind erforderlich.")
    fields = {"index", "enabled", "nozzles_enabled", "conveyor_velocity_delay", *LIMITS}
    indices = []
    for array in arrays:
        if not isinstance(array, dict) or set(array) != fields:
            raise ValueError("Unvollständige oder unbekannte Array-Parameter.")
        if type(array["index"]) is not int:
            raise ValueError("Array-Index muss eine Ganzzahl sein.")
        indices.append(array["index"])
        if type(array["enabled"]) is not bool or type(array["conveyor_velocity_delay"]) is not bool:
            raise ValueError("Array-Aktivierung und Geschwindigkeitsmodus müssen boolesch sein.")
        nozzles = array["nozzles_enabled"]
        if (
            not isinstance(nozzles, list)
            or len(nozzles) != 6
            or any(type(n) is not bool for n in nozzles)
        ):
            raise ValueError("Pro Array sind sechs Düsen-Aktivierungen erforderlich.")
        for key, (low, high, kind) in LIMITS.items():
            if not number(array[key], low, high, kind):
                raise ValueError(f"{key}: Wert außerhalb {low}–{high}.")
        if array["enabled"] and array["conveyor_velocity_delay"] and speed == 0:
            raise ValueError("Offset mit Bandgeschwindigkeit benötigt mehr als 0 mm/s.")
    if sorted(indices) != [1, 2, 3, 4]:
        raise ValueError("Array-Indizes müssen genau 1, 2, 3, 4 sein.")
    result = copy.deepcopy(parameters)
    result["arrays"].sort(key=lambda a: a["index"])
    return result


def transition_key(context):
    if not context:
        return None
    if context.get("kind") == "manual":
        return manual_identity(context)
    return [
        context.get(k)
        for k in (
            "part_name",
            "source_pose_id",
            "target_pose_id",
            "actuation",
            "component_edge_ids",
        )
    ]


def transition_identity(context):
    """Exact local choice identity, including grouped choices with the same pose pair."""
    if not context:
        return None
    return [*transition_key(context), context.get("edge_id"), context.get("source_group_key")]


def validate_recipe(recipe, context):
    if not isinstance(recipe, dict) or recipe.get("schema_version") != 1:
        raise ValueError("Unbekanntes Testreihenformat.")
    saved_context = read_context(recipe)
    if context is not None and transition_key(saved_context) != transition_key(context):
        raise ValueError("Testreihe gehört zu einer anderen Roadmap-Transition.")
    if context is not None and any(
        saved_context.get(key) and context.get(key) and saved_context[key] != context[key]
        for key in ("edge_id", "source_group_key")
    ):
        raise ValueError("Testreihe gehört zu einer anderen Roadmap-Transition.")
    variants = recipe.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ValueError("Testreihe enthält keine Varianten.")
    result = []
    for item in variants:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("Variante benötigt einen Namen und Parameter.")
        result.append(
            {"name": item["name"], "parameters": validate_parameters(item.get("parameters"))}
        )
    return result
